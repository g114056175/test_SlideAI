import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:\"'“”‘’()\[\]{}<>《》【】…\-—_`~@#$%^&*+=|\\/]+")
_SPACE_RE = re.compile(r"\s+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-']*")
_ASCII_COMPOSITE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+|[A-Za-z0-9]+")
_PUNCT_ONLY_RE = re.compile(r"^[，。！？；：、,.!?;:\"'“”‘’()\[\]{}<>《》【】…\-—_`~@#$%^&*+=|\\/]+$")
_SPLIT_CONNECTORS = set("的了在與和及或來去把將對讓給並且而但且則也再又並")
_CJK_SUFFIX_PROTECTED = {
    "架構", "模型", "方法", "系統", "模組", "版本", "參數", "資料", "資料集",
    "論文", "paper", "機制", "框架", "編碼器", "解碼器", "損失", "函數", "特徵",
    "表示", "能力", "層", "策略", "流程", "結果", "實驗", "訓練", "推論",
}
_CJK_PREFIX_LIGHT = {"本篇", "這篇", "該篇", "用", "採用", "透過", "使用", "把", "將", "以", "對"}
_CJK_COMMON_WORDS = tuple(sorted({
    "本篇", "這篇", "該篇", "說到", "採用", "架構", "模型", "方法", "加入", "新的", "改善", "結果",
    "對齊", "輸出", "時間戳", "重新", "切成", "適合", "字幕", "閱讀", "短句", "摘要", "生成",
    "同時", "保留", "中文", "術語", "英文", "專有名詞", "提升", "推論", "能力", "使用", "進行",
    "研究", "資料集", "編碼器", "解碼器", "機制", "策略", "流程", "實驗", "論文", "系統",
    "參數", "特徵", "表示", "同步", "目前", "朗讀", "保護", "切分", "切句", "顯示", "畫面",
    "安全區", "底部", "置中", "固定字級", "單行", "雙行", "可讀性", "完整", "短語", "片語",
}, key=len, reverse=True))
_JP_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_OPENCC_T2S = None
_OPENCC_S2T = None
_OPENCC_IMPORT_FAILED = False
_JIEBA = None
_JIEBA_IMPORT_FAILED = False
_EN_ORPHAN_END = {
    "and", "or", "that", "with", "for", "to", "of", "in", "on", "at",
    "by", "from", "if", "when", "while", "because", "so", "than", "as",
}
_EN_PREFERRED_BREAK_BEFORE = {
    "in", "on", "at", "by", "from", "with", "without",
    "after", "before", "when", "while", "because",
    "although", "whereas", "but", "which",
}
_EN_DISCOURSE_MARKERS = {
    "however", "therefore", "furthermore", "moreover", "meanwhile",
    "consequently", "thus", "instead", "otherwise", "specifically",
    "notably", "additionally",
}
_ZH_ORPHAN_END = {
    "在", "在不同", "在高", "以及", "而且", "並且", "如果", "因為", "所以",
    "也就是說", "或是", "或者", "與", "和", "及",
}
_ZH_FORBIDDEN_LINE_END = {
    "的", "了", "以", "在", "與", "或", "比", "單一", "以及", "並且", "而且", "若", "如果",
}
_EN_TECH_BIGRAMS = {
    ("kernel", "fusion"),
    ("remote", "procedure"),
    ("procedure", "calls"),
    ("batch", "size"),
    ("gpu", "memory"),
    ("qwen", "tts"),
    ("tts", "worker"),
    ("state", "space"),
    ("space", "model"),
    ("convolutional", "neural"),
    ("neural", "network"),
    ("long", "short-term"),
    ("short-term", "memory"),
}
_ZH_TECH_BIGRAMS = {
    ("提示", "詞範本"),
    ("提示詞", "範本"),
    ("工作", "流"),
    ("密集向量", "檢索"),
    ("高", "併發"),
    ("多", "進程"),
    ("多", "程序"),
}
_ZH_TECH_SUFFIX = {"範本", "詞範本", "工作流", "架構", "檢索", "策略", "框架", "機制", "能力", "模型"}
_ZH_TECH_PREFIX_HINT = {"密集向量", "提示詞", "提示", "多模態", "分散式", "高可用", "高併發"}
_OPEN_QUOTES = {"「", "『", "“", "‘", "(", "（", "[", "【", "{", "《", "<"}
_CLOSE_QUOTES = {"」", "』", "”", "’", ")", "）", "]", "】", "}", "》", ">"}
_PAIR_OPEN_TO_CLOSE = {"(": ")", "（": "）", "[": "]", "【": "】", "{": "}", "《": "》", "<": ">"}
_STRONG_PUNCT = set("。！？!?；;\n")
# Weak punctuation is considered a preferred subtitle boundary when a line
# grows too long.  Keep enumeration comma "、" out of this set: short list items
# such as "模型、方法、" should stay eligible to merge with following text.
_WEAK_PUNCT = set("，,:：…")
_WEAK_TAIL_ROLLBACK = {"將", "與", "在", "以", "並", "而", "及"}
_NON_TERMINAL_DOT_ABBREVIATIONS = {
    "dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st",
    "fig", "eq", "no", "dept", "inc", "vs",
}
_CONTINUATION_DOTTED_ABBREVIATIONS = {"e.g", "i.e"}
_TIME_DOTTED_ABBREVIATIONS = {"a.m", "p.m"}
_NO_SPACE_ASCII_UNITS = {
    "b", "kb", "mb", "gb", "tb", "pb",
    "hz", "khz", "mhz", "ghz",
    "ms", "px", "fps", "dpi",
}


def _has_cjk(text: Any) -> bool:
    if not text:
        return False
    return bool(_CJK_RE.search(str(text)))



def _clean_display_text(text: str) -> str:
    # Do NOT strip punctuation so it displays
    text = str(text or "")
    text = _SPACE_RE.sub("", text)
    return text.strip()


def _align_clean(text: str) -> str:
    """Strip spaces AND punctuation for alignment cursor tracking.
    Used when we need to count only phonetically-relevant characters
    (i.e. characters that Qwen's ForcedAligner actually produces timestamps for).
    Punctuation like ，。！？ has no timestamps from the aligner, so it must
    NOT be counted when we map display chunks onto the char-span array.
    """
    text = str(text or "")
    text = _SPACE_RE.sub("", text)
    text = _PUNCT_RE.sub("", text)
    return text.strip()


def _is_cjk_char(ch: str) -> bool:
    return bool(ch) and ("\u4e00" <= ch <= "\u9fff")


def _is_ascii_word(tok: str) -> bool:
    return bool(tok) and bool(_ASCII_WORD_RE.fullmatch(tok))


def _is_all_cjk(tok: str) -> bool:
    return bool(tok) and all(_is_cjk_char(ch) for ch in tok)


def _is_short_cjk_token(tok: str) -> bool:
    return _is_all_cjk(tok) and len(tok) <= 2


def _is_punct_only(tok: str) -> bool:
    return bool(tok) and bool(_PUNCT_ONLY_RE.fullmatch(tok.strip()))


def _is_ascii_token(tok: str) -> bool:
    return bool(tok) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", tok))


def _strip_edge_punct(tok: str) -> str:
    return re.sub(r"^[，,。.!！？?；;：:、（）()\[\]{}《》【】\s]+|[，,。.!！？?；;：:、（）()\[\]{}《》【】\s]+$", "", str(tok or ""))


def _last_ascii_word(tok: str) -> str:
    s = _strip_edge_punct(tok)
    m = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", s)
    return m[-1].lower() if m else ""


def _first_ascii_word(tok: str) -> str:
    s = _strip_edge_punct(tok)
    m = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", s)
    return m[0].lower() if m else ""


def _split_ascii_core_and_tail(tok: str) -> Tuple[str, str]:
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)([，,。.!！？?；;：:、]*)$", str(tok or ""))
    if not m:
        return "", str(tok or "")
    return m.group(1), m.group(2)


def _merge_tech_compounds(tokens: List[str]) -> List[str]:
    """
    Merge technical compounds after base tokenization:
    - [X] + [suffix]  => X+suffix  (e.g. 提示詞 + 範本)
    - [dense vector-like] + [檢索] => 密集向量檢索
    - [english phrase] + [中文技術後綴] => "..."+後綴
    """
    if not tokens:
        return tokens
    out: List[str] = []
    i = 0
    while i < len(tokens):
        cur = str(tokens[i] or "")
        # 3-gram Chinese technical compound merge.
        if i + 2 < len(tokens):
            nxt1 = str(tokens[i + 1] or "")
            nxt2 = str(tokens[i + 2] or "")
            c0 = _strip_edge_punct(cur)
            c1 = _strip_edge_punct(nxt1)
            c2 = _strip_edge_punct(nxt2)

            if c0 == "提示" and c1 == "詞範" and c2 == "本":
                out.append(cur + c1 + c2)
                i += 3
                continue
            if c0 == "密集" and c1 == "向量" and c2.startswith("檢索"):
                out.append(cur + c1 + c2)
                i += 3
                continue
            if c0 == "工作" and c1.startswith("流"):
                out.append(cur + c1)
                i += 2
                continue

        if i + 1 < len(tokens):
            nxt = str(tokens[i + 1] or "")
            cur_core = _strip_edge_punct(cur)
            nxt_core = _strip_edge_punct(nxt)

            # Highest-priority direct bigram protection:
            # keep known technical compounds as one token.
            if cur_core and nxt_core and (cur_core, nxt_core) in _ZH_TECH_BIGRAMS:
                out.append(cur + nxt_core)
                i += 2
                continue

            if nxt_core in _ZH_TECH_SUFFIX and cur_core:
                if cur_core in _ZH_TECH_PREFIX_HINT or _is_ascii_token(cur_core.split()[-1]) or _is_all_cjk(cur_core):
                    out.append(cur + nxt_core)
                    i += 2
                    continue

            if nxt_core.startswith("檢索") and (cur_core.endswith("向量") or cur_core.endswith("稠密向量") or cur_core.endswith("密集向量")):
                out.append(cur + nxt_core)
                i += 2
                continue

            # Generic CJK technical chain merge:
            # e.g. 密集 + 向量檢索 => 密集向量檢索
            #      提示 + 詞範本   => 提示詞範本
            if _is_all_cjk(cur_core) and _is_all_cjk(nxt_core):
                if (
                    cur_core in {"密集", "稠密", "提示", "提示詞", "工作", "向量", "技術", "模型"}
                    and (
                        nxt_core.startswith("向量")
                        or nxt_core.startswith("詞範本")
                        or nxt_core.startswith("範本")
                        or nxt_core.startswith("工作流")
                        or nxt_core.startswith("檢索")
                    )
                ):
                    out.append(cur + nxt_core)
                    i += 2
                    continue

        out.append(cur)
        i += 1
    return out


def _merge_atomic_spans(tokens: List[str]) -> List[str]:
    """Merge token stream to keep backtick and bracket spans atomic."""
    if not tokens:
        return tokens
    out: List[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = str(tokens[i] or "")
        core = _strip_edge_punct(tok)

        # placeholder span (for protected code fragments)
        if "\ue000" in tok and "\ue001" not in tok:
            j = i + 1
            buf = [tok]
            while j < n and "\ue001" not in str(tokens[j] or ""):
                buf.append(str(tokens[j] or ""))
                j += 1
            if j < n:
                buf.append(str(tokens[j] or ""))
                j += 1
            out.append("".join(buf))
            i = j
            continue

        # backtick span
        if tok.count("`") % 2 == 1:
            j = i + 1
            buf = [tok]
            tick = tok.count("`")
            while j < n and tick % 2 == 1:
                buf.append(str(tokens[j] or ""))
                tick += str(tokens[j] or "").count("`")
                j += 1
            out.append("".join(buf).strip())
            i = j
            continue

        # bracket span
        first = core[:1] if core else ""
        if first in _PAIR_OPEN_TO_CLOSE:
            close_ch = _PAIR_OPEN_TO_CLOSE[first]
            depth = 0
            j = i
            buf: List[str] = []
            while j < n:
                t = str(tokens[j] or "")
                buf.append(t)
                c = _strip_edge_punct(t)
                for ch in c:
                    if ch == first:
                        depth += 1
                    elif ch == close_ch and depth > 0:
                        depth -= 1
                j += 1
                if depth == 0 and any(close_ch in _strip_edge_punct(x) for x in buf):
                    break
            if len(buf) > 1:
                out.append("".join(buf))
                i = j
                continue

        out.append(tok)
        i += 1
    return out


def _merge_cjk_singletons(tokens: List[str]) -> List[str]:
    """Merge adjacent single-char CJK tokens into word-level units.
    This is a post-jieba safety layer for cases like 進/行, 映像/檔.
    """
    if not tokens:
        return tokens
    out: List[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        cur = str(tokens[i] or "")
        cur_core = _strip_edge_punct(cur)
        cur_suffix = cur[len(cur_core):] if cur.startswith(cur_core) else ""
        if len(cur_core) == 1 and _is_all_cjk(cur_core) and cur_core not in _SPLIT_CONNECTORS:
            # If the singleton already owns punctuation, keep that boundary.
            # Merging "好，" + "我們" into "好我們，" rewrites the sentence.
            if cur_suffix:
                out.append(cur)
                i += 1
                continue
            # try merge with next CJK token first (word-level guard)
            if i + 1 < n:
                nxt = str(tokens[i + 1] or "")
                nxt_core = _strip_edge_punct(nxt)
                nxt_suffix = nxt[len(nxt_core):] if nxt.startswith(nxt_core) else ""
                if _is_all_cjk(nxt_core) and nxt_core not in _SPLIT_CONNECTORS:
                    # preserve punctuation suffix from BOTH tokens.
                    out.append(cur_core + nxt_core + nxt_suffix + cur_suffix)
                    i += 2
                    continue
            # fallback: attach singleton suffix to previous CJK token
            if out:
                prev = out[-1]
                prev_core = _strip_edge_punct(prev)
                if prev_core and _is_all_cjk(prev_core):
                    out[-1] = prev + cur_core + cur_suffix
                    i += 1
                    continue
        out.append(cur)
        i += 1
    return out


def _strip_terminal_sentence_punct(text: str) -> str:
    """
    Strip sentence-ending punctuation at chunk end for final display,
    while preserving non-sentence dot usages:
    - ellipsis: "...", "……"
    - dotted abbreviations/acronyms like "U.S."
    """
    s = str(text or "").rstrip()
    if not s:
        return s

    # Keep ellipsis endings.
    if s.endswith("...") or s.endswith("……"):
        return s

    # Strip common terminal punctuation except '.'
    while s and s[-1] in {"，", ",", "。", "！", "!", "？", "?", "；", ";", "：", ":"}:
        s = s[:-1].rstrip()
    if not s:
        return s

    # Handle terminal period carefully.
    if s.endswith("."):
        # Keep dotted abbreviations/acronyms: U.S. / e.g. / i.e. / etc.
        if re.search(r"(?:\b[A-Za-z]\.){2,}$", s):
            return s
        if re.search(r"\b(?:e\.g|i\.e|etc)\.$", s, re.IGNORECASE):
            return s
        # Keep if previous char is digit and looks like decimal/version tail.
        if re.search(r"\d\.$", s):
            return s
        s = s[:-1].rstrip()

    return s


def _tokens_clean_len(tokens: List[str]) -> int:
    return sum(len(_clean_display_text(tok)) for tok in tokens)


def _display_text_units(text: str) -> float:
    total = 0.0
    for ch in str(text or ""):
        if ch.isspace():
            total += 0.2
        elif _is_cjk_char(ch):
            total += 1.0
        elif ch.isascii() and ch.isalnum():
            total += 0.6
        elif ch.isascii():
            total += 0.25
        else:
            total += 0.7
    return total


def _subtitle_split_units(text: str) -> float:
    total = 0.0
    for ch in str(text or ""):
        if ch.isspace():
            continue
        if _is_cjk_char(ch):
            total += 1.0
        elif ch.isascii() and ch.isalpha():
            total += 0.6
        elif ch.isascii() and ch.isdigit():
            total += 0.6
        elif ch.isascii():
            total += 0.25
        else:
            total += 0.5
    return total


def _tokens_display_len(tokens: List[str]) -> float:
    return _display_text_units(_join_tokens(tokens))


def _get_jieba():
    global _JIEBA, _JIEBA_IMPORT_FAILED
    if _JIEBA_IMPORT_FAILED:
        return None
    if _JIEBA is not None:
        return _JIEBA
    try:
        import jieba  # type: ignore
        _JIEBA = jieba
        return _JIEBA
    except Exception:
        # Try loading from project backend venv site-packages when caller
        # does not run inside that venv.
        try:
            here = Path(__file__).resolve()
            backend_root = here.parents[2]
            venv_lib = backend_root / ".venv" / "lib"
            if venv_lib.exists():
                for p in sorted(venv_lib.glob("python*/site-packages")):
                    sp = str(p)
                    if sp not in sys.path:
                        sys.path.insert(0, sp)
                import jieba  # type: ignore
                _JIEBA = jieba
                return _JIEBA
        except Exception:
            pass
        _JIEBA_IMPORT_FAILED = True
        return None


def _merge_ascii_runs(tokens: List[str]) -> List[str]:
    if not tokens:
        return []
    out: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not _is_ascii_word(tok):
            out.append(tok)
            i += 1
            continue
        run = [tok]
        j = i + 1
        while j < len(tokens) and _is_ascii_word(tokens[j]):
            run.append(tokens[j])
            j += 1
        out.append(" ".join(run))
        i = j
    return out


def _ascii_word_tokens(text: str) -> List[str]:
    return [m.group(0) for m in _ASCII_WORD_RE.finditer(str(text or ""))]


def _ascii_tokens_preserve_composite(text: str) -> List[str]:
    return [m.group(0) for m in _ASCII_COMPOSITE_TOKEN_RE.finditer(str(text or ""))]


def _tokenize_cjk_run_fallback(run: str) -> List[str]:
    src = str(run or "")
    if len(src) <= 12:
        return [src] if src else []
    out: List[str] = []
    i = 0
    while i < len(src):
        matched = ""
        for word in _CJK_COMMON_WORDS:
            if src.startswith(word, i):
                matched = word
                break
        if matched:
            out.append(matched)
            i += len(matched)
            continue
        remain = len(src) - i
        if remain <= 3:
            if out:
                out[-1] += src[i:]
            else:
                out.append(src[i:])
            break
        out.append(src[i:i + 2])
        i += 2
    return out


def _tokenize_ascii_readable(text: str) -> List[str]:
    tokens: List[str] = []
    pattern = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*(?:[，,。.!！？?；;：:]*)|[^\s]")
    for m in pattern.finditer(str(text or "")):
        tok = m.group(0)
        if _is_punct_only(tok) and tokens:
            tokens[-1] = f"{tokens[-1]}{tok}"
        elif tok.strip():
            tokens.append(tok)
    return tokens


def _tokenize_clause(text: str) -> List[str]:
    tokens: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Keep markdown/code spans as atomic tokens.
        if ch == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 1
            if j < n:
                tokens.append(text[i:j + 1])
                i = j + 1
                continue
        # Keep bracketed content as atomic token to avoid mid-pair split.
        if ch in _PAIR_OPEN_TO_CLOSE:
            close_ch = _PAIR_OPEN_TO_CLOSE[ch]
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if text[j] == ch:
                    depth += 1
                elif text[j] == close_ch:
                    depth -= 1
                j += 1
            if depth == 0:
                tokens.append(text[i:j])
                i = j
                continue
        if ch.isspace():
            i += 1
            continue
        if ch.isascii() and ch.isalnum():
            m = _ASCII_COMPOSITE_TOKEN_RE.match(text, i)
            if m:
                tokens.append(m.group(0))
                i = m.end()
                continue
        if _is_cjk_char(ch):
            j = i
            while j < n and _is_cjk_char(text[j]):
                j += 1
            run = text[i:j]
            # Word-level fallback: if jieba is unavailable, keep contiguous CJK
            # run as one token and avoid splitting inside unknown words.
            tokens.extend(_tokenize_cjk_run_fallback(run))
            i = j
            continue
        # Keep punctuation/symbols and bind to previous token when possible,
        # so we do not drop symbols from source text.
        if ch in {'\uff0c', '，', '。', '！', '？', '；', '：', ',', '.', '!', '?', ';', ':', '、', '「', '」', '『', '』', '“', '”', '‘', '’', '（', '）', '(', ')', '[', ']', '【', '】', '{', '}', '《', '》', '<', '>'}:
            if tokens:
                tokens[-1] = f"{tokens[-1]}{ch}"
            else:
                tokens.append(ch)
            i += 1
            continue
        # Keep unknown symbols too (preserve source as much as possible).
        if tokens:
            tokens[-1] = f"{tokens[-1]}{ch}"
        else:
            tokens.append(ch)
        i += 1
    return _merge_ascii_runs(tokens)


def _tokenize_clause_with_jieba(text: str) -> List[str]:
    src = str(text or "").strip()
    if not src:
        return []
    if not _has_cjk(src):
        return _ascii_word_tokens(src)

    # Protect inline code spans before jieba to avoid corruption.
    code_map: Dict[str, str] = {}
    def _code_repl(m: re.Match) -> str:
        key = f"\ue000{len(code_map)}\ue001"
        code_map[key] = m.group(0)
        return key
    src_masked = re.sub(r"`[^`]+`", _code_repl, src)

    jieba = _get_jieba()
    if jieba is None:
        toks = _merge_tech_compounds(_tokenize_clause(src_masked))
        return [code_map.get(t, t) for t in toks]

    rough = [seg.strip() for seg in jieba.lcut(src_masked, cut_all=False) if str(seg or "").strip()]
    tokens: List[str] = []
    buffer_ascii: List[str] = []

    def flush_ascii():
        nonlocal buffer_ascii
        if buffer_ascii:
            tokens.append(" ".join(buffer_ascii))
            buffer_ascii = []

    for seg in rough:
        if _is_punct_only(seg):
            flush_ascii()
            # Preserve punctuation clusters instead of dropping them.
            if tokens:
                tokens[-1] = f"{tokens[-1]}{seg}"
            else:
                tokens.append(seg)
            continue
        if _is_ascii_word(seg):
            buffer_ascii.append(seg)
            continue
        flush_ascii()
        if len(seg) == 1 and _is_cjk_char(seg):
            tokens.append(seg)
            continue
        if all(_is_cjk_char(ch) for ch in seg):
            tokens.append(seg)
            continue
        if _ASCII_WORD_RE.search(seg):
            # Preserve punctuation in mixed segments; do not drop commas/dots.
            inner = _tokenize_clause(seg)
            tokens.extend(inner)
            continue
        tokens.append(seg)

    flush_ascii()
    if not tokens:
        return _tokenize_clause(src)

    # Merge connector-split technical tokens:
    # "encoder-" + "decoder" -> "encoder-decoder"
    # "qwen_3." + "5_coder" -> "qwen_3.5_coder"
    merged: List[str] = []
    i = 0
    while i < len(tokens):
        cur = str(tokens[i] or "")
        if i + 1 < len(tokens):
            nxt = str(tokens[i + 1] or "")
            nxt_core, nxt_tail = _split_ascii_core_and_tail(nxt)
            if cur and nxt_core and cur[-1] in {"-", "_", "."} and _is_ascii_token(cur[:-1] or "A") and _is_ascii_token(nxt_core):
                merged.append(cur + nxt_core + nxt_tail)
                i += 2
                continue
        merged.append(cur)
        i += 1
    merged = _merge_tech_compounds(merged)
    merged = _merge_cjk_singletons(merged)
    merged = _merge_atomic_spans(merged)
    if code_map:
        restored: List[str] = []
        for t in merged:
            s = str(t)
            for k, v in code_map.items():
                s = s.replace(k, v)
            restored.append(s)
        merged = restored
    return merged


def _join_tokens(tokens: List[str]) -> str:
    out: List[str] = []
    for tok in tokens:
        if not tok: continue
        if out:
            prev = out[-1]
            if prev.strip() and tok.strip():
                p_last = prev[-1]
                t_first = tok[0]
                p_ascii = p_last.isascii() and p_last.isalnum()
                t_ascii = t_first.isascii() and t_first.isalnum()
                p_cjk = _is_cjk_char(p_last)
                t_cjk = _is_cjk_char(t_first)
                if p_cjk and t_ascii:
                    out.append(" ")
                elif p_ascii and t_cjk:
                    out.append(" ")
                elif p_ascii and t_ascii:
                    # Versions and identifiers are already atomic tokens.
                    # Reinsert ordinary English spacing, including Qwen3 +
                    # ForcedAligner and v2.1.0 + and.  Numeric-unit forms such
                    # as 1.23GB remain compact.
                    next_core = _strip_edge_punct(tok).lower()
                    compact_numeric_unit = p_last.isdigit() and next_core in _NO_SPACE_ASCII_UNITS
                    if not compact_numeric_unit:
                        out.append(" ")
                elif (
                    t_ascii
                    and p_last in ".,;:!?"
                    and not (
                        p_last in ",:"
                        and len(prev) >= 2
                        and prev[-2].isdigit()
                        and t_first.isdigit()
                    )
                ):
                    # Preserve normal spacing after English punctuation while
                    # keeping 1,000 and 10:30 compact.
                    out.append(" ")
        out.append(tok)
    return "".join(out).strip()


def _smart_concat(left: str, right: str) -> str:
    a = str(left or "").rstrip()
    b = str(right or "").lstrip()
    if not a:
        return b
    if not b:
        return a
    la = a[-1]
    rb = b[0]
    la_word = la.isascii() and la.isalnum()
    rb_word = rb.isascii() and rb.isalnum()
    if la in ".!?" and rb.isalpha():
        return f"{a} {b}"
    if la == "," and rb.isalpha():
        return f"{a} {b}"
    if la_word and rb_word:
        return f"{a} {b}"
    return f"{a}{b}"


def _repair_cjk_tail_char(lines: List[str]) -> List[str]:
    """Final safety pass:
    if a line ends with a single trailing CJK char and next line starts with CJK,
    move that trailing char to next line to avoid broken words like 全/面遷移.
    """
    if len(lines) < 2:
        return lines
    out = [str(x or "") for x in lines]
    punct_tail = ("，", ",", "。", ".", "！", "!", "？", "?", "；", ";", "：", ":", "、")
    for i in range(len(out) - 1):
        cur = out[i].rstrip()
        nxt = out[i + 1].lstrip()
        if not cur or not nxt:
            continue
        m = re.search(r"^(.*?)([\u4e00-\u9fff])$", cur)
        if not m:
            continue
        head = m.group(1).rstrip()
        tail = m.group(2)
        if not head:
            continue
        if head.endswith(punct_tail):
            continue
        if not _is_cjk_char(nxt[0]):
            continue
        # Keep this conservative: avoid moving if current line is already very short.
        if len(_align_clean(cur)) < 8:
            continue
        out[i] = head
        out[i + 1] = f"{tail}{nxt}"
    return [x for x in out if x.strip()]


def _repair_obvious_cjk_word_break(lines: List[str]) -> List[str]:
    """Hard quality gate for obvious CJK word breaks.
    If line i ends with a single trailing CJK char and line i+1 starts with CJK,
    move that char to next line (e.g., 進/行 -> 進行).
    """
    if len(lines) < 2:
        return lines
    out = [str(x or "") for x in lines]
    punct_tail = ("，", ",", "。", ".", "！", "!", "？", "?", "；", ";", "：", ":", "、")
    for i in range(len(out) - 1):
        cur = out[i].rstrip()
        nxt = out[i + 1].lstrip()
        if not cur or not nxt:
            continue
        if not _is_cjk_char(nxt[0]):
            continue
        m = re.search(r"^(.*?)([\u4e00-\u9fff])$", cur)
        if not m:
            continue
        head = m.group(1).rstrip()
        tail = m.group(2)
        if not head:
            continue
        if head.endswith(punct_tail):
            continue
        # only when tail is a singleton run at line end
        m_run = re.search(r"([\u4e00-\u9fff]+)$", cur)
        if not m_run or len(m_run.group(1)) != 1:
            continue
        out[i] = head
        out[i + 1] = f"{tail}{nxt}"
    return [x for x in out if x.strip()]


def _repair_short_orphan_lines(lines: List[str], min_chars: int, max_chars: int) -> List[str]:
    if len(lines) < 2:
        return lines
    out: List[str] = []
    i = 0
    min_units = max(8.0, float(min_chars) * 0.6)
    short_line_limit = max(min_units, float(min_chars) + 1.5)
    while i < len(lines):
        cur = str(lines[i] or "").strip()
        if not cur:
            i += 1
            continue
        if i + 1 < len(lines):
            nxt = str(lines[i + 1] or "").strip()
            if nxt:
                # Do not merge across strong sentence endings.
                if cur.endswith(("。", "！", "？", ".", "!", "?")):
                    out.append(cur)
                    i += 1
                    continue
                # Do not merge weak-punctuation-ended chunks; fix cutpoint upstream instead.
                if cur.endswith(("，", ",", "；", ";", "：", ":", "、")):
                    out.append(cur)
                    i += 1
                    continue
                cu = _subtitle_split_units(cur)
                merged = _smart_concat(cur, nxt)
                mu = _subtitle_split_units(merged)
                if cu < short_line_limit and mu <= (max_chars + 4.5):
                    out.append(merged)
                    i += 2
                    continue
        out.append(cur)
        i += 1
    return out


def _rollback_weak_tail(lines: List[str]) -> List[str]:
    if len(lines) < 2:
        return lines
    out = [str(x or "") for x in lines]
    punct_tail = ("，", ",", "。", ".", "！", "!", "？", "?", "；", ";", "：", ":", "、")
    for i in range(len(out) - 1):
        cur = out[i].rstrip()
        nxt = out[i + 1].lstrip()
        if not cur or not nxt:
            continue
        if cur.endswith(punct_tail):
            continue
        m = re.search(r"^(.*?)(將|與|在|以|並|而|及)$", cur)
        if not m:
            continue
        head = m.group(1).rstrip()
        tail = m.group(2)
        if not head:
            continue
        out[i] = head
        out[i + 1] = f"{tail}{nxt}"
    return [x for x in out if x.strip()]


def _rebalance_short_neighbor_lines(lines: List[str], min_chars: int, max_chars: int) -> List[str]:
    if len(lines) < 2:
        return lines
    out = [str(x or "").strip() for x in lines if str(x or "").strip()]
    min_units = max(8.0, float(min_chars) * 0.6)

    i = 0
    while i < len(out) - 1:
        cur = out[i]
        nxt = out[i + 1]
        if _subtitle_split_units(cur) < min_chars and _subtitle_split_units(nxt) > min_units:
            nxt_tokens = _tokenize_word_level(nxt)
            base_cur = cur
            moved: List[str] = []
            while nxt_tokens and _subtitle_split_units(cur) < min_chars:
                trial_moved = moved + [nxt_tokens[0]]
                # Rebuild from the original line.  Building on the already
                # updated `cur` duplicates tokens moved in earlier iterations.
                trial_cur = _smart_concat(base_cur, _join_tokens(trial_moved))
                if _subtitle_split_units(trial_cur) > max_chars:
                    break
                moved = trial_moved
                nxt_tokens = nxt_tokens[1:]
                cur = trial_cur
            if moved and nxt_tokens:
                out[i] = cur
                out[i + 1] = _join_tokens(nxt_tokens)
        i += 1

    i = 0
    while i < len(out) - 1:
        cur = out[i]
        nxt = out[i + 1]
        if _subtitle_split_units(nxt) < min_units:
            merged = _smart_concat(cur, nxt)
            if _subtitle_split_units(merged) <= max_chars + 3:
                out[i] = merged
                del out[i + 1]
                continue
        i += 1
    return out


def _repair_unwanted_line_boundaries(lines: List[str], min_chars: int, max_chars: int) -> List[str]:
    """Fix visually awkward but common line-boundary artifacts.

    This pass is intentionally conservative: it only adjusts boundaries that are
    clearly bad for subtitle readability, such as an enumeration comma at line
    end or a protected English phrase split across two lines.
    """
    if len(lines) < 2:
        return lines

    out = [str(x or "").strip() for x in lines if str(x or "").strip()]
    soft_max = max_chars + 3.0
    i = 0
    while i < len(out) - 1:
        cur = out[i].strip()
        nxt = out[i + 1].strip()
        if not cur or not nxt:
            i += 1
            continue

        # Do not leave enumeration commas dangling when the next word can fit.
        if cur.endswith("、"):
            nxt_tokens = _tokenize_word_level(nxt)
            moved_tokens: List[str] = []
            adjusted = False
            while nxt_tokens:
                moved_tokens.append(nxt_tokens.pop(0))
                moved = _join_tokens(moved_tokens)
                trial_cur = _smart_concat(cur, moved)
                if _subtitle_split_units(trial_cur) > soft_max:
                    break
                if not trial_cur.endswith("、"):
                    out[i] = trial_cur
                    out[i + 1] = _join_tokens(nxt_tokens)
                    if not out[i + 1].strip():
                        del out[i + 1]
                    adjusted = True
                    break
            if adjusted:
                i += 1
                continue

        # Keep known English technical bigrams together: state space, action
        # space, reward signal, etc.
        last_w = _last_ascii_word(cur)
        first_w = _first_ascii_word(nxt)
        if last_w and first_w and (last_w, first_w) in _EN_TECH_BIGRAMS:
            m = re.search(r"^(.*?)([A-Za-z0-9][A-Za-z0-9_.-]*)([，,。.!！？?；;：:、]*)$", cur)
            if m:
                head = m.group(1).rstrip()
                moved = m.group(2) + m.group(3)
                trial_next = _smart_concat(moved, nxt)
                if head and _subtitle_split_units(trial_next) <= soft_max:
                    out[i] = head
                    out[i + 1] = trial_next
                    i += 1
                    continue

        # Chinese label + English plural noun should stay together in this
        # report style, e.g. "訓練 episodes".
        if cur.endswith("訓練") and re.match(r"(?i)^episodes\b", nxt):
            head = cur[:-2].rstrip()
            trial_next = _join_tokens(["訓練", nxt])
            if head and _subtitle_split_units(trial_next) <= soft_max:
                out[i] = head
                out[i + 1] = trial_next
                i += 1
                continue

        i += 1

    return [x for x in out if x.strip()]


def _rebalance_short_english_lines(lines: List[str], min_words: int = 1) -> List[str]:
    """Merge very short pure-English lines into neighboring English lines."""
    if len(lines) < 2:
        return lines
    out = [str(x or "").strip() for x in lines if str(x or "").strip()]
    i = 0
    while i < len(out):
        cur = out[i]
        words = re.findall(r"[A-Za-z][A-Za-z0-9_-]*", cur)
        first_word = words[0].lower() if words else ""
        is_marker_line = (
            not _has_cjk(cur)
            and first_word in _EN_DISCOURSE_MARKERS
            and cur.rstrip().endswith(",")
            and len(words) <= 3
        )
        if not _has_cjk(cur) and len(words) <= min_words:
            if is_marker_line and i + 1 < len(out) and not _has_cjk(out[i + 1]):
                # Marker should lead the next clause, not attach to previous sentence.
                out[i + 1] = _smart_concat(cur, out[i + 1])
                del out[i]
                continue
            # Keep strong sentence boundary explicit:
            # "... phases." + "Furthermore," must stay split.
            if i > 0 and out[i - 1].rstrip().endswith((".", "!", "?", "。", "！", "？")) and not is_marker_line:
                i += 1
                continue
            if i > 0 and not _has_cjk(out[i - 1]):
                out[i - 1] = _smart_concat(out[i - 1], cur)
                del out[i]
                continue
            if i + 1 < len(out) and not _has_cjk(out[i + 1]):
                out[i + 1] = _smart_concat(cur, out[i + 1])
                del out[i]
                continue
        elif is_marker_line and i + 1 < len(out) and not _has_cjk(out[i + 1]):
            # English marker line (e.g., "However,") should usually lead into next clause.
            out[i + 1] = _smart_concat(cur, out[i + 1])
            del out[i]
            continue
        i += 1
    return out


def _tokenize_word_level(text: str) -> List[str]:
    src = str(text or "").strip()
    if not src:
        return []
    if not _has_cjk(src):
        return _tokenize_ascii_readable(src)

    # 1) Protect atomic spans: `...` and bracket groups.
    atoms: Dict[str, str] = {}

    def _atom_repl(m: re.Match) -> str:
        k = f"\ue100{len(atoms)}\ue101"
        atoms[k] = m.group(0)
        return k

    masked = re.sub(r"`[^`]+`", _atom_repl, src)
    masked = re.sub(r"（[^（）]*）|\([^()]*\)|【[^【】]*】|\[[^\[\]]*\]|《[^《》]*》|\{[^{}]*\}", _atom_repl, masked)

    # 2) Tokenize with jieba for CJK parts; keep ASCII/code-like tokens.
    jieba = _get_jieba()
    if jieba is None:
        base = _tokenize_clause(masked)
    else:
        base = [seg.strip() for seg in jieba.lcut(masked, cut_all=False) if str(seg or "").strip()]

    # 3) Normalize tokens: merge placeholders and punctuation bindings.
    merged: List[str] = []
    i = 0
    while i < len(base):
        tok = str(base[i] or "")
        # merge split placeholder chunks
        if "\ue100" in tok and "\ue101" not in tok:
            j = i + 1
            buf = [tok]
            while j < len(base) and "\ue101" not in str(base[j] or ""):
                buf.append(str(base[j] or ""))
                j += 1
            if j < len(base):
                buf.append(str(base[j] or ""))
                j += 1
            tok = "".join(buf)
            i = j
        else:
            i += 1

        # restore atom token if exact
        if tok in atoms:
            tok = atoms[tok]
        merged.append(tok)

    # 4) Bind pure punctuation to previous token where possible.
    out: List[str] = []
    for tok in merged:
        if _is_punct_only(tok):
            if out:
                out[-1] = f"{out[-1]}{tok}"
            else:
                out.append(tok)
            continue
        out.append(tok)

    # 4.5) Merge split ASCII connector tokens:
    # ERR_KUBE_SECRET_ + DECRYPTION_FAILED -> ERR_KUBE_SECRET_DECRYPTION_FAILED
    changed = True
    while changed:
        changed = False
        merged_ascii: List[str] = []
        i = 0
        while i < len(out):
            cur = str(out[i] or "")
            if i + 1 < len(out):
                nxt = str(out[i + 1] or "")
                cur_core = _strip_edge_punct(cur)
                nxt_core = _strip_edge_punct(nxt)
                if (
                    cur_core.endswith(("_", "-", "."))
                    and _is_ascii_token(cur_core[:-1] or "A")
                    and _is_ascii_token(nxt_core)
                ):
                    # keep punctuation suffix from nxt if any
                    suffix = nxt[len(nxt_core):] if nxt.startswith(nxt_core) else ""
                    merged_ascii.append(cur_core + nxt_core + suffix)
                    i += 2
                    changed = True
                    continue
            merged_ascii.append(cur)
            i += 1
        out = merged_ascii

    # 5) Restore atom markers that remained embedded.
    if atoms:
        restored: List[str] = []
        for tok in out:
            s = tok
            for k, v in atoms.items():
                s = s.replace(k, v)
            restored.append(s)
        out = restored

    # 5.5) Attach standalone "的" to previous token.
    if out:
        glued: List[str] = []
        for tok in out:
            core = _strip_edge_punct(tok)
            if core == "的" and glued:
                # preserve punctuation after 的 if any
                suffix = tok[len(core):] if tok.startswith(core) else ""
                glued[-1] = f"{glued[-1]}的{suffix}"
            else:
                glued.append(tok)
        out = glued

    # 6) Merge adjacent CJK singleton tokens into minimal word units.
    out = _merge_cjk_singletons(out)

    return out


def _token_unit(tok: str) -> float:
    return _subtitle_split_units(str(tok or ""))


def _is_strong_dot_boundary(text: str, idx: int) -> bool:
    """Decide whether '.' at text[idx] is a sentence boundary."""
    if idx < 0 or idx >= len(text) or text[idx] != ".":
        return False
    prev_ch = text[idx - 1] if idx > 0 else ""
    next_ch = text[idx + 1] if idx + 1 < len(text) else ""

    # Decimal/version: 3.14 / v2.4.1
    if prev_ch.isdigit() and next_ch.isdigit():
        return False

    # Common dot-connected tokens: main.py / abc.def
    # But allow sentence junction like "workloads.To"
    if prev_ch.isalnum() and next_ch.isalnum() and not (prev_ch.isalpha() and next_ch.isupper()):
        return False

    # Find next significant char (skip spaces/quotes/brackets)
    j = idx + 1
    while j < len(text) and text[j] in " \t\r\n\"'”’）)]】》":
        j += 1
    next_sig = text[j] if j < len(text) else ""

    # A title/reference abbreviation is followed by its argument, not a new
    # sentence: Dr. Chen / Fig. 3 / Prof. Wang.
    tail = text[max(0, idx - 12): idx + 1]
    word_match = re.search(r"\b([A-Za-z]+)\.$", tail)
    if word_match and word_match.group(1).lower() in _NON_TERMINAL_DOT_ABBREVIATIONS:
        return False
    # Person-name initials: J. Smith / J. R. R. Tolkien.
    if re.search(r"\b[A-Z]\.$", tail) and next_sig.isupper():
        return False
    dotted_match = re.search(r"\b((?:[A-Za-z]\.){2,})$", tail)
    dotted_core = dotted_match.group(1).rstrip(".").lower() if dotted_match else ""
    if dotted_core in _CONTINUATION_DOTTED_ABBREVIATIONS:
        return False
    # A time qualifier normally closes the sentence when the following token
    # starts like a new one: "at 3:45 p.m. They reviewed ...".
    if dotted_core in _TIME_DOTTED_ABBREVIATIONS:
        return bool(next_sig and (next_sig.isupper() or _is_cjk_char(next_sig)))
    # Other dotted acronyms are conservative unless another explicit sentence
    # boundary follows.  This avoids splitting "U.S. Army".
    if dotted_match:
        return False
    if re.search(r"\betc\.$", tail, re.IGNORECASE):
        return bool(next_sig and (next_sig.isupper() or _is_cjk_char(next_sig)))

    # If no next significant char, treat as sentence end.
    if not next_sig:
        return True

    # Strongly likely sentence boundary: next sentence starts with upper/CJK.
    if next_sig.isupper() or _is_cjk_char(next_sig):
        return True

    # Otherwise conservative: do not split.
    return False


def _pack_word_tokens(tokens: List[str], min_chars: int, max_chars: int, unit_fn=None) -> List[str]:
    if not tokens:
        return []
    if unit_fn is None:
        unit_fn = _subtitle_split_units
    lines: List[str] = []
    buf: List[str] = []

    def _ends_weak(tok: str) -> bool:
        s = str(tok or "").rstrip()
        return bool(s) and s[-1] in _WEAK_PUNCT

    def _ends_strong(tok: str) -> bool:
        s = str(tok or "").rstrip()
        return bool(s) and s[-1] in _STRONG_PUNCT

    def _is_numeric_comma_boundary(prev_tok: str, next_tok: str) -> bool:
        prev = str(prev_tok or "").rstrip()
        nxt = str(next_tok or "").lstrip()
        return bool(re.search(r"\d[,]$", prev) and re.match(r"\d", nxt))

    def _is_single_cjk(tok: str) -> bool:
        core = _strip_edge_punct(tok)
        return len(core) == 1 and _is_all_cjk(core)

    def _is_ascii_connector_token(tok: str) -> bool:
        core = _strip_edge_punct(tok)
        # keep snake_case / kebab-case / dotted-version as atomic by penalizing split
        return bool(re.fullmatch(r"[A-Za-z0-9]+(?:[_\-\.][A-Za-z0-9]+)+", core))

    def _choose_cut(candidate: List[str]) -> int:
        # choose split index k: left=candidate[:k], right=candidate[k:]
        # preference: weak punctuation boundary > balanced length > avoid CJK single-char split
        total_u = unit_fn(_join_tokens(candidate))
        if len(candidate) <= 1 or total_u <= max_chars:
            return len(candidate)
        target = (min_chars + max_chars) / 2.0
        best_k = len(candidate) - 1
        best_score = -1e18
        # First pass: prefer latest weak/strong punctuation boundary that keeps
        # left side not too short.
        for k in range(len(candidate) - 1, 0, -1):
            left = candidate[:k]
            lu = unit_fn(_join_tokens(left))
            if lu < max(8.0, min_chars * 0.6):
                continue
            prev_tok = left[-1]
            next_tok = candidate[k] if k < len(candidate) else ""
            right_u = unit_fn(_join_tokens(candidate[k:])) if k < len(candidate) else 0
            if (_ends_weak(prev_tok) and lu < min_chars and right_u > max_chars):
                continue
            if (_ends_strong(prev_tok) or _ends_weak(prev_tok)) and not _is_numeric_comma_boundary(prev_tok, next_tok):
                return k

        for k in range(1, len(candidate)):
            left = candidate[:k]
            right = candidate[k:]
            lu = unit_fn(_join_tokens(left))
            ru = unit_fn(_join_tokens(right))
            if lu <= 0 or ru <= 0:
                continue
            score = -abs(lu - target) * 3.0
            if lu > max_chars:
                score -= (lu - max_chars) * 60.0
            if lu < min_chars:
                score -= (min_chars - lu) * 25.0
            # Prefer front-long / back-short cadence.
            if lu < ru:
                score -= (ru - lu) * 8.0
            else:
                score += min((lu - ru), 8.0) * 2.5
            prev_tok = left[-1]
            next_tok = right[0]
            if _ends_strong(prev_tok):
                score += 1000.0
            elif _ends_weak(prev_tok):
                score += 700.0
                if lu < min_chars and ru > max_chars:
                    score -= 3000.0
            if _is_numeric_comma_boundary(prev_tok, next_tok):
                score -= 2400.0
            # avoid obvious lexical break like 關 / 係...
            if _is_single_cjk(prev_tok) and _strip_edge_punct(next_tok)[:1] and _is_cjk_char(_strip_edge_punct(next_tok)[:1]):
                score -= 1200.0
            if _is_ascii_connector_token(prev_tok) or _is_ascii_connector_token(next_tok):
                score -= 1600.0
            # generic CJK-CJK boundary (non-punctuation) should be less preferred.
            prev_core = _strip_edge_punct(prev_tok)
            next_core = _strip_edge_punct(next_tok)
            if prev_core.lower() in _EN_ORPHAN_END or prev_core in _ZH_ORPHAN_END or prev_core in _ZH_FORBIDDEN_LINE_END:
                score -= 2200.0
            next_lower = next_core.lower()
            if next_lower in _EN_PREFERRED_BREAK_BEFORE:
                # Prefer putting a complete prepositional/subordinate phrase on
                # the next subtitle: "processes 1.23GB of data" / "in 120ms".
                score += 420.0
            elif next_lower in _EN_ORPHAN_END or next_core in _ZH_ORPHAN_END:
                score -= 500.0
            last_w = _last_ascii_word(prev_tok)
            first_w = _first_ascii_word(next_tok)
            if last_w and first_w and (last_w, first_w) in _EN_TECH_BIGRAMS:
                score -= 2200.0
            if prev_core and next_core and (prev_core, next_core) in _ZH_TECH_BIGRAMS:
                score -= 2200.0
            if prev_core and next_core and _is_all_cjk(prev_core) and _is_all_cjk(next_core):
                if not _ends_weak(prev_tok) and not _ends_strong(prev_tok):
                    score -= 260.0
                # hard guard: do not split likely Chinese lexical chunks.
                if (
                    len(prev_core) == 1
                    or len(next_core) == 1
                    or (len(prev_core) <= 2 and len(next_core) <= 2)
                ):
                    score -= 1400.0
            # prefer not to start next line with closing quote
            nh = str(next_tok).lstrip()[:1]
            if nh in _CLOSE_QUOTES:
                score -= 900.0
            if score > best_score:
                best_score = score
                best_k = k
        return best_k

    for t in tokens:
        t = str(t or "").strip()
        if not t:
            continue
        buf.append(t)
        while unit_fn(_join_tokens(buf)) > max_chars and len(buf) > 1:
            k = _choose_cut(buf)
            left = buf[:k]
            right = buf[k:]
            if not left or not right:
                break
            lines.append(_join_tokens(left))
            buf = right

    if buf:
        lines.append(_join_tokens(buf))

    # orphan merge pass
    lines = _repair_short_orphan_lines(lines, min_chars=min_chars, max_chars=max_chars)
    lines = _rebalance_short_neighbor_lines(lines, min_chars=min_chars, max_chars=max_chars)
    lines = _rollback_weak_tail(lines)
    lines = _repair_obvious_cjk_word_break(lines)
    return [ln.strip() for ln in lines if ln.strip()]


def _boundary_penalty(prev_tok: str, next_tok: str) -> float:
    # Lower is better. Penalize cutting inside likely semantic units.
    p = 0.0
    # Chinese comma is a natural clause boundary — strongly prefer splitting just
    # AFTER it, so the comma stays at the end of the left segment (where it may
    # then be stripped) rather than appearing at the start of the right segment.
    if prev_tok == '\uff0c':  # ，= U+FF0C
        p -= 6.0
    if next_tok == '\uff0c':
        p += 3.0  # avoid cutting right BEFORE a comma (comma should stay with its clause)
    if re.search(r"\d[,]$", str(prev_tok or "").rstrip()) and re.match(r"\d", str(next_tok or "").lstrip()):
        p += 500.0
    if prev_tok in _SPLIT_CONNECTORS:
        p += 3.0
    if next_tok in _SPLIT_CONNECTORS:
        p -= 2.0
    # Never split across connector-bound technical tokens.
    if prev_tok.endswith(("-", "_", ".")) and _is_ascii_token(next_tok.split()[0] if next_tok else ""):
        p += 100.0
    if _is_ascii_word(prev_tok) and _is_ascii_word(next_tok):
        p += 14.0
    if len(prev_tok) == 1 and len(next_tok) == 1 and _is_cjk_char(prev_tok) and _is_cjk_char(next_tok):
        # likely cutting in the middle of a Chinese lexical unit
        p += 220.0
    if _is_ascii_word(prev_tok) and _is_short_cjk_token(next_tok):
        p += 9.0 if next_tok in _CJK_SUFFIX_PROTECTED else 5.5
    if _is_short_cjk_token(prev_tok) and _is_ascii_word(next_tok):
        p += 4.0 if prev_tok in _CJK_PREFIX_LIGHT else 2.5
    if _is_all_cjk(prev_tok) and _is_all_cjk(next_tok):
        if len(prev_tok) == 2 and len(next_tok) == 2:
            p += 3.0
        elif len(prev_tok) == 1 or len(next_tok) == 1:
            p += 220.0

    prev_core = _strip_edge_punct(prev_tok)
    next_core = _strip_edge_punct(next_tok)

    # Quote pairing guard:
    # - opening quotes should not be line end
    # - closing quotes should not be line start
    prev_tail = str(prev_tok or "").rstrip()[-1:] if str(prev_tok or "").strip() else ""
    next_head = str(next_tok or "").lstrip()[:1] if str(next_tok or "").strip() else ""
    if prev_tail in _OPEN_QUOTES:
        p += 260.0
    if next_head in _CLOSE_QUOTES:
        p += 260.0

    # Avoid line-end orphan connectors (English/Chinese).
    if prev_core.lower() in _EN_ORPHAN_END:
        p += 80.0
    if next_core.lower() in _EN_PREFERRED_BREAK_BEFORE:
        p -= 18.0
    if prev_core in _ZH_ORPHAN_END:
        p += 80.0
    if prev_core in _ZH_FORBIDDEN_LINE_END:
        p += 140.0
    if any(prev_core.endswith(x) for x in ("在不同", "在高", "在低")):
        p += 120.0

    # Protect common technical noun phrases.
    last_w = _last_ascii_word(prev_tok)
    first_w = _first_ascii_word(next_tok)
    if last_w and first_w and (last_w, first_w) in _EN_TECH_BIGRAMS:
        p += 100.0
    if prev_core and next_core and (prev_core, next_core) in _ZH_TECH_BIGRAMS:
        p += 140.0

    # Protect mixed CJK technical terms like 高併發, 高可用, 低延遲.
    if (prev_core in {"高", "低", "多", "單", "超高", "超低"} or prev_core.endswith(("在高", "在低", "在多", "在單"))) and (
        next_core.startswith("併發")
        or next_core.startswith("進程")
        or next_core.startswith("程序")
        or next_core.startswith("執行緒")
        or next_core.startswith("線程")
        or next_core.startswith("可用")
        or next_core.startswith("延遲")
        or next_core.startswith("負載")
    ):
        p += 120.0

    # Keep acronym subject with following verb phrase (e.g., ASR 會漏詞).
    if re.fullmatch(r"[A-Z]{2,6}", prev_core) and next_core[:1] in {"會", "能", "要", "可", "將"}:
        p += 70.0
    return p


def _segment_cost(text_len: float, min_chars: int, max_chars: int) -> float:
    target = (min_chars + max_chars) / 2.0
    if text_len < min_chars:
        return 1000.0 + (min_chars - text_len) ** 2
    if text_len > max_chars:
        return 1000.0 + (text_len - max_chars) ** 2
    return (text_len - target) ** 2


def _pair_cost(left: List[str], right: List[str], min_chars: int, max_chars: int) -> float:
    left_len = _tokens_display_len(left)
    right_len = _tokens_display_len(right)
    if left_len <= 0 or right_len <= 0:
        return float("inf")
    return (
        _segment_cost(left_len, min_chars, max_chars)
        + _segment_cost(right_len, min_chars, max_chars)
        + _boundary_penalty(left[-1], right[0])
    )


def _rebalance_token_segments(
    segments: List[List[str]],
    *,
    min_chars: int,
    max_chars: int,
) -> List[List[str]]:
    if len(segments) < 2:
        return segments

    soft_max = max_chars + 4.5
    made_change = True
    guard = 0
    while made_change and guard < 8:
        guard += 1
        made_change = False
        for idx in range(len(segments) - 1):
            left = segments[idx]
            right = segments[idx + 1]
            if not left or not right:
                continue

            current = _pair_cost(left, right, min_chars, max_chars)
            best_left = left
            best_right = right
            best_cost = current

            if len(right) >= 2:
                cand_left = left + [right[0]]
                cand_right = right[1:]
                if _tokens_display_len(cand_left) <= soft_max and _tokens_display_len(cand_right) >= min_chars:
                    cand_cost = _pair_cost(cand_left, cand_right, min_chars, max_chars)
                    if cand_cost + 0.75 < best_cost:
                        best_left = cand_left
                        best_right = cand_right
                        best_cost = cand_cost

            if len(left) >= 2:
                cand_left = left[:-1]
                cand_right = [left[-1]] + right
                if _tokens_display_len(cand_left) >= min_chars and _tokens_display_len(cand_right) <= soft_max:
                    cand_cost = _pair_cost(cand_left, cand_right, min_chars, max_chars)
                    if cand_cost + 0.75 < best_cost:
                        best_left = cand_left
                        best_right = cand_right
                        best_cost = cand_cost

            if best_left is not left or best_right is not right:
                segments[idx] = best_left
                segments[idx + 1] = best_right
                made_change = True

    return [seg for seg in segments if seg]


def _split_tokens_balanced(tokens: List[str], min_chars: int, max_chars: int) -> List[str]:
    if not tokens:
        return []
    total = _subtitle_split_units(_join_tokens(tokens))
    if total <= max_chars:
        return [_join_tokens(tokens)]

    # Number of segments search range.
    min_seg = max(1, int(math.ceil(total / max(max_chars, 1))))
    max_seg = max(min_seg, int(math.floor(total / max(min_chars, 1))))
    max_seg = max(1, min(max_seg, len(tokens)))

    best_split: List[List[str]] = []
    best_cost = float("inf")

    # DP by number of segments.
    for seg_count in range(min_seg, max_seg + 1):
        dp = [[float("inf")] * (len(tokens) + 1) for _ in range(seg_count + 1)]
        prev = [[-1] * (len(tokens) + 1) for _ in range(seg_count + 1)]
        dp[0][0] = 0.0

        for s in range(1, seg_count + 1):
            for j in range(1, len(tokens) + 1):
                # choose split i -> j
                for i in range(s - 1, j):
                    if dp[s - 1][i] == float("inf"):
                        continue
                    seg_text = _join_tokens(tokens[i:j])
                    pure_len = _subtitle_split_units(seg_text)
                    cost = dp[s - 1][i] + _segment_cost(pure_len, min_chars, max_chars)
                    if i > 0:
                        cost += _boundary_penalty(tokens[i - 1], tokens[i])
                    if cost < dp[s][j]:
                        dp[s][j] = cost
                        prev[s][j] = i

        if dp[seg_count][len(tokens)] >= best_cost:
            continue

        # reconstruct
        j = len(tokens)
        s = seg_count
        segs_rev: List[List[str]] = []
        ok = True
        while s > 0:
            i = prev[s][j]
            if i < 0:
                ok = False
                break
            segs_rev.append(tokens[i:j])
            j = i
            s -= 1
        if not ok:
            continue

        segs = list(reversed([x for x in segs_rev if x]))
        segs = _rebalance_token_segments(segs, min_chars=min_chars, max_chars=max_chars)
        best_cost = dp[seg_count][len(tokens)]
        best_split = segs

    if not best_split:
        return [_join_tokens(tokens)]
    return [_join_tokens(seg) for seg in best_split if seg]


def _split_for_readability(raw_text: str, min_chars: int = 6, max_chars: int = 36) -> List[str]:
    """
    Split subtitles with two goals:
    1) Respect common punctuation boundaries first (，。！？!?；;： and line breaks).
    2) Keep each segment around min_chars~max_chars to avoid lines too short/long.
    """
    src = str(raw_text or "").strip()
    if not src:
        return []

    has_cjk = _has_cjk(src)
    _tok_cache: Dict[str, List[str]] = {}
    _unit_cache: Dict[str, float] = {}

    def _tok_once(s: str) -> List[str]:
        k = str(s or "")
        if k not in _tok_cache:
            _tok_cache[k] = _tokenize_word_level(k)
        return _tok_cache[k]

    def _units_once(s: str) -> float:
        k = str(s or "")
        if k not in _unit_cache:
            _unit_cache[k] = _subtitle_split_units(k)
        return _unit_cache[k]

    # New word-level splitter:
    # 1) split by strong punctuation first
    # 2) tokenize each clause at word level (never char hard-slice)
    # 3) pack by max units with orphan repair
    def _split_strong(text: str) -> List[str]:
        parts: List[str] = []
        buf: List[str] = []
        in_backtick = False
        stack: List[str] = []
        for i, ch in enumerate(text):
            buf.append(ch)
            if ch == "`":
                in_backtick = not in_backtick
            elif not in_backtick and ch in _PAIR_OPEN_TO_CLOSE:
                stack.append(_PAIR_OPEN_TO_CLOSE[ch])
            elif not in_backtick and stack and ch == stack[-1]:
                stack.pop()
            if in_backtick or stack:
                continue
            is_strong = ch in _STRONG_PUNCT
            if ch == ".":
                is_strong = _is_strong_dot_boundary(text, i)
            # Treat the outer closing quote as the sentence boundary so the
            # next subtitle never starts with a dangling 」/” after 「...。」.
            if (
                ch in _CLOSE_QUOTES
                and i > 0
                and (
                    text[i - 1] in _STRONG_PUNCT
                    or (text[i - 1] == "." and _is_strong_dot_boundary(text, i - 1))
                )
            ):
                is_strong = True
            if is_strong:
                seg = "".join(buf).strip()
                if seg:
                    parts.append(seg)
                buf = []
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
        return parts

    def _split_weak(text: str) -> List[str]:
        # CJK: comma/colon splitting is useful.
        # English-only: prefer stronger punctuation rhythm; keep comma much weaker.
        weak_set = _WEAK_PUNCT if has_cjk else set(",:;：；")
        parts: List[str] = []
        buf: List[str] = []
        in_backtick = False
        stack: List[str] = []
        for idx, ch in enumerate(text):
            buf.append(ch)
            if ch == "`":
                in_backtick = not in_backtick
            elif not in_backtick and ch in _PAIR_OPEN_TO_CLOSE:
                stack.append(_PAIR_OPEN_TO_CLOSE[ch])
            elif not in_backtick and stack and ch == stack[-1]:
                stack.pop()
            if in_backtick or stack:
                continue
            # Thousands separators and decimals are not subtitle boundaries.
            # Without this guard, "1,000" can become "1" / "000" after the
            # terminal-punctuation display cleanup.
            if (
                ch == ","
                and idx > 0
                and idx + 1 < len(text)
                and text[idx - 1].isdigit()
                and text[idx + 1].isdigit()
            ):
                continue
            # Times, aspect ratios, URL schemes, host ports and compact
            # key:value identifiers are atomic.  A colon without surrounding
            # whitespace between ASCII characters is not a clause boundary.
            if (
                ch in {":", "："}
                and idx > 0
                and idx + 1 < len(text)
                and (
                    (text[idx - 1].isdigit() and text[idx + 1].isdigit())
                    or (
                        ch == ":"
                        and text[idx - 1].isascii()
                        and text[idx - 1].isalnum()
                        and text[idx + 1].isascii()
                        and (text[idx + 1].isalnum() or text[idx + 1] == "/")
                    )
                )
            ):
                continue
            if ch in weak_set:
                seg = "".join(buf).strip()
                if seg:
                    parts.append(seg)
                buf = []
        tail = "".join(buf).strip()
        if tail:
            parts.append(tail)
        return parts

    def _finalize_clause_lines(lines: List[str]) -> List[str]:
        # Token packing may isolate an outer closing quote after sentence
        # punctuation.  Attach it back before orphan repair so no subtitle is
        # just "」" and no following subtitle starts with a dangling quote.
        quote_fixed: List[str] = []
        close_chars = "".join(re.escape(ch) for ch in _CLOSE_QUOTES)
        for line in lines:
            value = str(line or "").strip()
            match = re.match(rf"^([。.!！？?]*[{close_chars}]+)(.*)$", value)
            if match and quote_fixed:
                quote_fixed[-1] = f"{quote_fixed[-1]}{match.group(1)}"
                value = match.group(2).lstrip()
            if value:
                quote_fixed.append(value)
        lines = quote_fixed
        lines = _repair_short_orphan_lines(lines, min_chars=min_chars, max_chars=max_chars)
        lines = _rebalance_short_neighbor_lines(lines, min_chars=min_chars, max_chars=max_chars)
        lines = _rebalance_short_english_lines(lines, min_words=1)
        lines = _repair_obvious_cjk_word_break(lines)
        lines = _repair_unwanted_line_boundaries(lines, min_chars=min_chars, max_chars=max_chars)
        return [x for x in lines if x.strip()]

    def _should_merge_incomplete_weak_clause(cur: str, nxt: str) -> bool:
        combined = str(cur or "") + str(nxt or "")
        if _units_once(combined) > max_chars:
            return False
        core = _strip_terminal_sentence_punct(str(cur or "").strip()).rstrip("，,：:")
        if not core:
            return False
        return core.endswith(("在於", "狀態下", "情況下", "條件下", "過程中", "環境中"))

    clauses = _split_strong(src)
    out_lines: List[str] = []
    for c in clauses:
        weak_clauses = _split_weak(c)
        merged_weak: List[str] = []
        i = 0
        while i < len(weak_clauses):
            cur = weak_clauses[i]
            while i + 1 < len(weak_clauses):
                nxt = weak_clauses[i + 1]
                # `_split_weak` trims surrounding whitespace.  Rejoin through
                # the spacing-aware helper so English punctuation does not
                # collapse "First, load" into "First,load".
                combined = _smart_concat(cur, nxt)
                cur_u = _units_once(cur)
                nxt_u = _units_once(nxt)
                # Weak punctuation is a semantic candidate boundary.
                # Keep it when the left side is already meaningful enough.
                if cur_u >= min_chars:
                    if _should_merge_incomplete_weak_clause(cur, nxt):
                        cur = combined
                        i += 1
                        continue
                    break
                # Too-short lead clause, e.g. "大家好，": merge if the merged
                # subtitle still fits.
                if _units_once(combined) <= max_chars or (cur_u <= 4.0 and _units_once(combined) <= max_chars + 2.0):
                    cur = combined
                    i += 1
                    continue
                # If next clause can stand alone, keep the weak boundary even
                # though cur is short: [8]，[24] with max 28 => [8] / [24].
                if nxt_u <= max_chars:
                    break
                # If next itself is too long, merge once and let the packer use
                # this comma as the preferred first cut: [8]，[32] => [8+x] /
                # [32-x], not [8] / [y] / [32-y].
                cur = combined
                i += 1
                continue
            merged_weak.append(cur)
            i += 1

        clause_lines: List[str] = []
        for wc in merged_weak:
            if _units_once(wc) <= max_chars + 1.5:
                clause_lines.append(wc)
                continue
            toks = _tok_once(wc)
            clause_lines.extend(_pack_word_tokens(toks, min_chars=min_chars, max_chars=max_chars, unit_fn=_units_once))
        out_lines.extend(_finalize_clause_lines(clause_lines))
    # Cross-clause safety pass.  Some token paths can still emit closing
    # punctuation/quotes as a separate line; always attach them backward.
    attached_lines: List[str] = []
    close_chars = "".join(re.escape(ch) for ch in _CLOSE_QUOTES)
    for line in out_lines:
        value = str(line or "").strip()
        match = re.match(rf"^([。.!！？?]*[{close_chars}]+)(.*)$", value)
        if match and attached_lines:
            attached_lines[-1] = f"{attached_lines[-1]}{match.group(1)}"
            value = match.group(2).lstrip()
        if value:
            attached_lines.append(value)
    # Restore prior display rule: drop trailing weak/strong sentence punctuation
    # (，。!?;: etc.) but keep brackets/quotes untouched.
    return [_strip_terminal_sentence_punct(x) for x in attached_lines if x.strip()]
