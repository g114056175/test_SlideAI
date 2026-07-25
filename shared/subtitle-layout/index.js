export const DEFAULTS = {
  HIGHLIGHT_LEAD_SEC: 0.0,
  LAST_TOKEN_HOLD_SEC: 0.2,
  SEGMENT_LINGER_SEC: 0.3,
  SUBTITLE_ACTIVE_TOLERANCE_SEC: 0.12,
  DEFAULT_SUBTITLE_MIN_WORD_MS: 90,
}

const minSpokenUnitDuration = (text) => {
  const raw = String(text || '').replace(/[\s，。！？；：、,.!?;:'"“”‘’)\]】》」』]+$/g, '').trim()
  if (/^\d+(?:\.\d+)?%$/.test(raw)) return Math.min(1.1, 0.42 + 0.08 * raw.length)
  return 0
}

/**
 * LAYOUT — single source of truth for subtitle box geometry.
 * Both the frontend SubtitleCanvasVariant.vue and the backend
 * canvas_renderer/render-optimized-v2.mjs MUST import from here.
 * Changing a value here automatically updates both sides.
 */
export const LAYOUT = {
  // ── Font ─────────────────────────────────────────────────────────────────
  // Weight 400 = Regular; matches NotoSerifCJK-Regular.ttc exactly.
  // Using 500 causes ambiguous fallback in skia-canvas vs browser.
  FONT_WEIGHT: '400',
  FONT_FAMILY: '"Noto Serif CJK TC", "Noto Serif CJK SC", serif',

  // ── Reference width for font-size scaling ────────────────────────────────
  BASE_CANVAS_WIDTH: 1920,

  // ── Text layout ratios (× effectiveFontSize) ─────────────────────────────
  LINE_HEIGHT_RATIO: 1.35,
  LINE_GAP_RATIO: 0.35,
  PAD_X_RATIO: 0.45,
  PAD_Y_RATIO: 0.35,

  // ── Minimum pixel values ──────────────────────────────────────────────────
  PAD_X_MIN: 12,
  PAD_Y_MIN: 8,
  LINE_GAP_MIN: 4,

  // ── Canvas-level ratios ───────────────────────────────────────────────────
  MAX_TEXT_WIDTH_RATIO: 0.82,     // max text box width relative to canvas width
  BOTTOM_MARGIN_RATIO: 0.03,      // gap from bottom of FULL canvas height (not strip)
  BOX_RADIUS_RATIO: 0.22,
  STROKE_WIDTH_RATIO: 0.14,

  // ── textBaseline strategy ─────────────────────────────────────────────────
  // Use 'top' so em-box top is at the draw point; less ambiguous than 'middle'
  // for CJK across browser canvas vs skia-canvas.
  TEXT_BASELINE: 'top',
}

export const isPuncOrSpace = (text) => /^[，。！？；：「」『』（）、,.!?;:'"“”‘’\-—(){}\[\]<>《》…\s]+$/.test(String(text || ''))
export const hasCjkChar = (text) => /[\u3400-\u9fff]/.test(String(text || ''))

export const tokenizeSubtitlePieces = (text) => {
  const src = String(text || '')
  if (/^\d+(?:\.\d+)+%?$/.test(src)) {
    return [{ text: src, highlightable: true }]
  }
  if (/\s/.test(src)) {
    return src
      .split(/(\s+)/)
      .filter((x) => x.length > 0)
      .map((x) => ({ text: x, highlightable: !isPuncOrSpace(x) }))
  }
  const out = []
  let asciiBuf = ''
  const flushAscii = () => {
    if (!asciiBuf) return
    out.push({ text: asciiBuf, highlightable: true })
    asciiBuf = ''
  }
  for (let i = 0; i < src.length;) {
    const rest = src.slice(i)
    const decimal = rest.match(/^\d+(?:\.\d+)+%?/)
    if (decimal) {
      flushAscii()
      out.push({ text: decimal[0], highlightable: true })
      i += decimal[0].length
      continue
    }
    const ch = src[i]
    if (/[A-Za-z0-9]/.test(ch)) {
      asciiBuf += ch
      i += 1
      continue
    }
    flushAscii()
    out.push({ text: ch, highlightable: !isPuncOrSpace(ch) })
    i += 1
  }
  flushAscii()
  return out
}

export const normalizeSubtitleSegments = (segments) => {
  return (Array.isArray(segments) ? segments : [])
    .map((seg) => {
      const words = Array.isArray(seg?.words)
        ? seg.words
            .map((w) => ({
              text: String(w?.text || w?.word || '').trim(),
              start: Number(w?.start),
              end: Number(w?.end),
            }))
            .filter((w) => w.text && Number.isFinite(w.start) && Number.isFinite(w.end) && w.end > w.start)
            .sort((a, b) => a.start - b.start)
        : []

      const segStart = Number(seg?.start) || 0
      const segEnd = Number(seg?.end) || 0
      const wordStart = words.length ? Number(words[0].start) : segStart
      const wordEnd = words.length ? Number(words[words.length - 1].end) : segEnd
      return {
        start: wordStart,
        end: wordEnd,
        text: String(seg?.text || '').trim(),
        words,
      }
    })
    .filter((seg) => seg.end > seg.start && seg.text.length > 0)
    .sort((a, b) => a.start - b.start)
}

export const resolveActiveWordIndex = (words, currentSec, strictMode = false) => {
  const now = Number(currentSec) || 0
  if (!Array.isArray(words) || words.length === 0) return -1

  let idx = words.findIndex((w) => now >= Number(w.start) && now < Number(w.end))
  if (idx >= 0) return idx

  const eps = strictMode ? 0.06 : 0.08
  idx = words.findIndex((w) => now >= Number(w.start) && now < Number(w.end) + eps)
  if (idx >= 0) return idx

  let prevIdx = -1
  for (let i = 0; i < words.length; i += 1) {
    const s = Number(words[i].start)
    if (!Number.isFinite(s)) continue
    if (s <= now) prevIdx = i
    else break
  }
  return prevIdx
}

export const buildSmoothedWordTimeline = (words, segStart, segEnd, minWordMs = DEFAULTS.DEFAULT_SUBTITLE_MIN_WORD_MS, lastTokenHoldSec = DEFAULTS.LAST_TOKEN_HOLD_SEC) => {
  if (!Array.isArray(words) || words.length === 0) return []
  const n = words.length
  const start = Number(segStart) || 0
  const end = Number(segEnd) || start
  const segDur = Math.max(0.001, end - start)
  const requestedMin = Math.max(0.03, (Number(minWordMs) || 90) / 1000)
  const feasibleMin = Math.max(0.02, (segDur / Math.max(n, 1)) * 0.9)
  const minDur = Math.min(requestedMin, feasibleMin)
  const uniformDur = segDur / n
  const blendRaw = 0.25

  const boundaries = new Array(n + 1).fill(0)
  boundaries[0] = start
  boundaries[n] = end

  for (let i = 1; i < n; i += 1) {
    const prev = words[i - 1]
    const cur = words[i]
    const prevEnd = Number(prev?.end)
    const curStart = Number(cur?.start)
    const rawMid = Number.isFinite(prevEnd) && Number.isFinite(curStart)
      ? (prevEnd + curStart) / 2
      : start + i * uniformDur
    const uniMid = start + i * uniformDur
    boundaries[i] = (blendRaw * rawMid) + ((1 - blendRaw) * uniMid)
  }

  for (let i = 1; i <= n; i += 1) boundaries[i] = Math.max(boundaries[i], boundaries[i - 1] + minDur)
  boundaries[n] = end
  for (let i = n - 1; i >= 0; i -= 1) boundaries[i] = Math.min(boundaries[i], boundaries[i + 1] - minDur)
  boundaries[0] = start
  boundaries[n] = end

  const out = []
  for (let i = 0; i < n; i += 1) {
    const s = Math.max(start, boundaries[i])
    let e = Math.min(end, boundaries[i + 1])
    if (e <= s) e = Math.min(end, s + Math.max(minDur * 0.5, 0.02))
    const minSpoken = minSpokenUnitDuration(words[i]?.text)
    if (minSpoken > 0 && e - s < minSpoken) e = Math.min(end, s + minSpoken)
    out.push({ text: String(words[i]?.text || '').trim(), start: s, end: e })
  }

  if (out.length > 0) {
    const lastIdx = out.length - 1
    const endCap = end + lastTokenHoldSec
    out[lastIdx].end = Math.max(out[lastIdx].end, Math.min(endCap, out[lastIdx].end + lastTokenHoldSec))
  }
  return out
}

export const findActiveSegment = (normalizedSegments, currentSec, isQwenBackend = false, toleranceSec = DEFAULTS.SUBTITLE_ACTIVE_TOLERANCE_SEC) => {
  const segs = Array.isArray(normalizedSegments) ? normalizedSegments : []
  if (!segs.length) return null
  const current = Number(currentSec) || 0
  const tol = isQwenBackend ? 0.0 : Number(toleranceSec || 0)
  const idx = segs.findIndex((seg) => current >= seg.start - tol && current < seg.end + tol)
  return idx >= 0 ? segs[idx] : null
}

export const buildSubtitleOverlayPieces = ({
  segment,
  currentSec,
  enableHighlight = true,
  isQwenBackend = false,
  forceWordTimeline = false,
  highlightLeadSec = DEFAULTS.HIGHLIGHT_LEAD_SEC,
  segmentLingerSec = DEFAULTS.SEGMENT_LINGER_SEC,
  minWordMs = DEFAULTS.DEFAULT_SUBTITLE_MIN_WORD_MS,
}) => {
  const seg = segment
  if (!seg?.text) return []
  const current = Number(currentSec) || 0
  const effectiveNow = Math.min(Number(seg.end) - 0.001, current + Number(highlightLeadSec || 0))

  const preferCharStable = hasCjkChar(seg.text)
  const canUseWordTimeline = Array.isArray(seg.words) && seg.words.length > 0
  if (canUseWordTimeline && (forceWordTimeline || !preferCharStable || isQwenBackend)) {
    const timelineWords = isQwenBackend
      ? seg.words
      : buildSmoothedWordTimeline(seg.words, seg.start, seg.end, minWordMs, DEFAULTS.LAST_TOKEN_HOLD_SEC)
    let activeWordIdx = resolveActiveWordIndex(timelineWords, effectiveNow, isQwenBackend)
    if (activeWordIdx < 0 && current >= Number(seg.end) - Number(segmentLingerSec || 0)) {
      activeWordIdx = timelineWords.length - 1
    }

    const out = []
    for (let i = 0; i < timelineWords.length; i += 1) {
      const w = timelineWords[i]
      const subPieces = tokenizeSubtitlePieces(w.text)
      for (const sp of subPieces) {
        out.push({
          text: sp.text,
          highlightable: sp.highlightable,
          active: Boolean(enableHighlight) && sp.highlightable && (i === activeWordIdx),
        })
      }
      if (i < timelineWords.length - 1) {
        const nextText = timelineWords[i + 1].text
        const t1 = w.text.slice(-1)
        const t2 = nextText.charAt(0)
        const isAscii1 = /[A-Za-z0-9]/.test(t1)
        const isAscii2 = /[A-Za-z0-9]/.test(t2)
        const isCjk1 = /[\u3400-\u9fff]/.test(t1)
        const isCjk2 = /[\u3400-\u9fff]/.test(t2)
        if ((isAscii1 && isAscii2) || (isCjk1 && isAscii2) || (isAscii1 && isCjk2)) {
          out.push({ text: ' ', highlightable: false, active: false })
        }
      }
    }
    return out
  }

  const pieces = tokenizeSubtitlePieces(seg.text)
  const hlCount = pieces.filter((p) => p.highlightable).length
  if (hlCount <= 0) return pieces.map((p) => ({ ...p, active: false }))

  const segDur = Math.max(0.001, Number(seg.end) - Number(seg.start))
  const progress = Math.max(0, Math.min(1, (effectiveNow - Number(seg.start)) / segDur))
  const activeIdx = Math.min(hlCount - 1, Math.floor(progress * hlCount))

  let seen = -1
  return pieces.map((p) => {
    if (!p.highlightable) return { ...p, active: false }
    seen += 1
    return { ...p, active: Boolean(enableHighlight) && seen === activeIdx }
  })
}

const _piecesSignature = (pieces) => (Array.isArray(pieces) ? pieces.map((p) => `${p.text}:${p.active ? 1 : 0}`).join('|') : '')

export const buildSubtitleStateTimeline = ({
  normalizedSegments,
  durationSec,
  enableHighlight = true,
  isQwenBackend = false,
  forceWordTimeline = false,
  highlightLeadSec = DEFAULTS.HIGHLIGHT_LEAD_SEC,
  segmentLingerSec = DEFAULTS.SEGMENT_LINGER_SEC,
  minWordMs = DEFAULTS.DEFAULT_SUBTITLE_MIN_WORD_MS,
  toleranceSec = DEFAULTS.SUBTITLE_ACTIVE_TOLERANCE_SEC,
}) => {
  const segs = Array.isArray(normalizedSegments) ? normalizedSegments : []
  const endSec = Math.max(0.001, Number(durationSec) || 0.001)
  const boundaries = new Set([0, endSec])

  for (const seg of segs) {
    boundaries.add(Math.max(0, Number(seg.start) || 0))
    boundaries.add(Math.max(0, Number(seg.end) || 0))
    if (!enableHighlight) {
      continue
    }
    const words = Array.isArray(seg.words) ? seg.words : []
    const timelineWords = (isQwenBackend || forceWordTimeline)
      ? words
      : buildSmoothedWordTimeline(words, seg.start, seg.end, minWordMs, DEFAULTS.LAST_TOKEN_HOLD_SEC)
    for (const w of timelineWords) {
      boundaries.add(Math.max(0, Number(w.start) || 0))
      boundaries.add(Math.max(0, Number(w.end) || 0))
    }
  }

  const sorted = Array.from(boundaries).filter((x) => Number.isFinite(x) && x >= 0 && x <= endSec).sort((a, b) => a - b)
  const out = []
  let prevSig = null
  for (let i = 0; i < sorted.length - 1; i += 1) {
    const start = sorted[i]
    const end = sorted[i + 1]
    if (end <= start) continue
    const t = start + (end - start) * 0.5
    const seg = findActiveSegment(segs, t, isQwenBackend, toleranceSec)
    const pieces = buildSubtitleOverlayPieces({
      segment: seg,
      currentSec: t,
      enableHighlight,
      isQwenBackend,
      forceWordTimeline,
      highlightLeadSec,
      segmentLingerSec,
      minWordMs,
    })
    const sig = _piecesSignature(pieces)
    if (out.length > 0 && sig === prevSig) {
      out[out.length - 1].end = end
    } else {
      out.push({start, end, pieces})
      prevSig = sig
    }
  }
  return out
}
