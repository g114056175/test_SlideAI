import { computed } from 'vue'

export function useVideoRuns({
  currentRunId,
  runManifest,
  selectedVariantIds,
  renderedPageVideos,
  selectedSlideIndex,
  getApiEndpoint,
  renderMessage,
}) {
  const runPages = computed(() => Array.isArray(runManifest.value?.pages) ? runManifest.value.pages : [])
  const selectedRunPage = computed(() => runPages.value[selectedSlideIndex.value] || null)
  const selectedPageVariants = computed(() => Array.isArray(selectedRunPage.value?.variants) ? selectedRunPage.value.variants : [])
  const selectedPageVariantId = computed(() => selectedVariantIds.value[selectedSlideIndex.value] || selectedRunPage.value?.selected_variant_id || '')
  const variantCountsByPage = computed(() => {
    const out = {}
    runPages.value.forEach((page, idx) => {
      out[idx] = Array.isArray(page?.variants) ? page.variants.length : 0
    })
    return out
  })
  const exportVariants = computed(() => Array.isArray(runManifest.value?.exports?.variants) ? runManifest.value.exports.variants : [])
  const selectedExportVariantId = computed(() => runManifest.value?.exports?.selected_variant_id || exportVariants.value[exportVariants.value.length - 1]?.variant_id || '')

  const variantVideoUrl = (pageIdx, variantId) => {
    if (!currentRunId.value || !variantId) return ''
    return getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${pageIdx}/variants/${encodeURIComponent(variantId)}/video`)
  }

  const variantSrtUrl = (pageIdx, variantId) => {
    if (!currentRunId.value || !variantId) return ''
    return getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${pageIdx}/variants/${encodeURIComponent(variantId)}/subtitles.srt`)
  }

  const variantBundleUrl = (pageIdx, variantId) => {
    if (!currentRunId.value || !variantId) return ''
    return getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${pageIdx}/variants/${encodeURIComponent(variantId)}/download.zip`)
  }

  const exportVideoUrl = (variantId) => {
    if (!currentRunId.value || !variantId) return ''
    return getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/exports/${encodeURIComponent(variantId)}/video`)
  }

  const exportSrtUrl = (variantId) => {
    if (!currentRunId.value || !variantId) return ''
    return getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/exports/${encodeURIComponent(variantId)}/subtitles.srt`)
  }

  const exportBundleUrl = (variantId) => {
    if (!currentRunId.value || !variantId) return ''
    return getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/exports/${encodeURIComponent(variantId)}/download.zip`)
  }

  const refreshRunManifest = async ({ applySelected = false } = {}) => {
    if (!currentRunId.value) return
    try {
      const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}`))
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `run manifest failed (${res.status})`)
      runManifest.value = data
      const nextSelected = {}
      const nextVideos = { ...renderedPageVideos.value }
      ;(data.pages || []).forEach((page, idx) => {
        const selected = page.selected_variant_id || page.variants?.[page.variants.length - 1]?.variant_id || ''
        if (selected) {
          nextSelected[idx] = selected
          if (applySelected || !nextVideos[idx]) nextVideos[idx] = variantVideoUrl(idx, selected)
        }
      })
      selectedVariantIds.value = { ...selectedVariantIds.value, ...nextSelected }
      if (applySelected) renderedPageVideos.value = nextVideos
    } catch (err) {
      console.warn('[VideoRun] manifest refresh failed:', err)
    }
  }

  const selectPageVariant = async (variantId) => {
    if (!currentRunId.value || !variantId) return
    const idx = selectedSlideIndex.value
    try {
      const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${idx}/variants/${encodeURIComponent(variantId)}/select`), {
        method: 'POST',
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `選定影片失敗 (${res.status})`)
      selectedVariantIds.value = { ...selectedVariantIds.value, [idx]: variantId }
      const prevUrl = renderedPageVideos.value[idx]
      if (prevUrl && prevUrl.startsWith('blob:')) {
        try { URL.revokeObjectURL(prevUrl) } catch {}
      }
      renderedPageVideos.value = { ...renderedPageVideos.value, [idx]: variantVideoUrl(idx, variantId) }
      await refreshRunManifest()
    } catch (err) {
      renderMessage.value = err.message || '選定影片失敗'
    }
  }

  const deletePageVariant = async (variantId) => {
    if (!currentRunId.value || !variantId) return
    const idx = selectedSlideIndex.value
    const ok = window.confirm(`確定刪除第 ${idx + 1} 頁的此產出影片？`)
    if (!ok) return
    try {
      const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/pages/${idx}/variants/${encodeURIComponent(variantId)}`), {
        method: 'DELETE',
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `刪除影片失敗 (${res.status})`)
      if (selectedVariantIds.value[idx] === variantId) {
        const next = data.selected_variant_id || data.variants?.[data.variants.length - 1]?.variant_id || ''
        selectedVariantIds.value = { ...selectedVariantIds.value, [idx]: next }
        if (next) {
          renderedPageVideos.value = { ...renderedPageVideos.value, [idx]: variantVideoUrl(idx, next) }
        } else {
          const copy = { ...renderedPageVideos.value }
          delete copy[idx]
          renderedPageVideos.value = copy
        }
      }
      await refreshRunManifest()
    } catch (err) {
      renderMessage.value = err.message || '刪除影片失敗'
    }
  }

  const selectExportVariant = async (variantId) => {
    if (!currentRunId.value || !variantId) return
    try {
      const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/exports/${encodeURIComponent(variantId)}/select`), {
        method: 'POST',
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `選定合併影片失敗 (${res.status})`)
      runManifest.value = { ...runManifest.value, exports: data }
      await refreshRunManifest()
    } catch (err) {
      renderMessage.value = err.message || '選定合併影片失敗'
    }
  }

  const deleteExportVariant = async (variantId) => {
    if (!currentRunId.value || !variantId) return
    const ok = window.confirm('確定刪除此合併匯出影片？')
    if (!ok) return
    try {
      const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/exports/${encodeURIComponent(variantId)}`), {
        method: 'DELETE',
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `刪除合併影片失敗 (${res.status})`)
      runManifest.value = { ...runManifest.value, exports: data }
      await refreshRunManifest()
    } catch (err) {
      renderMessage.value = err.message || '刪除合併影片失敗'
    }
  }

  return {
    runPages,
    selectedRunPage,
    selectedPageVariants,
    selectedPageVariantId,
    variantCountsByPage,
    exportVariants,
    selectedExportVariantId,
    variantVideoUrl,
    variantSrtUrl,
    variantBundleUrl,
    exportVideoUrl,
    exportSrtUrl,
    exportBundleUrl,
    refreshRunManifest,
    selectPageVariant,
    deletePageVariant,
    selectExportVariant,
    deleteExportVariant,
  }
}
