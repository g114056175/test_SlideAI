export function formatAssTime(sec) {
  sec = Math.max(0, sec)
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  let s = Math.floor(sec % 60)
  let cs = Math.round((sec - Math.floor(sec)) * 100)
  if (cs === 100) {
    cs = 99
    s += 1
    if (s === 60) {
      s = 0
      m += 1
    }
  }
  const pad = (num) => String(num).padStart(2, '0')
  return `${h}:${pad(m)}:${pad(s)}.${pad(cs)}`
}

export function tokenizePieces(text) {
  const src = String(text || '')
  const isPuncOrSpace = (s) => /^[，。！？；：「」『』（）、,.!?;:'"“”‘’\-—(){}\[\]<>《》…\s]+$/.test(s)

  if (/\\s/.test(src)) {
    return src
      .split(/(\\s+)/)
      .filter((x) => x.length > 0)
      .map((x) => ({ text: x, hg: !isPuncOrSpace(x) }))
  }

  const out = []
  let asciiBuf = ''
  const flush = () => {
    if (!asciiBuf) return
    out.push({ text: asciiBuf, hg: true })
    asciiBuf = ''
  }

  for (const ch of Array.from(src)) {
    if (/[A-Za-z0-9]/.test(ch)) {
      asciiBuf += ch
    } else {
      flush()
      out.push({ text: ch, hg: !isPuncOrSpace(ch) })
    }
  }
  flush()
  return out
}

export function smoothWordTimeline(words, segStart, segEnd, minWordSec = 0.09) {
  if (!words || !words.length) return []
  const n = words.length
  segStart = Number(segStart)
  segEnd = Number(segEnd)
  const segDur = Math.max(0.001, segEnd - segStart)

  const requestedMin = Math.max(0.03, minWordSec)
  const feasibleMin = Math.max(0.02, (segDur / Math.max(n, 1)) * 0.9)
  const minDur = Math.min(requestedMin, feasibleMin)

  const uniformDur = segDur / n
  const blendRaw = 0.25

  const boundaries = new Array(n + 1).fill(0)
  boundaries[0] = segStart
  boundaries[n] = segEnd

  for (let i = 1; i < n; i++) {
    const prev = words[i - 1]
    const cur = words[i]
    let rawMid
    if (prev.end !== undefined && cur.start !== undefined) {
      rawMid = (Number(prev.end) + Number(cur.start)) / 2
    } else {
      rawMid = segStart + i * uniformDur
    }
    const uniMid = segStart + i * uniformDur
    boundaries[i] = blendRaw * rawMid + (1 - blendRaw) * uniMid
  }

  for (let i = 1; i <= n; i++) {
    boundaries[i] = Math.max(boundaries[i], boundaries[i - 1] + minDur)
  }
  boundaries[n] = segEnd
  for (let i = n - 1; i >= 0; i--) {
    boundaries[i] = Math.min(boundaries[i], boundaries[i + 1] - minDur)
  }

  boundaries[0] = segStart
  boundaries[n] = segEnd

  const out = []
  for (let i = 0; i < n; i++) {
    let s = Math.max(segStart, boundaries[i])
    let e = Math.min(segEnd, boundaries[i + 1])
    if (e <= s) {
      e = Math.min(segEnd, s + Math.max(minDur * 0.5, 0.02))
    }
    out.push({
      text: String(words[i].text || '').trim(),
      start: s,
      end: e
    })
  }
  return out
}

export function generateAssString(width, height, segments, styleName, fontSize, alpha, highlight, isQwen) {
  const alphaHex = Math.round(255 - (alpha / 100) * 255).toString(16).padStart(2, '0').toUpperCase()

  const baseColor = '&H00FFFFFF'
  const hgColor = '&H0024BFFB'

  let bgColor = `&H${alphaHex}37291F`
  let borderStyle = 4
  let outline = 0
  let shadow = Math.max(1, Math.round(fontSize * 0.25))
  let outlineColor = '&H00000000'

  if (styleName === 'bg-gray') {
    bgColor = `&H${alphaHex}63554B`
  } else if (styleName === 'stroke-dark') {
    borderStyle = 1
    outline = Math.max(2, Math.floor(fontSize * 0.08))
    shadow = 0
    bgColor = '&HFF000000'
    outlineColor = '&H00000000'
  }

  const marginV = Math.max(20, Math.floor(height * 0.04))

  let header = `[Script Info]\nScriptType: v4.00+\nPlayResX: ${width}\nPlayResY: ${height}\nWrapStyle: 1\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n`

  header += `Style: Layer0,Noto Sans CJK TC,${fontSize},${baseColor},${baseColor},${outlineColor},${bgColor},-1,0,0,0,100,100,1.0,0,${borderStyle},${outline},${shadow},2,20,20,${marginV},1\n`
  header += `Style: Layer1,Noto Sans CJK TC,${fontSize},${baseColor},${baseColor},&H00000000,&HFF000000,-1,0,0,0,100,100,1.0,0,1,0,0,2,20,20,${marginV},1\n\n`

  header += `[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n`

  const events = []

  for (const seg of segments) {
    const segStart = Number(seg.start || 0)
    const segEnd = Number(seg.end || 0)
    const segText = seg.text || ''
    const words = seg.words || []

    const hasCjk = /[\u4e00-\u9fff]/.test(segText)

    if (!highlight || !words.length || (hasCjk && !isQwen)) {
      events.push(`Dialogue: 0,${formatAssTime(segStart)},${formatAssTime(segEnd)},Layer0,,0,0,0,,${segText}`)
      events.push(`Dialogue: 1,${formatAssTime(segStart)},${formatAssTime(segEnd)},Layer1,,0,0,0,,${segText}`)
      continue
    }

    const tokenTimeline = isQwen ? words : smoothWordTimeline(words, segStart, segEnd)
    if (!tokenTimeline || !tokenTimeline.length) {
      events.push(`Dialogue: 0,${formatAssTime(segStart)},${formatAssTime(segEnd)},Layer0,,0,0,0,,${segText}`)
      events.push(`Dialogue: 1,${formatAssTime(segStart)},${formatAssTime(segEnd)},Layer1,,0,0,0,,${segText}`)
      continue
    }

    const plainEvents = []
    const taggedEvents = []

    for (let i = 0; i < tokenTimeline.length; i++) {
      const tStr = String(tokenTimeline[i].text || '')
      const t1 = Math.floor((Number(tokenTimeline[i].start) - segStart) * 1000)
      const t2 = Math.floor((Number(tokenTimeline[i].end) - segStart) * 1000)

      const pieces = tokenizePieces(tStr)
      for (const p of pieces) {
        plainEvents.push(p.text)
        if (p.hg) {
          taggedEvents.push(`{\\c${baseColor}&\\t(${t1},${t1 + 1},\\c${hgColor}&)\\t(${t2},${t2 + 1},\\c${baseColor}&)}${p.text}`)
        } else {
          taggedEvents.push(`{\\c${baseColor}&}${p.text}`)
        }
      }

      if (i < tokenTimeline.length - 1) {
        const t1Char = tStr.slice(-1)
        const nextStr = String(tokenTimeline[i + 1].text || '')
        const t2Char = nextStr.charAt(0)

        const isAscii1 = /[A-Za-z0-9]/.test(t1Char)
        const isAscii2 = /[A-Za-z0-9]/.test(t2Char)
        const isCjk1 = /[\u3400-\u9fff]/.test(t1Char)
        const isCjk2 = /[\u3400-\u9fff]/.test(t2Char)
        if ((isAscii1 && isAscii2) || (isCjk1 && isAscii2) || (isAscii1 && isCjk2)) {
          plainEvents.push(' ')
          taggedEvents.push(`{\\c${baseColor}&} `)
        }
      }
    }
      events.push(`Dialogue: 0,${formatAssTime(segStart)},${formatAssTime(segEnd)},Layer0,,0,0,0,,${plainEvents.join('')}`)
      events.push(`Dialogue: 1,${formatAssTime(segStart)},${formatAssTime(segEnd)},Layer1,,0,0,0,,{\\c${baseColor}&}${taggedEvents.join('')}{\\c${baseColor}&}`)
  }

  return header + events.join('\n') + '\n'
}
