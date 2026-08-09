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
  persistRunSettingsNow,
  onMergedPreviewReady,
}) {
  const renderStopRequested = ref(false)
  let currentRenderAbortController = null
  const singleRenderQueue = ref([])
  const activeSingleRenderPage = ref(null)
  const renderingPageStatus = ref({})
  const activeBatchJobId = ref('')

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
    if (currentRunId.value && activeBatchJobId.value) {
      fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/jobs/${encodeURIComponent(activeBatchJobId.value)}/cancel`), {
        method: 'POST',
      }).catch(() => {})
    }
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
    formData.append('selected_voice_key', String(selectedVoiceKey?.value || ''))
    if (cloneAudioFile.value) formData.append('reference_audio', cloneAudioFile.value)
    if (currentRunId.value) formData.append('response_mode', 'json')

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
    if (currentRunId.value) {
      const data = await res.json().catch(() => ({}))
      const variantId = String(data?.variant_id || data?.tts_id || '')
      if (!variantId) throw new Error('TTS 已完成，但後端未回傳變體 ID')
      return {
        persistent: true,
        ttsId: String(data?.tts_id || variantId),
        variantId,
        audioUrl: String(data?.audio_url || ''),
      }
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
    if (audioFile instanceof Blob) formData.append('audio_file', audioFile)
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
      warning: String(data?.warning || ''),
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
    let imgBlob = null
    if (!currentRunId.value) {
      currentRenderAbortController = new AbortController()
      const imgResp = await fetch(slide.thumbnailUrl, { signal: currentRenderAbortController.signal })
      imgBlob = await imgResp.blob()
    }

    const token = localStorage.getItem('token')
    const formData = new FormData()
    if (audioFile instanceof Blob) formData.append('audio_file', audioFile)
    if (imgBlob) formData.append('slide_image', imgBlob, `slide_${slideIdx + 1}.png`)
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
      renderMessage.value = aligned.warning
        ? `第 ${idx + 1} 頁渲染完成，但對齊可信度偏低，建議試聽確認。`
        : `第 ${idx + 1} 頁渲染完成。`
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

  const regenerateTtsChunk = async ({ variantId, chunkIndex, text }) => {
    if (!currentRunId.value || !variantId || rendering.value || renderingAll.value) return
    const pageIdx = selectedSlideIndex.value
    const scriptText = String(slides.value[pageIdx]?.scriptText || '').trim()
    rendering.value = true
    renderStopRequested.value = false
    renderingPageStatus.value = { ...renderingPageStatus.value, [pageIdx]: 'running' }
    try {
      renderMessage.value = `第 ${pageIdx + 1} 頁：重生第 ${Number(chunkIndex) + 1} 段語音...`
      const formData = new FormData()
      formData.append('text', String(text || '').trim())
      const res = await fetch(getApiEndpoint(
        `/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${pageIdx}/variants/${encodeURIComponent(variantId)}/chunks/${Number(chunkIndex)}/regenerate`,
      ), { method: 'POST', body: formData })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `局部語音重生失敗 (${res.status})`)
      const nextVariantId = String(data?.variant_id || '')
      if (!nextVariantId) throw new Error('局部語音重生後未取得變體 ID')
      const audio = {
        persistent: true,
        ttsId: String(data?.tts_id || nextVariantId),
        variantId: nextVariantId,
        audioUrl: String(data?.audio_url || ''),
      }
      const outputMode = subtitleOutputMode?.value || 'burn'
      const aligned = outputMode === 'none'
        ? { segments: [], backend: '', alignId: '', variantId: nextVariantId, warning: '' }
        : await alignSubtitleForAudio(audio, scriptText, pageIdx)
      await renderAssVideoFromPrepared(pageIdx, audio, aligned, outputMode)
      await refreshRunManifest({ applySelected: true })
      renderMessage.value = aligned.warning
        ? `第 ${pageIdx + 1} 頁局部重生完成，但對齊可信度偏低，建議試聽。`
        : `第 ${pageIdx + 1} 頁第 ${Number(chunkIndex) + 1} 段已重生並重新渲染。`
    } catch (error) {
      renderMessage.value = error?.message || '局部語音重生失敗'
    } finally {
      renderingPageStatus.value = { ...renderingPageStatus.value, [pageIdx]: '' }
      rendering.value = false
      currentRenderAbortController = null
    }
  }

  const waitForBackendBatchJob = async (jobId, pagesToRender) => {
    const stageLabels = { queued: '排隊', tts: 'TTS', alignment: '字幕對齊', render: '影片渲染' }
    while (true) {
      const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/jobs/${encodeURIComponent(jobId)}`))
      const job = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(job?.detail || `讀取批次任務失敗 (${res.status})`)
      const pageStates = job?.pages || {}
      const nextStatus = { ...renderingPageStatus.value }
      for (const pageIdx of pagesToRender) {
        const status = pageStates[String(pageIdx)]?.status || ''
        nextStatus[pageIdx] = status === 'rendered' ? '' : (status ? 'running' : '')
      }
      renderingPageStatus.value = nextStatus
      const stage = String(job?.stage || 'queued')
      const progress = Number(job?.stage_total || 0)
        ? ` ${Number(job?.stage_index || 0)}/${Number(job.stage_total)}`
        : ''
      const currentPage = Number.isInteger(job?.current_page_index)
        ? `（第 ${Number(job.current_page_index) + 1} 頁）`
        : ''
      const queue = job?.queue || {}
      if (job?.status === 'queued' || queue?.queue_state === 'queued') {
        const ahead = Number(queue?.jobs_ahead || 0)
        const position = Number(queue?.queue_position || 0)
        const active = queue?.active || null
        const activeStage = active
          ? `${stageLabels[String(active.stage || '')] || active.stage || '處理中'}${Number(active.stage_total || 0) ? ` ${Number(active.stage_index || 0)}/${Number(active.stage_total)}` : ''}`
          : '準備切換任務'
        renderMessage.value = `正在等待其他任務：前方 ${ahead} 個${position ? `（等待序號 ${position}）` : ''}；目前工作站：${activeStage}。`
      } else {
        renderMessage.value = `${stageLabels[stage] || stage}${progress}${currentPage}：後端任務執行中，可重新整理後續跑。`
      }
      if (job?.status === 'completed') return job
      if (job?.status === 'cancelled') throw Object.assign(new Error('批次渲染已終止。'), { name: 'AbortError' })
      if (job?.status === 'failed') throw new Error(job?.error || '後端批次渲染失敗')
      await new Promise((resolve) => setTimeout(resolve, 1500))
    }
  }

  const renderAllPagesWithBackendJob = async (pagesToRender) => {
    const outputMode = subtitleOutputMode?.value || 'burn'
    if (typeof persistRunSettingsNow === 'function') {
      const saved = await persistRunSettingsNow({ includeReferenceAudio: Boolean(cloneAudioFile.value) })
      if (saved === false) throw new Error('語音設定尚未成功保存，無法開始批次渲染。')
    }
    const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/jobs/render`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page_indexes: pagesToRender,
        subtitle_mode: outputMode,
        split_min_chars: splitMinChars,
        split_max_chars: splitMaxChars,
        tts_voice: String(globalSettings.value.tts.voice || ''),
        tts_speed: Number(globalSettings.value.tts.speed || 1),
        selected_voice_key: String(selectedVoiceKey?.value || ''),
        reference_text: String(referenceText.value || ''),
        subtitle_settings: {
          enable_highlight: Boolean(enableSubtitleHighlight.value),
          font_size: Number(globalSettings.value.subtitle.fontSize ?? 52),
          enable_background: Boolean(globalSettings.value.subtitle.enableBackground),
          bg_color: String(globalSettings.value.subtitle.bgColor || '#000000'),
          bg_opacity: Number(globalSettings.value.subtitle.bgOpacity || 55),
          margin_v: Number(globalSettings.value.subtitle.marginV ?? 90),
        },
      }),
    })
    const job = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(job?.detail || `建立批次任務失敗 (${res.status})`)
    activeBatchJobId.value = String(job?.job_id || '')
    if (!activeBatchJobId.value) throw new Error('後端未回傳批次任務 ID')
    const completed = await waitForBackendBatchJob(activeBatchJobId.value, pagesToRender)
    await refreshRunManifest({ applySelected: true })
    const warnings = Object.entries(completed?.pages || {})
      .filter(([, state]) => state?.warning)
      .map(([index]) => Number(index) + 1)
    renderMessage.value = warnings.length
      ? `全部渲染完成；第 ${warnings.join('、')} 頁對齊可信度偏低，建議試聽確認。`
      : `全部渲染完成（${pagesToRender.length} 頁）。`
  }

  const reattachActiveBatchJob = async () => {
    if (!currentRunId.value || renderingAll.value) return false
    const res = await fetch(getApiEndpoint(
      `/api/video-runs/${encodeURIComponent(currentRunId.value)}/jobs-current`,
    ))
    if (res.status === 204) return false
    const job = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(job?.detail || `讀取進行中任務失敗 (${res.status})`)
    const jobId = String(job?.job_id || '')
    if (!jobId) return false
    const requested = Array.isArray(job?.payload?.page_indexes)
      ? job.payload.page_indexes.map(Number).filter(Number.isInteger)
      : renderablePageIndexes.value
    activeBatchJobId.value = jobId
    renderingAll.value = true
    renderStopRequested.value = false
    try {
      await waitForBackendBatchJob(jobId, requested)
      await refreshRunManifest({ applySelected: true })
      renderMessage.value = `已接續並完成批次渲染（${requested.length} 頁）。`
    } catch (error) {
      if (error?.name !== 'AbortError') renderMessage.value = error?.message || '批次渲染失敗'
    } finally {
      activeBatchJobId.value = ''
      renderingAll.value = false
    }
    return true
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
    const outputMode = subtitleOutputMode?.value || 'burn'
    const preparedAudio = new Map()
    const preparedAlignment = new Map()
    const alignmentWarnings = []

    const markOnlyCurrentPageRunning = (pageIdx = null) => {
      const next = { ...renderingPageStatus.value }
      for (const idx of pagesToRender) {
        if (next[idx] === 'running') next[idx] = ''
      }
      if (Number.isInteger(pageIdx)) next[pageIdx] = 'running'
      renderingPageStatus.value = next
    }

    try {
      if (currentRunId.value) {
        await renderAllPagesWithBackendJob(pagesToRender)
        return
      }
      // Keep one GPU model resident for a whole batch.  The previous page-wise
      // TTS -> alignment -> render loop repeatedly unloaded Nano VoxCPM and the
      // Qwen aligner.  Running the batch in stages preserves the same peak-VRAM
      // behaviour while reducing model transitions from roughly 2 * pages to 2.
      renderMessage.value = `TTS 階段 0/${total}：準備語音模型...`
      for (let n = 0; n < total; n += 1) {
        ensureRenderNotStopped()
        const i = pagesToRender[n]
        const s = String(slides.value[i]?.scriptText || '').trim()
        markOnlyCurrentPageRunning(i)
        renderMessage.value = `TTS 階段 ${n + 1}/${total}（第 ${i + 1} 頁）`
        const audioFile = await createAudioFromTtsPreview(s, i)
        preparedAudio.set(i, audioFile)
      }

      markOnlyCurrentPageRunning()
      if (outputMode !== 'none') {
        renderMessage.value = `字幕對齊階段 0/${total}：切換強制對齊模型...`
        for (let n = 0; n < total; n += 1) {
          ensureRenderNotStopped()
          const i = pagesToRender[n]
          const s = String(slides.value[i]?.scriptText || '').trim()
          const audioFile = preparedAudio.get(i)
          if (!audioFile) throw new Error(`第 ${i + 1} 頁缺少已生成音訊`)
          markOnlyCurrentPageRunning(i)
          renderMessage.value = `字幕對齊階段 ${n + 1}/${total}（第 ${i + 1} 頁）`
          const aligned = await alignSubtitleForAudio(audioFile, s, i)
          preparedAlignment.set(i, aligned)
          if (aligned.warning) alignmentWarnings.push(i)
        }
      } else {
        for (const i of pagesToRender) {
          const audioFile = preparedAudio.get(i)
          preparedAlignment.set(i, {
            segments: [],
            backend: '',
            alignId: '',
            variantId: String(audioFile?.variantId || ''),
          })
        }
      }

      markOnlyCurrentPageRunning()
      for (let n = 0; n < total; n += 1) {
        ensureRenderNotStopped()
        const i = pagesToRender[n]
        const audioFile = preparedAudio.get(i)
        const aligned = preparedAlignment.get(i)
        if (!audioFile || !aligned) throw new Error(`第 ${i + 1} 頁缺少批次渲染資料`)
        selectedSlideIndex.value = i
        const outputLabel = outputMode === 'burn'
          ? 'ASS 字幕渲染'
          : (outputMode === 'sidecar' ? 'SRT 影片輸出' : '無字幕影片輸出')
        renderMessage.value = `${outputLabel}階段 ${n + 1}/${total}（第 ${i + 1} 頁）`
        await renderAssVideoFromPrepared(i, audioFile, aligned, outputMode)
        preparedAudio.delete(i)
        preparedAlignment.delete(i)
      }
      const warningSuffix = alignmentWarnings.length
        ? `；第 ${alignmentWarnings.map((i) => i + 1).join('、')} 頁對齊可信度偏低，建議試聽確認`
        : ''
      renderMessage.value = (skipped ? `全部渲染完成（${total} 頁，跳過 ${skipped} 頁空講稿）` : `全部渲染完成（${total} 頁）`) + warningSuffix
    } catch (err) {
      renderMessage.value = err?.name === 'AbortError' ? '批次渲染已終止。' : (err.message || '全部渲染失敗')
      Object.keys(renderingPageStatus.value || {}).forEach((k) => {
        if (renderingPageStatus.value[k] === 'running') renderingPageStatus.value[k] = ''
      })
    } finally {
      preparedAudio.clear()
      preparedAlignment.clear()
      currentRenderAbortController = null
      activeBatchJobId.value = ''
      renderingAll.value = false
      if (singleRenderQueue.value.length) await processSingleRenderQueue()
    }
  }

  const mergeAndDownloadRenderedVideos = async (transitionsEnabled = false) => {
    const transitionLabel = transitionsEnabled ? '，並加入系統隨機轉場' : ''
    const ok = window.confirm(`將依目前頁序合併已渲染影片${transitionLabel}（未渲染頁會跳過），並直接下載。確定執行？`)
    if (!ok) return
    const pageIndexes = slides.value.map((_, i) => i).filter((i) => !!renderedPageVideos.value[i])
    if (!pageIndexes.length) {
      renderMessage.value = '沒有可合併的已渲染影片。'
      return
    }
    try {
      renderMessage.value = `合併匯出中（${pageIndexes.length} 段）...`
      const formData = new FormData()
      formData.append('transitions_enabled', transitionsEnabled ? 'true' : 'false')
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
    activeBatchJobId,
    cancellableSinglePages,
    renderablePageIndexes,
    requestStopAllRendering,
    requestStopPage,
    renderCurrentPage,
    renderAllPages,
    reattachActiveBatchJob,
    regenerateTtsChunk,
    mergeAndDownloadRenderedVideos,
  }
}
