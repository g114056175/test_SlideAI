import { computed, ref } from 'vue'

export function useRenderQueue({
  slides,
  selectedSlide,
  selectedSlideIndex,
  renderedPageVideos,
  rendering,
  renderingAll,
  renderMessage,
  currentRunId,
  globalSettings,
  subtitleOutputMode,
  referenceText,
  cloneAudioFile,
  selectedVoiceKey,
  enableSubtitleHighlight,
  selectedVariantIds,
  refreshRunManifest,
  getMergedDownloadFilename,
  getApiEndpoint,
  emitter,
  splitMinChars,
  splitMaxChars,
  onMergedPreviewReady,
}) {
  const renderStopRequested = ref(false)
  let currentRenderAbortController = null
  const singleRenderQueue = ref([])
  const activeSingleRenderPage = ref(null)
  const renderingPageStatus = ref({})

  const cancellableSinglePages = computed(() => {
    const pages = []
    if (Number.isInteger(activeSingleRenderPage.value)) pages.push(activeSingleRenderPage.value)
    for (const pageIdx of singleRenderQueue.value) {
      if (!pages.includes(pageIdx)) pages.push(pageIdx)
    }
    return pages.slice(0, 3)
  })

  const renderablePageIndexes = computed(() => slides.value
    .map((slide, idx) => ({ idx, script: String(slide?.scriptText || '').trim() }))
    .filter((item) => item.script)
    .map((item) => item.idx))

  const ensureRenderNotStopped = () => {
    if (renderStopRequested.value) throw new Error('渲染已由使用者終止')
  }

  const requestStopAllRendering = () => {
    const ok = window.confirm('確定要終止目前渲染流程嗎？')
    if (!ok) return
    renderStopRequested.value = true
    singleRenderQueue.value = []
    try { currentRenderAbortController?.abort() } catch {}
  }

  const requestStopPage = (pageIdx) => {
    const ok = window.confirm(`確定要終止第 ${pageIdx + 1} 頁渲染嗎？`)
    if (!ok) return
    if (activeSingleRenderPage.value === pageIdx && rendering.value) {
      renderStopRequested.value = true
      try { currentRenderAbortController?.abort() } catch {}
      return
    }
    singleRenderQueue.value = singleRenderQueue.value.filter((x) => x !== pageIdx)
  }

  const createAudioFromTtsPreview = async (text, slideIdx = selectedSlideIndex.value) => {
    ensureRenderNotStopped()
    const token = localStorage.getItem('token')
    const formData = new FormData()
    formData.append('text', String(text || '').trim())
    formData.append('voice', globalSettings.value.tts.voice)
    formData.append('speed', String(globalSettings.value.tts.speed))
    formData.append('reference_text', referenceText.value.trim())
    if (cloneAudioFile.value) formData.append('reference_audio', cloneAudioFile.value)

    currentRenderAbortController = new AbortController()
    const endpoint = currentRunId.value
      ? getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${slideIdx}/tts`)
      : getApiEndpoint('/api/video-abstract/tts-preview')
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: formData,
      signal: currentRenderAbortController.signal,
    })
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}))
      throw new Error(errData?.detail || `TTS 生成失敗 (${res.status})`)
    }
    const audioBlob = await res.blob()
    const audioFile = new File([audioBlob], `page_${slideIdx + 1}_tts.wav`, { type: audioBlob.type || 'audio/wav' })
    audioFile.ttsId = res.headers.get('X-TTS-Id') || ''
    audioFile.variantId = res.headers.get('X-Variant-Id') || audioFile.ttsId
    return audioFile
  }

  const alignSubtitleForAudio = async (audioFile, text, slideIdx = selectedSlideIndex.value) => {
    ensureRenderNotStopped()
    const token = localStorage.getItem('token')
    const formData = new FormData()
    formData.append('audio_file', audioFile)
    formData.append('text', String(text || '').trim())
    formData.append('split_min_chars', String(splitMinChars))
    formData.append('split_max_chars', String(splitMaxChars))
    if (audioFile?.ttsId) formData.append('tts_id', audioFile.ttsId)
    if (audioFile?.variantId) formData.append('variant_id', audioFile.variantId)
    currentRenderAbortController = new AbortController()
    const endpoint = currentRunId.value
      ? getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${slideIdx}/align`)
      : getApiEndpoint('/api/video-abstract/subtitle-align')
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: formData,
      signal: currentRenderAbortController.signal,
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `字幕對齊失敗 (${res.status})`)
    return {
      segments: Array.isArray(data?.segments) ? data.segments : [],
      backend: String(data?.backend || ''),
      alignId: String(data?.align_id || res.headers.get('X-Align-Id') || ''),
      variantId: String(data?.variant_id || res.headers.get('X-Variant-Id') || audioFile?.variantId || ''),
    }
  }

  const renderAssVideoFromPrepared = async (slideIdx, audioFile, aligned, outputMode = 'burn') => {
    ensureRenderNotStopped()
    const slide = slides.value[slideIdx]
    if (!slide) throw new Error('找不到指定投影片')
    if (!slide.thumbnailUrl) throw new Error(`第 ${slideIdx + 1} 頁尚無縮圖，請稍後再試`)
    if (outputMode === 'burn' && !aligned.segments.length) throw new Error(`第 ${slideIdx + 1} 頁字幕對齊結果為空`)
    renderingPageStatus.value = { ...renderingPageStatus.value, [slideIdx]: 'running' }

    renderMessage.value = `第 ${slideIdx + 1} 頁：讀取背景圖...`
    currentRenderAbortController = new AbortController()
    const imgResp = await fetch(slide.thumbnailUrl, { signal: currentRenderAbortController.signal })
    const imgBlob = await imgResp.blob()

    const token = localStorage.getItem('token')
    const formData = new FormData()
    formData.append('audio_file', audioFile)
    formData.append('slide_image', imgBlob, `slide_${slideIdx + 1}.png`)
    formData.append('segments_json', JSON.stringify(aligned.segments))
    formData.append('subtitle_mode', outputMode)
    formData.append('subtitle_style', 'bg-dark')
    formData.append('enable_highlight', String(enableSubtitleHighlight.value))
    formData.append('font_size', String(Number(globalSettings.value.subtitle.fontSize ?? 52)))
    formData.append('text_color', String(globalSettings.value.subtitle.color || '#ffffff'))
    formData.append('active_word_color', String(globalSettings.value.subtitle.activeWordColor || '#facc15'))
    formData.append('enable_background', String(Boolean(globalSettings.value.subtitle.enableBackground)))
    formData.append('bg_color', String(globalSettings.value.subtitle.bgColor || '#000000'))
    formData.append('bg_opacity', String(Number(globalSettings.value.subtitle.bgOpacity || 55)))
    formData.append('margin_v', String(Number(globalSettings.value.subtitle.marginV ?? 90)))
    formData.append('enable_outline', String(Boolean(globalSettings.value.subtitle.enableOutline)))
    formData.append('outline_color', String(globalSettings.value.subtitle.outlineColor || '#000000'))
    formData.append('outline_width', String(Number(globalSettings.value.subtitle.outlineWidth ?? 2)))
    formData.append('tts_voice', String(globalSettings.value.tts.voice || ''))
    formData.append('tts_speed', String(globalSettings.value.tts.speed ?? 1))
    formData.append('selected_voice_key', String(selectedVoiceKey?.value || ''))
    formData.append('reference_text', String(referenceText.value || ''))
    formData.append('align_backend', aligned.backend || '')
    if (audioFile?.ttsId) formData.append('tts_id', audioFile.ttsId)
    if (aligned?.alignId) formData.append('align_id', aligned.alignId)
    if (aligned?.variantId || audioFile?.variantId) formData.append('variant_id', aligned?.variantId || audioFile?.variantId)
    if (currentRunId.value) {
      formData.append('run_id', currentRunId.value)
      formData.append('page_index', String(slideIdx))
      formData.append('variant_label', `web-page-${slideIdx + 1}`)
    }

    renderMessage.value = `第 ${slideIdx + 1} 頁：ASS 影片渲染中...`
    currentRenderAbortController = new AbortController()
    const res = await fetch(getApiEndpoint('/api/video-abstract/render-subtitle-ass-video'), {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: formData,
      signal: currentRenderAbortController.signal,
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err?.detail || `第 ${slideIdx + 1} 頁影片渲染失敗 (${res.status})`)
    }

    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const variantId = res.headers.get('X-Variant-Id') || ''
    const prevUrl = renderedPageVideos.value[slideIdx]
    if (prevUrl && prevUrl.startsWith('blob:')) {
      try { URL.revokeObjectURL(prevUrl) } catch {}
    }
    renderedPageVideos.value = { ...renderedPageVideos.value, [slideIdx]: url }
    if (variantId) {
      selectedVariantIds.value = { ...selectedVariantIds.value, [slideIdx]: variantId }
      await refreshRunManifest()
      emitter.emit('refresh-video-runs')
    }
    renderingPageStatus.value = { ...renderingPageStatus.value, [slideIdx]: '' }
  }

  const processSingleRenderQueue = async () => {
    if (renderingAll.value || rendering.value) return
    const nextPage = singleRenderQueue.value.shift()
    if (nextPage == null) return
    selectedSlideIndex.value = nextPage
    await startSingleRender(nextPage)
  }

  const startSingleRender = async (idx) => {
    rendering.value = true
    activeSingleRenderPage.value = idx
    renderingPageStatus.value = { ...renderingPageStatus.value, [idx]: 'running' }
    renderStopRequested.value = false
    renderMessage.value = `第 ${idx + 1} 頁：準備渲染...`
    try {
      const slide = slides.value[idx]
      const scriptText = String(slide?.scriptText || '').trim()
      if (!scriptText) throw new Error(`第 ${idx + 1} 頁講稿為空，無法渲染`)
      renderMessage.value = `第 ${idx + 1} 頁：TTS 生成中...`
      const audioFile = await createAudioFromTtsPreview(scriptText, idx)
      const outputMode = subtitleOutputMode?.value || 'burn'
      const aligned = outputMode === 'none'
        ? { segments: [], backend: '', alignId: '', variantId: String(audioFile?.variantId || '') }
        : await alignSubtitleForAudio(audioFile, scriptText, idx)
      if (outputMode !== 'none') renderMessage.value = `第 ${idx + 1} 頁：字幕對齊完成，準備輸出...`
      await renderAssVideoFromPrepared(idx, audioFile, aligned, outputMode)
      renderMessage.value = `第 ${idx + 1} 頁渲染完成。`
    } catch (err) {
      renderMessage.value = err?.name === 'AbortError' ? `第 ${idx + 1} 頁已終止。` : (err.message || '渲染失敗')
      renderingPageStatus.value = { ...renderingPageStatus.value, [idx]: '' }
    } finally {
      currentRenderAbortController = null
      rendering.value = false
      activeSingleRenderPage.value = null
      if (!renderingAll.value) await processSingleRenderQueue()
    }
  }

  const renderCurrentPage = async () => {
    if (!selectedSlide.value) {
      renderMessage.value = '請先選擇頁面。'
      return
    }
    const idx = selectedSlideIndex.value
    if (rendering.value || renderingAll.value) {
      if (activeSingleRenderPage.value === idx || singleRenderQueue.value.includes(idx)) {
        renderMessage.value = `第 ${idx + 1} 頁已在渲染/排隊中。`
        return
      }
      if (singleRenderQueue.value.length >= 3) {
        renderMessage.value = '單頁排隊最多 3 個，請先終止部分排隊。'
        return
      }
      singleRenderQueue.value.push(idx)
      renderMessage.value = `第 ${idx + 1} 頁已加入排隊。`
      return
    }
    await startSingleRender(idx)
  }

  const renderAllPages = async () => {
    if (!slides.value.length) return
    const pagesToRender = renderablePageIndexes.value
    if (!pagesToRender.length) {
      renderMessage.value = '沒有可渲染頁面：所有講稿皆為空。'
      return
    }
    if (rendering.value) {
      const active = Number.isInteger(activeSingleRenderPage.value) ? activeSingleRenderPage.value : null
      const nextQueue = [...singleRenderQueue.value]
      for (const idx of pagesToRender) {
        if (idx === active || nextQueue.includes(idx)) continue
        nextQueue.push(idx)
      }
      singleRenderQueue.value = nextQueue
      renderMessage.value = `已加入渲染全部 queue（共 ${pagesToRender.length} 個有講稿頁面，空講稿頁已跳過）。`
      return
    }
    if (renderingAll.value) {
      renderMessage.value = '批次渲染已在進行中。'
      return
    }
    renderingAll.value = true
    renderStopRequested.value = false
    singleRenderQueue.value = []
    const skipped = slides.value.length - pagesToRender.length
    renderMessage.value = skipped ? `開始批次渲染：${pagesToRender.length} 頁，跳過 ${skipped} 頁空講稿。` : '開始批次渲染...'
    const total = pagesToRender.length
    try {
      for (let n = 0; n < total; n += 1) {
        ensureRenderNotStopped()
        const i = pagesToRender[n]
        const s = String(slides.value[i]?.scriptText || '').trim()
        renderingPageStatus.value = { ...renderingPageStatus.value, [i]: 'running' }
        renderMessage.value = `TTS ${n + 1}/${total}（第 ${i + 1} 頁）`
        const audioFile = await createAudioFromTtsPreview(s, i)
        ensureRenderNotStopped()
        const outputMode = subtitleOutputMode?.value || 'burn'
        const aligned = outputMode === 'none'
          ? { segments: [], backend: '', alignId: '', variantId: String(audioFile?.variantId || '') }
          : await alignSubtitleForAudio(audioFile, s, i)
        ensureRenderNotStopped()
        selectedSlideIndex.value = i
        renderMessage.value = `${outputMode === 'burn' ? 'ASS 字幕渲染' : '無字幕影片輸出'} ${n + 1}/${total}（第 ${i + 1} 頁）`
        await renderAssVideoFromPrepared(i, audioFile, aligned, outputMode)
      }
      renderMessage.value = skipped ? `全部渲染完成（${total} 頁，跳過 ${skipped} 頁空講稿）` : `全部渲染完成（${total} 頁）`
    } catch (err) {
      renderMessage.value = err?.name === 'AbortError' ? '批次渲染已終止。' : (err.message || '全部渲染失敗')
      Object.keys(renderingPageStatus.value || {}).forEach((k) => {
        if (renderingPageStatus.value[k] === 'running') renderingPageStatus.value[k] = ''
      })
    } finally {
      currentRenderAbortController = null
      renderingAll.value = false
      if (singleRenderQueue.value.length) await processSingleRenderQueue()
    }
  }

  const mergeAndDownloadRenderedVideos = async () => {
    const ok = window.confirm('將依目前頁序合併已渲染影片（未渲染頁會跳過），並直接下載。確定執行？')
    if (!ok) return
    const pageIndexes = slides.value.map((_, i) => i).filter((i) => !!renderedPageVideos.value[i])
    if (!pageIndexes.length) {
      renderMessage.value = '沒有可合併的已渲染影片。'
      return
    }
    try {
      renderMessage.value = `合併匯出中（${pageIndexes.length} 段）...`
      const formData = new FormData()
      let mergeUrl = getApiEndpoint('/api/video-abstract/merge-rendered-videos')
      if (currentRunId.value) {
        mergeUrl = getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/exports/merge-selected`)
        formData.append('run_id', currentRunId.value)
        formData.append('page_indexes_json', JSON.stringify(pageIndexes))
        formData.append('variant_ids_json', JSON.stringify(selectedVariantIds.value || {}))
        formData.append('response_mode', 'video')
      } else {
        for (const idx of pageIndexes) {
          const url = renderedPageVideos.value[idx]
          const resp = await fetch(url)
          if (!resp.ok) throw new Error(`第 ${idx + 1} 頁影片讀取失敗 (${resp.status})`)
          const blob = await resp.blob()
          formData.append('videos', new File([blob], `page_${idx + 1}.mp4`, { type: 'video/mp4' }))
        }
      }
      const token = localStorage.getItem('token')
      const res = await fetch(mergeUrl, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: formData,
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err?.detail || `合併失敗 (${res.status})`)
      }
      const blob = await res.blob()
      const exportVariantId = res.headers.get('X-Export-Variant-Id') || ''
      const previewUrl = URL.createObjectURL(blob)
      if (typeof onMergedPreviewReady === 'function') {
        onMergedPreviewReady(previewUrl, exportVariantId)
      }
      const a = document.createElement('a')
      a.href = previewUrl
      a.download = typeof getMergedDownloadFilename === 'function'
        ? getMergedDownloadFilename()
        : 'merged_rendered_preview.mp4'
      a.click()
      if (currentRunId.value && exportVariantId) await refreshRunManifest()
      renderMessage.value = '合併匯出完成。'
    } catch (e) {
      renderMessage.value = e.message || '合併匯出失敗'
    }
  }

  return {
    singleRenderQueue,
    activeSingleRenderPage,
    renderingPageStatus,
    cancellableSinglePages,
    renderablePageIndexes,
    requestStopAllRendering,
    requestStopPage,
    renderCurrentPage,
    renderAllPages,
    mergeAndDownloadRenderedVideos,
  }
}
