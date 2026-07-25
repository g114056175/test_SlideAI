<template>
  <div class="tts-single-col">
    <div class="ctrl-section">
      <label class="ctrl-label tts-label">音色來源</label>
      <select class="styled-select" :value="selectedVoiceKey" @change="onVoiceSelect">
        <option v-for="(info, key) in presetManifest" :key="key" :value="key">{{ key }}</option>
        <option value="custom">自訂上傳...</option>
      </select>
    </div>

    <div class="ctrl-section">
      <div class="reference-grid-labels">
        <label class="ctrl-label tts-label">參考音色檔案</label>
        <label class="ctrl-label tts-label">
          參考音色文本
          <span class="ctrl-hint-inline">（建議 3~10 秒，可留空）</span>
          <span v-if="presetLoading" class="ctrl-hint-inline">⏳ 載入中...</span>
          <span v-else-if="presetLoadError && isPresetVoice" class="ctrl-hint-inline error-hint">⚠️ 音檔載入失敗</span>
        </label>
        <span class="ctrl-label tts-label asr-label-spacer">ASR</span>
      </div>

      <div class="upload-with-text vox-reference-grid">
        <div class="upload-file-col vox-grid-audio">
          <div
            v-if="!cloneAudioUrl && !isPresetVoice"
            class="upload-drop-zone"
            :class="{ 'drag-over': isRefDragOver }"
            @dragenter.prevent="isRefDragOver = true"
            @dragover.prevent="isRefDragOver = true"
            @dragleave="isRefDragOver = false"
            @drop.prevent="handleRefAudioDrop"
            @click="refAudioInput?.click()"
          >
            <span>拖曳或點擊上傳</span>
          </div>

          <div v-else-if="cloneAudioUrl" class="inline-upload-preview">
            <WaveformPlayer
              :src="cloneAudioUrl"
              :download-name="cloneAudioFile?.name || 'reference.wav'"
              :show-delete="!isPresetVoice"
              :compact="true"
              @delete="$emit('clear-clone-audio')"
            />
          </div>

          <div v-else-if="isPresetVoice && !presetLoading" class="upload-hint-text">
            音色檔案尚未準備，請確認後端已放置對應音檔。
          </div>
        </div>

        <div class="vox-grid-text">
          <textarea
            v-if="!isPresetVoice"
            :value="referenceText"
            class="ref-text-input"
            placeholder="可選：輸入參考音檔文本；若留空則走 x-vector only。"
            rows="4"
            @input="$emit('update:referenceText', $event.target.value)"
          />
          <div v-else class="ref-text-input ref-text-readonly" style="white-space: pre-wrap;">{{ referenceText || '（無參考文字）' }}</div>
        </div>

        <div class="vox-grid-toggle">
          <button
            type="button"
            class="asr-fill-btn"
            :class="{ disabled: isPresetVoice || !cloneAudioFile || asrFilling }"
            :disabled="isPresetVoice || !cloneAudioFile || asrFilling"
            :title="isPresetVoice ? '預設音色已提供參考文字，不需要 ASR 代填' : (!cloneAudioFile ? '請先上傳參考音檔' : '使用本地 ASR 代填參考文本')"
            @click="$emit('fill-reference-text')"
          >
            <span v-if="asrFilling">辨識中</span>
            <span v-else-if="isPresetVoice">不需<br>代填</span>
            <span v-else-if="!cloneAudioFile">上傳後<br>可代填</span>
            <span v-else>本地ASR<br>代填</span>
          </button>
        </div>
      </div>
      <input ref="refAudioInput" type="file" accept="audio/*" style="display:none" @change="$emit('clone-file-change', $event)" />
    </div>

    <div class="ctrl-section listen-section">
      <div class="listen-grid-labels">
        <label class="ctrl-label tts-label">語音速度</label>
        <label class="ctrl-label tts-label">試聽文字</label>
        <span class="ctrl-label tts-label action-label-spacer">生成</span>
      </div>
      <div class="tts-preview-compose">
        <div class="tts-speed-panel">
          <div class="tts-speed-row">
            <span class="tts-speed-label">語音速度:</span>
            <input
              :value="ttsPreviewSpeed"
              type="range"
              class="styled-range tts-speed-slider"
              min="0.50"
              max="2.00"
              step="0.05"
              @input="$emit('update:ttsPreviewSpeed', Number($event.target.value))"
            />
            <input
              :value="ttsPreviewSpeed"
              type="number"
              class="form-control dark-input tts-speed-number"
              min="0.50"
              max="2.00"
              step="0.05"
              @input="$emit('update:ttsPreviewSpeed', Number($event.target.value))"
            />
            <span class="tts-speed-unit">x</span>
          </div>
          <div class="ctrl-hint-inline tts-speed-hint">1.00 為原速；小於 1 會放慢試聽音檔</div>
        </div>
        <div class="tts-text-panel">
          <textarea
            :value="ttsPreviewText"
            class="tts-test-input tts-test-input-main"
            placeholder="輸入要試聽的文字..."
            rows="4"
            @input="$emit('update:ttsPreviewText', $event.target.value)"
          />
        </div>
        <button class="btn-generate" :disabled="!ttsPreviewText.trim() || ttsGenerating" @click="$emit('generate-preview')">
          <span v-if="ttsGenerating">生成中...</span>
          <span v-else>▶ 生成試聽音檔</span>
        </button>
      </div>
    </div>

    <div v-if="ttsPreviewUrl" class="ctrl-section generated-audio-section">
      <label class="ctrl-label tts-label">生成結果</label>
      <WaveformPlayer :src="ttsPreviewUrl" download-name="tts_preview.wav" />
    </div>

    <div v-if="ttsError" class="tts-error">{{ ttsError }}</div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import WaveformPlayer from './WaveformPlayer.vue'

const refAudioInput = ref(null)
const isRefDragOver = ref(false)

defineProps({
  presetManifest: { type: Object, default: () => ({}) },
  selectedVoiceKey: { type: String, default: '' },
  presetLoading: { type: Boolean, default: false },
  presetLoadError: { type: Boolean, default: false },
  isPresetVoice: { type: Boolean, default: false },
  cloneAudioUrl: { type: String, default: '' },
  cloneAudioFile: { type: Object, default: null },
  referenceText: { type: String, default: '' },
  asrFilling: { type: Boolean, default: false },
  ttsPreviewText: { type: String, default: '' },
  ttsPreviewSpeed: { type: Number, default: 1 },
  ttsGenerating: { type: Boolean, default: false },
  ttsPreviewUrl: { type: String, default: '' },
  ttsError: { type: String, default: '' },
})

const emit = defineEmits([
  'update:selectedVoiceKey',
  'update:referenceText',
  'update:ttsPreviewText',
  'update:ttsPreviewSpeed',
  'voice-change',
  'ref-audio-drop',
  'clone-file-change',
  'clear-clone-audio',
  'fill-reference-text',
  'generate-preview',
])

const onVoiceSelect = (event) => {
  const value = event.target.value
  emit('update:selectedVoiceKey', value)
  emit('voice-change', value)
}

const handleRefAudioDrop = (event) => {
  isRefDragOver.value = false
  emit('ref-audio-drop', event)
}
</script>

<style scoped>
.tts-single-col {
  display: flex;
  flex-direction: column;
  gap: 18px;
  height: 100%;
  overflow-y: auto;
  padding: 18px 20px 20px;
  background: #0b1220;
}

.tts-single-col::-webkit-scrollbar { width: 6px; height: 6px; }
.tts-single-col::-webkit-scrollbar-track { background: transparent; }
.tts-single-col::-webkit-scrollbar-thumb { background: #1e3a5f; border-radius: 3px; }
.tts-single-col::-webkit-scrollbar-thumb:hover { background: #3b82f6; }

.ctrl-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.ctrl-label {
  font-size: 16px;
  font-weight: 800;
  color: #e2e8f0;
  letter-spacing: 0.3px;
}

.tts-label { color: #e2e8f0 !important; }

.ctrl-hint-inline {
  margin-left: 8px;
  font-size: 13px;
  color: #94a3b8;
  font-weight: 400;
}

.error-hint { color: #f87171; }

.styled-select,
.ref-text-input,
.tts-test-input {
  width: 100%;
  background: #1e293b;
  border: 1px solid #334155;
  border-radius: 8px;
  color: #f8fafc;
}

.styled-select {
  min-height: 42px;
  padding: 9px 11px;
}

.reference-grid-labels,
.vox-reference-grid {
  --voice-row-height: 170px;
  display: grid;
  grid-template-columns: minmax(360px, 1.05fr) minmax(340px, 1fr) 144px;
  gap: 14px;
  align-items: end;
}

.vox-reference-grid {
  align-items: stretch;
  grid-auto-rows: var(--voice-row-height);
}

.vox-reference-grid > * {
  height: var(--voice-row-height);
}

.asr-label-spacer {
  opacity: 0;
  pointer-events: none;
}

.upload-file-col,
.vox-grid-audio,
.vox-grid-text,
.vox-grid-toggle { min-width: 0; }

.upload-drop-zone {
  height: 100%;
  min-height: 100%;
  border: 1px dashed #38bdf8;
  border-radius: 12px;
  background: rgba(14, 165, 233, 0.08);
  display: grid;
  place-items: center;
  color: #bae6fd;
  cursor: pointer;
  text-align: center;
  font-weight: 800;
}
.upload-drop-zone:hover,
.upload-drop-zone.drag-over {
  border-color: #67e8f9;
  background: rgba(14, 165, 233, 0.22);
  color: #e0f2fe;
  box-shadow: inset 0 0 0 1px rgba(103, 232, 249, 0.34), 0 0 20px rgba(14, 165, 233, 0.16);
}

.inline-upload-preview {
  min-width: 0;
  height: 100%;
}
.inline-upload-preview :deep(.waveform-player.compact) {
  height: 100%;
  min-height: 100%;
  box-sizing: border-box;
  justify-content: space-between;
}
.inline-upload-preview :deep(.waveform-player.compact .wave) {
  flex: 1 1 auto;
  min-height: 74px;
}
.inline-upload-preview :deep(.waveform-player.compact .control-deck) {
  flex: 0 0 auto;
}

.upload-hint-text {
  height: 100%;
  min-height: 100%;
  display: grid;
  place-items: center;
  border: 1px solid #334155;
  border-radius: 12px;
  color: #94a3b8;
  padding: 12px;
  text-align: center;
}

.ref-text-input {
  height: 100%;
  min-height: 100%;
  padding: 12px 13px;
  resize: none;
  line-height: 1.55;
}
.ref-text-readonly {
  overflow: auto;
  background: #111827;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
}

.vox-grid-toggle {
  display: flex;
  align-items: stretch;
}

.asr-fill-btn {
  width: 100%;
  height: 100%;
  min-height: 100%;
  border-radius: 12px;
  border: 1px solid rgba(56, 189, 248, 0.72);
  background: linear-gradient(180deg, rgba(14, 116, 144, 0.42), rgba(12, 74, 110, 0.72));
  color: #ecfeff;
  font-size: 14px;
  font-weight: 800;
  line-height: 1.45;
  letter-spacing: 0.2px;
  padding: 12px 10px;
  cursor: pointer;
  transition: transform 0.12s, border-color 0.15s, background 0.15s, opacity 0.15s;
}
.asr-fill-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  border-color: #67e8f9;
  background: linear-gradient(180deg, rgba(8, 145, 178, 0.62), rgba(14, 116, 144, 0.88));
}
.asr-fill-btn.disabled,
.asr-fill-btn:disabled {
  border-color: rgba(71, 85, 105, 0.7);
  background: #111827;
  color: #64748b;
  opacity: 0.72;
  cursor: not-allowed;
  transform: none;
}

.listen-grid-labels,
.tts-preview-compose {
  display: grid;
  grid-template-columns: minmax(320px, 0.48fr) minmax(420px, 1fr) 160px;
  gap: 14px;
  align-items: start;
}

.listen-grid-labels {
  margin-bottom: 2px;
}

.listen-grid-labels .ctrl-label {
  display: flex;
  align-items: flex-end;
  min-height: 22px;
  line-height: 1.15;
}

.tts-text-panel {
  min-width: 0;
}

.action-label-spacer {
  opacity: 0;
  pointer-events: none;
}

.tts-speed-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  height: 118px;
}

.tts-speed-row {
  min-height: 60px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid #334155;
  border-radius: 10px;
  background: #111827;
  color: #e2e8f0;
  font-size: 13px;
  flex-wrap: nowrap;
  min-width: 0;
}

.tts-speed-label {
  white-space: nowrap;
  color: #cbd5e1;
  font-weight: 700;
}

.tts-speed-slider { flex: 1; min-width: 130px; }
.tts-speed-number { width: 88px; flex: 0 0 88px; }
.tts-speed-unit {
  color: #94a3b8;
  font-size: 13px;
  font-weight: 700;
  min-width: 12px;
}
.tts-speed-hint { padding: 0 4px; }

.tts-test-input {
  min-width: 0;
  height: 118px;
  min-height: 118px;
  padding: 12px 13px;
  font-size: 14px;
  resize: none;
  overflow-y: auto;
  line-height: 1.6;
}
.tts-test-input:focus,
.ref-text-input:focus,
.styled-select:focus {
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 2px rgba(59,130,246,0.2);
}

.btn-generate {
  height: 118px;
  min-height: 118px;
  background: linear-gradient(180deg, #2563eb 0%, #4338ca 100%);
  color: #fff;
  border: 1px solid rgba(147, 197, 253, 0.42);
  border-radius: 12px;
  padding: 12px 12px;
  font-size: 14px;
  font-weight: 800;
  line-height: 1.45;
  cursor: pointer;
  transition: opacity 0.2s, transform 0.1s, box-shadow 0.15s;
  box-shadow: 0 8px 22px rgba(37, 99, 235, 0.18);
}
.btn-generate:hover:not(:disabled) {
  opacity: 0.94;
  transform: translateY(-1px);
  box-shadow: 0 10px 26px rgba(37, 99, 235, 0.28);
}
.btn-generate:disabled {
  opacity: 0.42;
  cursor: not-allowed;
  box-shadow: none;
}

.generated-audio-section {
  margin-top: 2px;
}

.tts-error {
  color: #f87171;
  font-size: 13px;
  background: rgba(248,113,113,0.08);
  border: 1px solid rgba(248,113,113,0.3);
  border-radius: 8px;
  padding: 8px 12px;
}

@media (max-width: 1280px) {
  .reference-grid-labels,
  .vox-reference-grid,
  .listen-grid-labels,
  .tts-preview-compose {
    grid-template-columns: 1fr;
  }

  .asr-label-spacer,
  .action-label-spacer { display: none; }

  .asr-fill-btn,
  .btn-generate {
    min-height: 52px;
    height: 52px;
  }

  .tts-speed-panel {
    height: auto;
  }

  .vox-reference-grid {
    grid-auto-rows: auto;
  }

  .vox-reference-grid > * {
    height: auto;
  }
}

@media (max-width: 700px) {
  .tts-single-col {
    min-height: 0;
    overflow: auto;
    padding: 12px;
  }

  .tts-speed-row {
    flex-wrap: wrap;
    gap: 8px;
  }

  .tts-speed-number {
    width: 76px;
    flex-basis: 76px;
  }
}
</style>
