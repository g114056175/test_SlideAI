"""Optional forced-alignment repair helpers.

This module is deliberately isolated from the main subtitle pipeline.  Set
SLIDEAI_ALIGNMENT_REPAIR=0 to disable it, or remove this module plus the small
hook in subtitle_alignment.py when the upstream aligner becomes reliable enough.
"""

from __future__ import annotations

import math
import os
import re
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from backend.app.services.alignment.time_mapper import _align_clean


@dataclass(frozen=True)
class RepairPlan:
    enabled: bool
    reason: str = ""
    repaired_text: str = ""
    char_map: List[Tuple[int, int]] | None = None


_NUMBER_PATTERN = r"\d+(?:\.\d+)?"
_UNIT_PATTERN = r"(?:px|gb|mb|kb|tb|pb|ms|hz|khz|mhz|ghz|fps|dpi|%)"
_NUM_UNIT_RE = re.compile(
    rf"(?<![A-Za-z0-9_]){_NUMBER_PATTERN}{_UNIT_PATTERN}(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_RISKY_TOKEN_RE = re.compile(rf"^{_NUMBER_PATTERN}{_UNIT_PATTERN}$", re.IGNORECASE)
_NUMBER_ONLY_RE = re.compile(rf"^{_NUMBER_PATTERN}$")
_UNIT_ONLY_RE = re.compile(rf"^{_UNIT_PATTERN}$", re.IGNORECASE)
_REWRITE_RE = _NUM_UNIT_RE

_DIGITS = {
    "0": "零",
    "1": "一",
    "2": "二",
    "3": "三",
    "4": "四",
    "5": "五",
    "6": "六",
    "7": "七",
    "8": "八",
    "9": "九",
}


def repair_enabled() -> bool:
    return str(os.getenv("SLIDEAI_ALIGNMENT_REPAIR", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _strip_display_punct(text: str) -> str:
    return str(text or "").strip().strip("，。！？；：、,.!?;:\"'“”‘’()[]{}<>《》【】…")


def _is_risky_numeric_unit(text: str) -> bool:
    return bool(_RISKY_TOKEN_RE.fullmatch(_strip_display_punct(text)))


def _word_duration(word: Dict[str, Any]) -> float:
    start = float(word.get("start") or 0.0)
    end = float(word.get("end") or start)
    return max(0.0, end - start)


def find_risky_alignment_units(words: Sequence[Dict[str, Any]]) -> List[int]:
    """Return indexes that look like collapsed numeric/unit alignments."""
    bad: List[int] = []
    rates: List[float] = []
    for word in words:
        clean_len = len(_align_clean(str(word.get("text") or "")))
        duration = _word_duration(word)
        if clean_len > 0 and duration > 0.05:
            rates.append(duration / clean_len)
    median_rate = statistics.median(rates) if rates else 0.0

    idx = 0
    while idx < len(words):
        word = words[idx]
        text = _strip_display_punct(str(word.get("text") or ""))
        end_idx = idx
        combined_text = text
        # Some aligners emit "1.23" and "GB" as two adjacent units.  Treat
        # that pair exactly like a combined "1.23GB" token.
        if (
            _NUMBER_ONLY_RE.fullmatch(text)
            and idx + 1 < len(words)
            and _UNIT_ONLY_RE.fullmatch(
                _strip_display_punct(str(words[idx + 1].get("text") or ""))
            )
        ):
            end_idx = idx + 1
            combined_text += _strip_display_punct(str(words[end_idx].get("text") or ""))
        if not _is_risky_numeric_unit(combined_text):
            idx += 1
            continue
        start = float(word.get("start") or 0.0)
        end = float(words[end_idx].get("end") or start)
        duration = max(0.0, end - start)
        next_start = None
        if end_idx + 1 < len(words):
            next_start = float(words[end_idx + 1].get("start") or 0.0)
        next_gap = max(0.0, (next_start - end)) if next_start is not None else 0.0
        clean_len = max(1, len(_align_clean(combined_text)))

        # Numeric-unit tokens usually need more acoustic time than a single CJK
        # character.  A zero/near-zero duration or a large following gap usually
        # means the aligner skipped the spoken number/unit.
        expected_floor = min(0.75, max(0.18, clean_len * 0.055))
        observed_rate = duration / clean_len
        locally_collapsed = (
            median_rate > 0.0
            and observed_rate < median_rate * 0.30
            and duration < expected_floor
        )
        suspicious_pause = (
            duration < expected_floor
            and next_gap >= 0.18
        )
        # A long natural pause after a correctly aligned number is not itself
        # an anomaly.  Require the token duration to be short as well.
        large_gap_with_short_token = (
            next_gap >= 0.45
            and duration < expected_floor * 0.75
        )
        if duration <= 0.05 or suspicious_pause or large_gap_with_short_token or locally_collapsed:
            bad.extend(range(idx, end_idx + 1))
        idx = end_idx + 1
    return bad


def _int_group_to_zh(group: int, *, omit_leading_one_ten: bool) -> str:
    units = ["千", "百", "十", ""]
    divisors = [1000, 100, 10, 1]
    out: List[str] = []
    zero_pending = False
    for pos, divisor in enumerate(divisors):
        digit = (group // divisor) % 10
        if digit:
            if zero_pending and out:
                out.append("零")
            if not (
                divisor == 10
                and digit == 1
                and not out
                and omit_leading_one_ten
            ):
                out.append(_DIGITS[str(digit)])
            out.append(units[pos])
            zero_pending = False
        elif out and group % divisor:
            zero_pending = True
    return "".join(out)


def _int_to_zh(num: int) -> str:
    if num == 0:
        return "零"
    if num < 0:
        return "負" + _int_to_zh(abs(num))
    big_units = ["", "萬", "億", "兆", "京"]
    groups: List[int] = []
    value = num
    while value:
        groups.append(value % 10000)
        value //= 10000
    if len(groups) > len(big_units):
        # Extremely large technical identifiers are more reliably spoken as
        # individual digits than through unsupported Chinese large-number units.
        return "".join(_DIGITS[ch] for ch in str(num))

    out: List[str] = []
    skipped_group = False
    highest = len(groups) - 1
    for group_idx in range(highest, -1, -1):
        group = groups[group_idx]
        if group == 0:
            if out:
                skipped_group = True
            continue
        if out and (skipped_group or group < 1000):
            if out[-1] != "零":
                out.append("零")
        group_text = _int_group_to_zh(
            group,
            omit_leading_one_ten=(not out),
        )
        out.append(group_text)
        out.append(big_units[group_idx])
        skipped_group = False
    return "".join(out)


def _number_to_spoken_zh(num_text: str) -> str:
    raw = str(num_text or "")
    if "." in raw:
        left, right = raw.split(".", 1)
        left_text = _int_to_zh(int(left or "0"))
        right_text = "".join(_DIGITS.get(ch, ch) for ch in right if ch.isdigit())
        return f"{left_text}點{right_text}" if right_text else left_text
    return _int_to_zh(int(raw or "0"))


_EN_ONES = (
    "zero", "one", "two", "three", "four",
    "five", "six", "seven", "eight", "nine",
)
_EN_TEENS = {
    10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen",
}
_EN_TENS = {
    20: "twenty", 30: "thirty", 40: "forty", 50: "fifty",
    60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety",
}
_EN_SCALES = (
    (1_000_000_000_000, "trillion"),
    (1_000_000_000, "billion"),
    (1_000_000, "million"),
    (1_000, "thousand"),
)
_EN_UNIT_NAMES = {
    "px": ("pixel", "pixels"),
    "kb": ("kilobyte", "kilobytes"),
    "mb": ("megabyte", "megabytes"),
    "gb": ("gigabyte", "gigabytes"),
    "tb": ("terabyte", "terabytes"),
    "pb": ("petabyte", "petabytes"),
    "ms": ("millisecond", "milliseconds"),
    "hz": ("hertz", "hertz"),
    "khz": ("kilohertz", "kilohertz"),
    "mhz": ("megahertz", "megahertz"),
    "ghz": ("gigahertz", "gigahertz"),
    "fps": ("frame per second", "frames per second"),
    "dpi": ("dot per inch", "dots per inch"),
}


def _int_to_en(num: int) -> str:
    if num < 0:
        return "minus " + _int_to_en(abs(num))
    if num < 10:
        return _EN_ONES[num]
    if num < 20:
        return _EN_TEENS[num]
    if num < 100:
        tens = (num // 10) * 10
        rest = num % 10
        return _EN_TENS[tens] + (f" {_EN_ONES[rest]}" if rest else "")
    if num < 1000:
        rest = num % 100
        return f"{_EN_ONES[num // 100]} hundred" + (f" {_int_to_en(rest)}" if rest else "")
    for scale, name in _EN_SCALES:
        if num >= scale:
            rest = num % scale
            return f"{_int_to_en(num // scale)} {name}" + (f" {_int_to_en(rest)}" if rest else "")
    return " ".join(_EN_ONES[int(ch)] for ch in str(num))


def _number_to_spoken_en(num_text: str) -> str:
    raw = str(num_text or "")
    if "." in raw:
        left, right = raw.split(".", 1)
        right_text = " ".join(_EN_ONES[int(ch)] for ch in right if ch.isdigit())
        return f"{_int_to_en(int(left or '0'))} point {right_text}".strip()
    return _int_to_en(int(raw or "0"))


def _is_english_language(language: str) -> bool:
    value = str(language or "").strip().lower()
    return value.startswith("en") or "english" in value


def _rewrite_numeric_unit(match: re.Match[str], language: str = "Chinese") -> str:
    raw = match.group(0)
    m = re.match(r"(\d+(?:\.\d+)?)(.*)", raw)
    if not m:
        return raw
    number = m.group(1)
    suffix = m.group(2)
    if _is_english_language(language):
        number_spoken = _number_to_spoken_en(number)
        if suffix == "%":
            return f"{number_spoken} percent"
        unit_names = _EN_UNIT_NAMES.get(suffix.lower())
        if unit_names:
            singular = float(number) == 1.0
            return f"{number_spoken} {unit_names[0] if singular else unit_names[1]}"
        return f"{number_spoken} {suffix}"

    number_spoken = _number_to_spoken_zh(number)
    if suffix == "%":
        return "百分之" + number_spoken
    if suffix.lower() == "px":
        return number_spoken + "pixel"
    return number_spoken + suffix.upper()


def _append_piece(
    out: List[str],
    char_map: List[Tuple[int, int]],
    cursor: int,
    original: str,
    replacement: str,
) -> int:
    out.append(replacement)
    orig_clean = _align_clean(original)
    repl_clean = _align_clean(replacement)
    repl_len = len(repl_clean)
    orig_len = len(orig_clean)
    if orig_len <= 0:
        return cursor + repl_len
    if repl_len <= 0:
        for _ in range(orig_len):
            char_map.append((cursor, cursor))
        return cursor
    for idx in range(orig_len):
        start = int(math.floor(idx * repl_len / orig_len))
        end = int(math.floor((idx + 1) * repl_len / orig_len))
        if end <= start:
            end = start + 1
        char_map.append((cursor + start, min(cursor + repl_len, cursor + end)))
    return cursor + repl_len


def build_repaired_alignment_text(
    source_text: str,
    language: str = "Chinese",
) -> Tuple[str, List[Tuple[int, int]]]:
    """Rewrite risky display tokens into spoken form and map original chars back.

    The returned char_map has one item per _align_clean(source_text) character.
    Each tuple points to the corresponding clean-char range in repaired_text.
    """
    src = str(source_text or "")
    out: List[str] = []
    char_map: List[Tuple[int, int]] = []
    cursor = 0
    pos = 0
    for match in _REWRITE_RE.finditer(src):
        if match.start() > pos:
            chunk = src[pos:match.start()]
            cursor = _append_piece(out, char_map, cursor, chunk, chunk)
        original = match.group(0)
        replacement = _rewrite_numeric_unit(match, language=language)
        cursor = _append_piece(out, char_map, cursor, original, replacement)
        pos = match.end()
    if pos < len(src):
        chunk = src[pos:]
        cursor = _append_piece(out, char_map, cursor, chunk, chunk)
    return "".join(out), char_map


def make_repair_plan(
    source_text: str,
    words: Sequence[Dict[str, Any]],
    language: str = "Chinese",
) -> RepairPlan:
    if not repair_enabled():
        return RepairPlan(enabled=False, reason="disabled")
    bad = find_risky_alignment_units(words)
    if not bad:
        return RepairPlan(enabled=False, reason="no_anomaly")
    repaired_text, char_map = build_repaired_alignment_text(source_text, language=language)
    if _align_clean(repaired_text) == _align_clean(source_text):
        return RepairPlan(enabled=False, reason="no_rewrite")
    return RepairPlan(
        enabled=True,
        reason=",".join(str(i) for i in bad),
        repaired_text=repaired_text,
        char_map=char_map,
    )


def map_repaired_spans_to_source(
    source_text: str,
    repaired_char_spans: Sequence[Tuple[float, float]],
    char_map: Sequence[Tuple[int, int]],
) -> List[Tuple[float, float]]:
    source_clean_len = len(_align_clean(source_text))
    if source_clean_len <= 0:
        return []
    spans: List[Tuple[float, float]] = []
    max_idx = len(repaired_char_spans)
    for idx in range(source_clean_len):
        if idx >= len(char_map):
            break
        start_idx, end_idx = char_map[idx]
        start_idx = max(0, min(max_idx - 1, int(start_idx))) if max_idx else 0
        end_idx = max(start_idx + 1, min(max_idx, int(end_idx))) if max_idx else 0
        if not max_idx:
            spans.append((0.0, 0.001))
            continue
        start = float(repaired_char_spans[start_idx][0])
        end = float(repaired_char_spans[end_idx - 1][1])
        if end <= start:
            end = start + 0.001
        spans.append((start, end))
    return spans


def validate_repaired_spans(
    source_text: str,
    mapped_spans: Sequence[Tuple[float, float]],
    *,
    audio_end: float,
    text_match_ratio: float,
    min_text_match_ratio: float = 0.55,
) -> Tuple[bool, str]:
    """Reject a second alignment pass that would make the timeline less safe."""
    expected_len = len(_align_clean(source_text))
    if expected_len <= 0:
        return False, "empty_source"
    if len(mapped_spans) != expected_len:
        return False, f"length_mismatch:{len(mapped_spans)}/{expected_len}"
    if text_match_ratio < min_text_match_ratio:
        return False, f"text_match_too_low:{text_match_ratio:.3f}"

    previous_start = -1.0
    previous_end = -1.0
    upper_bound = max(0.1, float(audio_end or 0.0)) + 0.35
    positive_durations = 0
    for start, end in mapped_spans:
        start = float(start)
        end = float(end)
        if not math.isfinite(start) or not math.isfinite(end):
            return False, "non_finite_span"
        if start < -0.01 or end <= start:
            return False, "invalid_span"
        if start + 1e-3 < previous_start or end + 1e-3 < previous_end:
            return False, "non_monotonic_span"
        if end > upper_bound:
            return False, f"span_beyond_audio:{end:.3f}/{audio_end:.3f}"
        if end - start > 1e-3:
            positive_durations += 1
        previous_start = start
        previous_end = end
    if positive_durations < max(1, int(expected_len * 0.8)):
        return False, "too_many_collapsed_spans"
    return True, "ok"
