"""Optional forced-alignment repair helpers.

This module is deliberately isolated from the main subtitle pipeline.  Set
SLIDEAI_ALIGNMENT_REPAIR=0 to disable it, or remove this module plus the small
hook in subtitle_alignment.py when the upstream aligner becomes reliable enough.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from backend.app.services.alignment.time_mapper import _align_clean


@dataclass(frozen=True)
class RepairPlan:
    enabled: bool
    reason: str = ""
    repaired_text: str = ""
    char_map: List[Tuple[int, int]] | None = None


_NUM_UNIT_RE = re.compile(r"\d+(?:\.\d+)?(?:px|PX|gb|GB|mb|MB|kb|KB|tb|TB|ms|MS)")
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?%")
_RISKY_TOKEN_RE = re.compile(r"^(?:\d+(?:\.\d+)?(?:px|PX|gb|GB|mb|MB|kb|KB|tb|TB|ms|MS)|\d+(?:\.\d+)?%)")
_REWRITE_RE = re.compile(r"\d+(?:\.\d+)?(?:px|PX|gb|GB|mb|MB|kb|KB|tb|TB|ms|MS|%)")

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
    return bool(_RISKY_TOKEN_RE.match(_strip_display_punct(text)))


def _word_duration(word: Dict[str, Any]) -> float:
    start = float(word.get("start") or 0.0)
    end = float(word.get("end") or start)
    return max(0.0, end - start)


def find_risky_alignment_units(words: Sequence[Dict[str, Any]]) -> List[int]:
    """Return indexes that look like collapsed numeric/unit alignments."""
    bad: List[int] = []
    for idx, word in enumerate(words):
        text = str(word.get("text") or "")
        if not _is_risky_numeric_unit(text):
            continue
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or start)
        duration = max(0.0, end - start)
        next_start = None
        if idx + 1 < len(words):
            next_start = float(words[idx + 1].get("start") or 0.0)
        next_gap = max(0.0, (next_start - end)) if next_start is not None else 0.0
        clean_len = max(1, len(_align_clean(text)))

        # Numeric-unit tokens usually need more acoustic time than a single CJK
        # character.  A zero/near-zero duration or a large following gap usually
        # means the aligner skipped the spoken number/unit.
        expected_floor = min(0.75, max(0.18, clean_len * 0.055))
        if duration <= 0.05 or (duration < expected_floor and next_gap >= 0.18) or next_gap >= 0.45:
            bad.append(idx)
    return bad


def _int_to_zh(num: int) -> str:
    if num == 0:
        return "零"
    if num < 0:
        return "負" + _int_to_zh(abs(num))
    units = ["", "十", "百", "千"]
    big_units = ["", "萬", "億"]
    parts: List[str] = []
    group_idx = 0
    need_zero = False
    while num > 0:
        group = num % 10000
        if group == 0:
            need_zero = bool(parts)
        else:
            group_text = ""
            zero_pending = False
            for pos in range(4):
                digit = group % 10
                group //= 10
                if digit == 0:
                    if group_text:
                        zero_pending = True
                    continue
                piece = _DIGITS[str(digit)] + units[pos]
                if zero_pending:
                    group_text = "零" + group_text
                    zero_pending = False
                group_text = piece + group_text
            if group_text.startswith("一十"):
                group_text = group_text[1:]
            if need_zero:
                group_text = "零" + group_text
                need_zero = False
            parts.insert(0, group_text + big_units[group_idx])
        num //= 10000
        group_idx += 1
    return "".join(parts)


def _number_to_spoken_zh(num_text: str) -> str:
    raw = str(num_text or "")
    if "." in raw:
        left, right = raw.split(".", 1)
        left_text = _int_to_zh(int(left or "0"))
        right_text = "".join(_DIGITS.get(ch, ch) for ch in right if ch.isdigit())
        return f"{left_text}點{right_text}" if right_text else left_text
    return _int_to_zh(int(raw or "0"))


def _rewrite_numeric_unit(match: re.Match[str]) -> str:
    raw = match.group(0)
    m = re.match(r"(\d+(?:\.\d+)?)(.*)", raw)
    if not m:
        return raw
    number = m.group(1)
    suffix = m.group(2)
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


def build_repaired_alignment_text(source_text: str) -> Tuple[str, List[Tuple[int, int]]]:
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
        replacement = _rewrite_numeric_unit(match)
        cursor = _append_piece(out, char_map, cursor, original, replacement)
        pos = match.end()
    if pos < len(src):
        chunk = src[pos:]
        cursor = _append_piece(out, char_map, cursor, chunk, chunk)
    return "".join(out), char_map


def make_repair_plan(source_text: str, words: Sequence[Dict[str, Any]]) -> RepairPlan:
    if not repair_enabled():
        return RepairPlan(enabled=False, reason="disabled")
    bad = find_risky_alignment_units(words)
    if not bad:
        return RepairPlan(enabled=False, reason="no_anomaly")
    repaired_text, char_map = build_repaired_alignment_text(source_text)
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
