import difflib
import re
from typing import Any, Dict, List, Tuple

_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:\"'“”‘’()\[\]{}<>《》【】…\-—_`~@#$%^&*+=|\\/]+")
_SPACE_RE = re.compile(r"\s+")


def _clean_display_text(text: str) -> str:
    text = str(text or "")
    text = _SPACE_RE.sub("", text)
    return text.strip()


def _align_clean(text: str) -> str:
    text = str(text or "")
    text = _SPACE_RE.sub("", text)
    text = _PUNCT_RE.sub("", text)
    return text.strip()


def estimate_char_times_from_words(words: List[Dict[str, Any]]) -> Tuple[str, List[Tuple[float, float]], float]:
    chars: List[str] = []
    spans: List[Tuple[float, float]] = []
    audio_end = 0.0
    for w in words:
        t = _clean_display_text(str(w.get("text") or ""))
        if not t:
            audio_end = max(audio_end, float(w.get("end") or 0.0))
            continue
        ws = float(w.get("start") or 0.0)
        we = float(w.get("end") or ws)
        audio_end = max(audio_end, we)
        step = max((we - ws) / max(len(t), 1), 1e-3)
        for i, ch in enumerate(t):
            s = ws + i * step
            e = min(we, s + step)
            chars.append(ch)
            spans.append((s, e))
    return "".join(chars), spans, audio_end


def align_reference_chars(
    ref_text_clean: str,
    asr_text: str,
    asr_spans: List[Tuple[float, float]],
    audio_end: float,
) -> Tuple[List[Tuple[float, float]], float]:
    if not ref_text_clean:
        return [], 1.0
    if not asr_text or not asr_spans:
        dur = max(audio_end, 0.1)
        step = dur / max(len(ref_text_clean), 1)
        return [(i * step, min(dur, (i + 1) * step)) for i in range(len(ref_text_clean))], 0.0

    # Convert both to simplified chinese and lowercase for robust fuzzy matching
    try:
        from opencc import OpenCC
        t2s = OpenCC("t2s")
        ref_match = t2s.convert(ref_text_clean).lower()
        asr_match = t2s.convert(asr_text).lower()
    except Exception:
        ref_match = ref_text_clean.lower()
        asr_match = asr_text.lower()

    mapped: List[Tuple[float, float] | None] = [None] * len(ref_text_clean)
    matcher = difflib.SequenceMatcher(None, ref_match, asr_match)
    ratio = matcher.ratio()
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        n = min(i2 - i1, j2 - j1)
        for k in range(n):
            mapped[i1 + k] = asr_spans[j1 + k]

    # Interpolate unmatched chars.
    valid = [(i, v) for i, v in enumerate(mapped) if v is not None]
    if not valid:
        dur = max(audio_end, 0.1)
        step = dur / max(len(ref_text_clean), 1)
        return [(i * step, min(dur, (i + 1) * step)) for i in range(len(ref_text_clean))]

    # Fill front
    first_idx, first_span = valid[0]
    for i in range(0, first_idx):
        mapped[i] = (max(0.0, first_span[0] - (first_idx - i) * 0.04), first_span[0])
    # Fill middle gaps
    for (li, lv), (ri, rv) in zip(valid, valid[1:]):
        gap = ri - li
        if gap <= 1:
            continue
        left_t = lv[1]
        right_t = rv[0]
        span = max(right_t - left_t, 1e-3)
        for i in range(li + 1, ri):
            p = (i - li) / gap
            s = left_t + span * p
            e = s + max(span / gap * 0.9, 1e-3)
            mapped[i] = (s, min(right_t, e))
    # Fill tail
    last_idx, last_span = valid[-1]
    for i in range(last_idx + 1, len(mapped)):
        s = last_span[1] + (i - last_idx - 1) * 0.04
        e = s + 0.04
        mapped[i] = (s, min(max(audio_end, e), e))

    out = [(float(s), float(e)) for (s, e) in mapped if s is not None and e is not None]
    if len(out) != len(ref_text_clean):
        dur = max(audio_end, 0.1)
        step = dur / max(len(ref_text_clean), 1)
        return [(i * step, min(dur, (i + 1) * step)) for i in range(len(ref_text_clean))], ratio
    return out, ratio


def expand_units_to_char_spans(units: List[Dict[str, Any]]) -> Tuple[str, List[Tuple[float, float]]]:
    """Expand per-word aligner output into per-character (speech chars only) spans.
    Uses _align_clean so punctuation is excluded — the forced aligner does not
    produce timestamps for punctuation, so they must not appear in the span array.
    This keeps asr_clean_text and asr_char_spans consistent with the cursor
    arithmetic in align_subtitles_from_audio_and_text which also uses _align_clean.
    """
    clean_chars: List[str] = []
    char_spans: List[Tuple[float, float]] = []
    for unit in units:
        unit_text = str(unit.get("text") or "")
        unit_clean = _align_clean(unit_text)  # strip punctuation — no aligner stamp for it
        if not unit_clean:
            continue
        start = float(unit.get("start") or 0.0)
        end = float(unit.get("end") or start)
        if end <= start:
            end = start + 1e-3
        step = (end - start) / max(len(unit_clean), 1)
        for idx, ch in enumerate(unit_clean):
            clean_chars.append(ch)
            span_start = start + step * idx
            span_end = end if idx == len(unit_clean) - 1 else start + step * (idx + 1)
            if span_end <= span_start:
                span_end = span_start + 1e-3
            char_spans.append((float(span_start), float(span_end)))
    return "".join(clean_chars), char_spans
