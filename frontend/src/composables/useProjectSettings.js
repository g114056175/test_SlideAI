import { ref } from 'vue'

const clampOutlineWidth = (value) => Math.max(0, Math.min(10, Number(value ?? 2)))

export function useProjectSettings({
  currentRunId,
  stage,
  runManifest,
  globalSettings,
  enableSubtitleHighlight,
  selectedVoiceKey,
  referenceText,
  cloneAudioFile,
  cloneAudioUrl,
  tempObjectUrls,
  renderMessage,
  getApiEndpoint,
  onVoiceKeyChange,
}) {
  const suppressProjectSettingsSave = ref(false)
  const pendingReferenceAudioUpload = ref(false)
  let projectSettingsSaveTimer = null

  const collectCurrentProjectSettings = () => ({
    font_size: Number(globalSettings.value.subtitle.fontSize ?? 52),
    text_color: String(globalSettings.value.subtitle.color || '#ffffff'),
    active_word_color: String(globalSettings.value.subtitle.activeWordColor || '#facc15'),
    enable_background: Boolean(globalSettings.value.subtitle.enableBackground),
    bg_color: String(globalSettings.value.subtitle.bgColor || '#000000'),
    bg_opacity: Number(globalSettings.value.subtitle.bgOpacity ?? 55),
    margin_v: Number(globalSettings.value.subtitle.marginV ?? 90),
    enable_highlight: Boolean(enableSubtitleHighlight.value),
    enable_outline: Boolean(globalSettings.value.subtitle.enableOutline),
    outline_color: String(globalSettings.value.subtitle.outlineColor || '#000000'),
    outline_width: clampOutlineWidth(globalSettings.value.subtitle.outlineWidth),
    subtitle_output_mode: String(globalSettings.value.subtitle.outputMode || 'burn'),
    tts_voice: String(globalSettings.value.tts.voice || ''),
    tts_speed: Number(globalSettings.value.tts.speed ?? 1),
    selected_voice_key: String(selectedVoiceKey.value || ''),
    reference_text: String(referenceText.value || ''),
    has_reference_audio: Boolean(cloneAudioFile.value),
  })

  const applyVariantSettingsToUi = (variant, { includeVoice = true } = {}) => {
    const settings = variant?.settings || {}
    if (!settings || Object.keys(settings).length === 0) return
    if (settings.font_size !== undefined) globalSettings.value.subtitle.fontSize = Number(settings.font_size) || globalSettings.value.subtitle.fontSize
    if (settings.text_color) globalSettings.value.subtitle.color = settings.text_color
    if (settings.active_word_color) globalSettings.value.subtitle.activeWordColor = settings.active_word_color
    if (settings.enable_background !== undefined) globalSettings.value.subtitle.enableBackground = Boolean(settings.enable_background)
    if (settings.bg_color) globalSettings.value.subtitle.bgColor = settings.bg_color
    if (settings.bg_opacity !== undefined) globalSettings.value.subtitle.bgOpacity = Number(settings.bg_opacity)
    if (settings.margin_v !== undefined) globalSettings.value.subtitle.marginV = Number(settings.margin_v)
    if (settings.enable_highlight !== undefined) enableSubtitleHighlight.value = Boolean(settings.enable_highlight)
    if (settings.enable_outline !== undefined) globalSettings.value.subtitle.enableOutline = Boolean(settings.enable_outline)
    if (settings.outline_color) globalSettings.value.subtitle.outlineColor = settings.outline_color
    if (settings.outline_width !== undefined) globalSettings.value.subtitle.outlineWidth = clampOutlineWidth(settings.outline_width)
    if (['none', 'sidecar', 'burn'].includes(settings.subtitle_output_mode)) {
      globalSettings.value.subtitle.outputMode = settings.subtitle_output_mode
    }
    if (settings.tts_voice) globalSettings.value.tts.voice = settings.tts_voice
    if (settings.tts_speed !== undefined && settings.tts_speed !== '') globalSettings.value.tts.speed = Number(settings.tts_speed) || globalSettings.value.tts.speed
    if (includeVoice && settings.selected_voice_key) {
      onVoiceKeyChange(settings.selected_voice_key)
    } else if (includeVoice && settings.reference_text) {
      referenceText.value = settings.reference_text
    }
  }

  const persistRunSettingsNow = async ({ includeReferenceAudio = false } = {}) => {
    if (!currentRunId.value || stage.value !== 'workspace' || suppressProjectSettingsSave.value) return false
    const formData = new FormData()
    formData.append('settings_json', JSON.stringify(collectCurrentProjectSettings()))
    if (includeReferenceAudio && selectedVoiceKey.value === 'custom' && cloneAudioFile.value) {
      formData.append('reference_audio', cloneAudioFile.value)
    }
    try {
      const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(currentRunId.value)}/settings`), {
        method: 'PATCH',
        body: formData,
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data?.detail || `設定保存失敗 (${res.status})`)
      runManifest.value = data
      pendingReferenceAudioUpload.value = false
      return true
    } catch (err) {
      console.warn('[VideoRun] settings save failed:', err)
      renderMessage.value = err.message || '設定保存失敗'
      return false
    }
  }

  const schedulePersistRunSettings = ({ includeReferenceAudio = false, delay = 700 } = {}) => {
    if (!currentRunId.value || stage.value !== 'workspace' || suppressProjectSettingsSave.value) return
    if (includeReferenceAudio) pendingReferenceAudioUpload.value = true
    if (projectSettingsSaveTimer) clearTimeout(projectSettingsSaveTimer)
    projectSettingsSaveTimer = setTimeout(() => {
      projectSettingsSaveTimer = null
      persistRunSettingsNow({ includeReferenceAudio: pendingReferenceAudioUpload.value })
    }, delay)
  }

  const loadRunReferenceAudio = async (runId, filename = 'reference.wav') => {
    if (!runId) return
    const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(runId)}/reference-audio`))
    if (!res.ok) return
    const blob = await res.blob()
    const file = new File([blob], filename || 'reference.wav', { type: blob.type || 'audio/wav' })
    cloneAudioFile.value = file
    if (cloneAudioUrl.value) URL.revokeObjectURL(cloneAudioUrl.value)
    const objectUrl = URL.createObjectURL(blob)
    cloneAudioUrl.value = objectUrl
    tempObjectUrls.value.push(objectUrl)
  }

  const applyProjectSettingsToUi = async (manifest) => {
    const settings = manifest?.settings?.current || manifest?.settings?.last_render || {}
    if (!settings || Object.keys(settings).length === 0) return
    applyVariantSettingsToUi({ settings }, { includeVoice: false })
    if (settings.selected_voice_key) {
      selectedVoiceKey.value = settings.selected_voice_key
      if (settings.selected_voice_key === 'custom') {
        if (cloneAudioUrl.value) {
          URL.revokeObjectURL(cloneAudioUrl.value)
          cloneAudioUrl.value = ''
        }
        cloneAudioFile.value = null
        referenceText.value = String(settings.reference_text || '')
        const ref = settings.reference_audio || {}
        await loadRunReferenceAudio(manifest.run_id, ref.filename || 'reference.wav')
      } else {
        await onVoiceKeyChange(settings.selected_voice_key)
      }
    } else if (settings.reference_text) {
      referenceText.value = String(settings.reference_text || '')
    }
  }

  const clearProjectSettingsSaveTimer = () => {
    if (projectSettingsSaveTimer) clearTimeout(projectSettingsSaveTimer)
    projectSettingsSaveTimer = null
  }

  return {
    suppressProjectSettingsSave,
    collectCurrentProjectSettings,
    applyVariantSettingsToUi,
    persistRunSettingsNow,
    schedulePersistRunSettings,
    applyProjectSettingsToUi,
    clearProjectSettingsSaveTimer,
  }
}
