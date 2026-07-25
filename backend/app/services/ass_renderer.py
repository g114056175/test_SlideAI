import json
import re

GAP_FILL_EPS_SEC = 0.06
TAIL_UNIT_HOLD_MAX_SEC = 0.95
MIN_HIGHLIGHT_SEC = 0.16
MAX_INLINE_GAP_FILL_SEC = 0.75
LARGE_GAP_FILL_FRACTION = 0.92


def _strip_trailing_punctuation(text: str) -> str:
    return re.sub(r"[\s，。！？；：、,.!?;:'\"“”‘’)\]】》」』]+$", "", str(text or "")).strip()


def _looks_like_spoken_unit_token(text: str) -> bool:
    """Return True for tails such as 1180px, 45.2ms, 32GB.

    Forced alignment often compresses these into a short token, while TTS
    pronounces them as multiple spoken pieces ("one thousand ... p x").
    """
    raw = _strip_trailing_punctuation(text)
    if not raw:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?%", raw):
        return True
    if not re.search(r"\d", raw) or not re.search(r"[A-Za-z]", raw):
        return False
    return bool(re.search(r"\d(?:[\d.]*)(?:px|ms|s|gb|mb|kb|tb|fps|hz|khz|mhz|ghz|vram|gpu|cpu|api|ui)$", raw, re.I))


def _min_spoken_unit_duration(text: str) -> float:
    raw = _strip_trailing_punctuation(text)
    if re.fullmatch(r"\d+(?:\.\d+)?%", raw):
        # Percentages like 100% are spoken as multi-syllable units (百分之一百),
        # but aligners often timestamp only the numeric characters.
        return min(1.1, 0.42 + 0.08 * len(raw))
    if _looks_like_spoken_unit_token(raw):
        return min(0.95, 0.24 + 0.07 * len(raw))
    return 0.0


def _has_boundary_pause_punctuation(text: str, *, trailing: bool) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    # Delimiters where holding highlight through the pause looks like a visual
    # stall. Comma/pause punctuation are intentionally included here.
    pause_chars = "，。！？；：、,.!?;:"
    edge = raw[-1] if trailing else raw[0]
    return edge in pause_chars


def _is_highlightable_token(text: str) -> bool:
    return any(piece.get("hg") for piece in tokenize_pieces(str(text or "")))


def _can_fill_inline_gap(cur: dict, nxt: dict) -> bool:
    """Return True when a gap is probably an alignment hole, not a spoken pause."""
    cur_text = str(cur.get("text", ""))
    nxt_text = str(nxt.get("text", ""))
    if not _is_highlightable_token(cur_text) or not _is_highlightable_token(nxt_text):
        return False
    if _has_boundary_pause_punctuation(cur_text, trailing=True):
        return False
    if _has_boundary_pause_punctuation(nxt_text, trailing=False):
        return False
    return True


def _segment_event_end(seg_end: float, next_seg_start: float | None, words: list) -> float:
    """Extend subtitle visibility for final numeric-unit tokens when there is a gap.

    Normal CJK tokens should end at the segment boundary. Numeric+unit tails are
    the exception because ASR/forced alignment frequently ends them too early.
    """
    event_end = float(seg_end)
    if next_seg_start is None or next_seg_start <= event_end:
        return event_end
    gap = float(next_seg_start) - event_end
    if gap < 0.15 or not words:
        return event_end
    tail = str((words[-1] or {}).get("text", ""))
    if not _looks_like_spoken_unit_token(tail):
        return event_end
    raw = _strip_trailing_punctuation(tail)
    extra = min(TAIL_UNIT_HOLD_MAX_SEC, _min_spoken_unit_duration(raw))
    return min(float(next_seg_start) - 0.08, event_end + extra)

def format_ass_time(sec: float) -> str:
    sec = max(0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        cs = 99
        s += 1
        if s == 60:
            s = 0
            m += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def tokenize_pieces(text: str):
    import re
    def is_punc_or_space(s):
        return bool(re.match(r'^[，。！？；：「」『』（）、,.!?;:\'"“”‘’\-—(){}\[\]<>《》…\s]+$', s))
    if re.match(r'^\d+(?:\.\d+)+%?$', text):
        return [{"text": text, "hg": True}]
    if re.search(r'\s', text):
        parts = [p for p in re.split(r'(\s+)', text) if p]
        return [{"text": p, "hg": not is_punc_or_space(p)} for p in parts]
    
    out = []
    ascii_buf = ""
    def flush():
        nonlocal ascii_buf
        if ascii_buf:
            out.append({"text": ascii_buf, "hg": True})
            ascii_buf = ""
    i = 0
    while i < len(text):
        rest = text[i:]
        m = re.match(r'^\d+(?:\.\d+)+%?', rest)
        if m:
            flush()
            out.append({"text": m.group(0), "hg": True})
            i += len(m.group(0))
            continue
        ch = text[i]
        if re.match(r'[A-Za-z0-9]', ch):
            ascii_buf += ch
            i += 1
        else:
            flush()
            out.append({"text": ch, "hg": not is_punc_or_space(ch)})
            i += 1
    flush()
    return out

def smooth_word_timeline(words, seg_start, seg_end, min_word_sec=0.09):
    if not words:
        return []
    n = len(words)
    seg_start = float(seg_start)
    seg_end = float(seg_end)
    seg_dur = max(0.001, seg_end - seg_start)
    
    requested_min = max(0.03, min_word_sec)
    feasible_min = max(0.02, (seg_dur / max(n, 1)) * 0.9)
    min_dur = min(requested_min, feasible_min)
    
    uniform_dur = seg_dur / n
    blend_raw = 0.25
    
    boundaries = [0] * (n + 1)
    boundaries[0] = seg_start
    boundaries[n] = seg_end
    
    for i in range(1, n):
        prev = words[i - 1]
        cur = words[i]
        prev_end = prev.get("end")
        cur_start = cur.get("start")
        if prev_end is not None and cur_start is not None:
            raw_mid = (float(prev_end) + float(cur_start)) / 2
        else:
            raw_mid = seg_start + i * uniform_dur
        uni_mid = seg_start + i * uniform_dur
        boundaries[i] = (blend_raw * raw_mid) + ((1 - blend_raw) * uni_mid)
        
    for i in range(1, n + 1):
        boundaries[i] = max(boundaries[i], boundaries[i - 1] + min_dur)
    boundaries[n] = seg_end
    for i in range(n - 1, -1, -1):
        boundaries[i] = min(boundaries[i], boundaries[i + 1] - min_dur)
        
    boundaries[0] = seg_start
    boundaries[n] = seg_end
    
    out = []
    for i in range(n):
        s = max(seg_start, boundaries[i])
        e = min(seg_end, boundaries[i + 1])
        if e <= s:
            e = min(seg_end, s + max(min_dur * 0.5, 0.02))
        out.append({
            "text": str(words[i].get("text", "")).strip(),
            "start": s,
            "end": e
        })
    return out

def build_qwen_stable_timeline(words, seg_start, seg_end, min_word_sec=0.06, gap_fill_eps=GAP_FILL_EPS_SEC):
    """Qwen 對齊下的穩定時間軸：
    - 不做全域平滑，避免「整句被推遲」
    - 只做最小時長補齊 + 微小 gap fill，避免高亮跳字
    """
    if not words:
        return []
    start = float(seg_start)
    end = float(seg_end)
    seg_dur = max(0.001, end - start)
    n = max(1, len(words))
    feasible_min = max(0.02, (seg_dur / n) * 0.8)
    min_dur = min(max(0.02, min_word_sec), feasible_min)

    out = []
    last_end = start
    for w in words:
        t = str(w.get("text", "")).strip()
        if not t:
            continue
        s = float(w.get("start", last_end))
        e = float(w.get("end", s + min_dur))
        if s < last_end:
            s = last_end
        if e <= s:
            e = s + min_dur
        min_spoken = _min_spoken_unit_duration(t)
        if min_spoken > 0 and e - s < min_spoken:
            e = min(end, s + min_spoken)
        out.append({"text": t, "start": s, "end": e})
        last_end = e

    if not out:
        return out

    # If forced alignment leaves a large gap before a very short token, the
    # token's start is usually too late, not merely too short. Move the next
    # token earlier so the highlight is handed off inside the spoken phrase
    # (e.g. "講稿"), while keeping punctuation/real-pause boundaries intact.
    for i in range(1, len(out)):
        cur = out[i]
        prev = out[i - 1]
        cur_dur = float(cur["end"] - cur["start"])
        gap = float(cur["start"] - prev["end"])
        if cur_dur < MIN_HIGHLIGHT_SEC and gap > 0.04 and _can_fill_inline_gap(prev, cur):
            if gap > 0.12:
                desired_dur = min(0.42, max(MIN_HIGHLIGHT_SEC, cur_dur + min(gap * 0.65, 0.28)))
                desired_start = float(cur["end"]) - desired_dur
                # Keep a tiny handoff gap so adjacent ASS override blocks do not
                # collapse into the previous glyph's visual highlight.
                cur["start"] = max(float(prev["end"]) + 0.02, min(float(cur["start"]), desired_start))
            else:
                borrow = min(gap - 1e-3, MIN_HIGHLIGHT_SEC - cur_dur)
                if borrow > 0:
                    cur["start"] -= borrow

    # Gap fill: inside one spoken segment, non-punctuation token boundaries are
    # usually continuous. Fill probable alignment holes so highlight is stolen
    # by the next word instead of turning off in the middle of a phrase.
    for i in range(len(out) - 1):
        cur = out[i]
        nxt = out[i + 1]
        if cur["end"] >= nxt["start"] or not _can_fill_inline_gap(cur, nxt):
            continue
        gap = float(nxt["start"] - cur["end"])
        if gap <= max(gap_fill_eps, 0.08):
            cur["end"] = max(cur["end"], nxt["start"] - 1e-3)
        elif gap <= MAX_INLINE_GAP_FILL_SEC:
            fill_to = cur["end"] + gap * LARGE_GAP_FILL_FRACTION
            cur["end"] = max(cur["end"], min(nxt["start"] - 1e-3, fill_to))

    # 句尾不額外延長，避免「講完後字幕仍卡著高亮」。
    out[-1]["end"] = min(out[-1]["end"], end)
    return out

def _hex_to_ass_bgr(hex_color: str) -> str:
    raw = str(hex_color or "#000000").strip().replace("#", "")
    if len(raw) == 3:
        raw = "".join([c + c for c in raw])
    raw = (raw + "000000")[:6]
    r = int(raw[0:2], 16)
    g = int(raw[2:4], 16)
    b = int(raw[4:6], 16)
    return f"{b:02X}{g:02X}{r:02X}"


def generate_ass_script(
    width: int,
    height: int,
    segments: list,
    style: str,
    font_size: int,
    alpha: int,
    highlight: bool,
    is_qwen: bool,
    margin_v: int | None = None,
    enable_background: bool = True,
    background_color: str = "#000000",
    text_color: str = "#ffffff",
    highlight_color: str = "#facc15",
    enable_outline: bool = False,
    outline_color: str = "#000000",
    outline_width: int = 2,
) -> str:
    alpha_hex = f"{int(255 - (alpha / 100) * 255):02X}"
    
    base_color = f"&H00{_hex_to_ass_bgr(text_color)}"
    hg_color = f"&H00{_hex_to_ass_bgr(highlight_color)}"
    
    border_style = 4
    outline = max(0, int(outline_width)) if enable_outline else 0
    shadow = max(1, round(font_size * 0.25))
    
    bgr = _hex_to_ass_bgr(background_color)
    outline_color_ass = f"&H00{_hex_to_ass_bgr(outline_color)}"
    if style == 'bg-gray':
        bg_color = f"&H{alpha_hex}{bgr}"
        final_outline_color = outline_color_ass
    elif style == 'stroke-dark':
        border_style = 1 
        outline = max(outline, max(2, int(font_size * 0.08)))
        shadow = 0
        bg_color = "&HFF000000"
        final_outline_color = outline_color_ass
    else:
        bg_color = f"&H{alpha_hex}{bgr}"
        final_outline_color = outline_color_ass
        
    marginV = int(height * 0.04) if margin_v is None else max(12, int(margin_v))
    if (not enable_background) or int(alpha) <= 0:
        shadow = 0
    
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Layer0,Noto Sans CJK TC,{font_size},{base_color},{base_color},{final_outline_color},{bg_color},-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow if enable_background and int(alpha) > 0 else 0},2,20,20,{marginV},1
Style: Layer1,Noto Sans CJK TC,{font_size},{base_color},{base_color},{final_outline_color},&HFF000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,20,20,{marginV},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    
    for seg_idx, seg in enumerate(segments):
        seg_start = float(seg.get("start", 0))
        seg_end = float(seg.get("end", 0))
        seg_text = seg.get("text", "")
        words = seg.get("words", [])
        next_seg_start = None
        if seg_idx + 1 < len(segments):
            try:
                next_seg_start = float(segments[seg_idx + 1].get("start", 0))
            except Exception:
                next_seg_start = None
        
        has_cjk = any("\u4e00" <= c <= "\u9fff" for c in seg_text)
        
        event_end = _segment_event_end(seg_end, next_seg_start, words)
        event_end_ms = int(max(0, (event_end - seg_start) * 1000))

        if not highlight or not words or (has_cjk and not is_qwen):
            events.append(f"Dialogue: 0,{format_ass_time(seg_start)},{format_ass_time(event_end)},Layer0,,0,0,0,,{seg_text}")
            events.append(f"Dialogue: 1,{format_ass_time(seg_start)},{format_ass_time(event_end)},Layer1,,0,0,0,,{seg_text}")
            continue
            
        # 非 Qwen：使用既有平滑
        # Qwen：使用「不改整段起訖」的穩定補齊，避免跳字但不造成整體延後
        token_timeline = build_qwen_stable_timeline(words, seg_start, seg_end) if is_qwen else smooth_word_timeline(words, seg_start, seg_end)
        if not token_timeline:
            events.append(f"Dialogue: 0,{format_ass_time(seg_start)},{format_ass_time(event_end)},Layer0,,0,0,0,,{seg_text}")
            events.append(f"Dialogue: 1,{format_ass_time(seg_start)},{format_ass_time(event_end)},Layer1,,0,0,0,,{seg_text}")
            continue
            
        plain_events = []
        tagged_events = []
        
        for i, token in enumerate(token_timeline):
            t_str = str(token.get('text', ''))
            t1 = int((float(token["start"]) - seg_start) * 1000)
            t2 = int((float(token["end"]) - seg_start) * 1000)
            next_t1 = None
            if i + 1 < len(token_timeline):
                try:
                    next_t1 = int((float(token_timeline[i + 1].get("start", token["end"])) - seg_start) * 1000)
                except Exception:
                    next_t1 = None
            # 高亮採「搶佔式」：
            # - 非最後一字：亮到下一字開始前一刻（不主動熄滅）
            # - 最後一字：只亮到句尾（不跨句延長）
            if next_t1 is not None:
                t2h = max(t2, next_t1 - 1)
            else:
                t2h = max(t2, event_end_ms - 1)
            
            pieces = tokenize_pieces(t_str)
            for p in pieces:
                plain_events.append(p["text"])
                if p["hg"]:
                    tagged_events.append(f"{{\\c{base_color}&\\t({t1},{t1+1},\\c{hg_color}&)\\t({t2h},{t2h+1},\\c{base_color}&)}}{p['text']}")
                else:
                    tagged_events.append(f"{{\\c{base_color}&}}{p['text']}")
            
            if i < len(token_timeline) - 1:
                t1_char = t_str[-1] if t_str else ""
                t2_char = str(token_timeline[i+1].get("text", ""))[0] if str(token_timeline[i+1].get("text", "")) else ""
                
                is_ascii1 = t1_char.isalnum()
                is_ascii2 = t2_char.isalnum()
                def check_cjk(s): return any('\u4e00' <= c <= '\u9fff' for c in s)
                is_cjk1 = check_cjk(t1_char)
                is_cjk2 = check_cjk(t2_char)
                
                if (is_ascii1 and is_ascii2) or (is_cjk1 and is_ascii2) or (is_ascii1 and is_cjk2):
                    plain_events.append(" ")
                    tagged_events.append(f"{{\\c{base_color}&}} ")
                    
        layer0_str = "".join(plain_events)
        layer1_str = "".join(tagged_events)
        
        events.append(f"Dialogue: 0,{format_ass_time(seg_start)},{format_ass_time(event_end)},Layer0,,0,0,0,,{layer0_str}")
        events.append(f"Dialogue: 1,{format_ass_time(seg_start)},{format_ass_time(event_end)},Layer1,,0,0,0,,{{\\c{base_color}&}}{layer1_str}{{\\c{base_color}&}}")

    return header + "\n".join(events) + "\n"
