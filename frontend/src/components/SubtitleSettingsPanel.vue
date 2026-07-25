<template>
  <div class="subtitle-controls-panel simple-subtitle-controls">
    <div class="subtitle-setting-item main-setting-row">
      <label class="ctrl-label subtitle-left-label m-0">字幕輸出</label>
      <select class="subtitle-setting-input subtitle-output-mode" :value="outputMode" @change="$emit('update:outputMode', $event.target.value)">
        <option value="none">無字幕</option>
        <option value="sidecar">輸出 SRT</option>
        <option value="burn">燒錄字幕</option>
      </select>
    </div>
    <template v-if="outputMode === 'burn'">
    <div class="subtitle-setting-item main-setting-row">
      <label class="ctrl-label subtitle-left-label m-0">字體大小</label>
      <input v-model.number="settings.fontSize" type="number" min="11" max="120" class="subtitle-setting-input" />
    </div>

    <div class="subtitle-setting-item main-setting-row">
      <label class="ctrl-label subtitle-left-label m-0">字幕高度</label>
      <input v-model.number="settings.marginV" type="number" min="0" max="220" step="2" class="subtitle-setting-input" />
    </div>

    <div class="subtitle-setting-item main-setting-row">
      <label class="ctrl-label subtitle-left-label m-0">字體顏色</label>
      <div class="subtitle-color-editor">
        <input :value="getColorHex('color')" type="color" class="subtitle-setting-color" @input="onColorHexInput('color', $event.target.value)" />
        <input :value="getColorHex('color')" type="text" class="subtitle-hex-input" maxlength="7" @input="onColorHexInput('color', $event.target.value)" @change="onColorHexInput('color', $event.target.value)" @blur="onColorHexInput('color', $event.target.value)" />
      </div>
    </div>

    <template v-if="outputMode === 'burn'">
    <div class="subtitle-setting-item subtitle-switch-line compact-switch main-setting-row">
      <span class="text-white subtitle-left-label">開啟底色</span>
      <div class="form-check form-switch m-0">
        <input class="form-check-input flex-shrink-0 cursor-pointer" type="checkbox" v-model="settings.enableBackground">
      </div>
    </div>
    <div v-if="settings.enableBackground" class="subtitle-subsetting">
      <div class="subtitle-setting-item main-setting-row">
        <label class="ctrl-label subtitle-left-label m-0">底色顏色</label>
        <div class="subtitle-color-editor">
          <input :value="getColorHex('bgColor')" type="color" class="subtitle-setting-color" @input="onColorHexInput('bgColor', $event.target.value)" />
          <input :value="getColorHex('bgColor')" type="text" class="subtitle-hex-input" maxlength="7" @input="onColorHexInput('bgColor', $event.target.value)" @change="onColorHexInput('bgColor', $event.target.value)" @blur="onColorHexInput('bgColor', $event.target.value)" />
        </div>
      </div>
      <div class="subtitle-setting-item">
        <label class="ctrl-label subtitle-left-label m-0">底色透明度 {{ settings.bgOpacity }}%</label>
        <input v-model.number="settings.bgOpacity" type="range" min="0" max="100" class="subtitle-setting-range subtitle-subsetting-range" />
      </div>
    </div>

    <div class="subtitle-setting-item subtitle-switch-line compact-switch main-setting-row">
      <span class="text-white subtitle-left-label">逐字高光</span>
      <div class="form-check form-switch m-0">
        <input class="form-check-input flex-shrink-0 cursor-pointer" type="checkbox" :checked="enableHighlight" @change="$emit('update:enableHighlight', $event.target.checked)">
      </div>
    </div>
    <div v-if="enableHighlight" class="subtitle-subsetting">
      <div class="subtitle-setting-item main-setting-row">
        <label class="ctrl-label subtitle-left-label m-0">高光字顏色</label>
        <div class="subtitle-color-editor">
          <input :value="getColorHex('activeWordColor')" type="color" class="subtitle-setting-color" @input="onColorHexInput('activeWordColor', $event.target.value)" />
          <input :value="getColorHex('activeWordColor')" type="text" class="subtitle-hex-input" maxlength="7" @input="onColorHexInput('activeWordColor', $event.target.value)" @change="onColorHexInput('activeWordColor', $event.target.value)" @blur="onColorHexInput('activeWordColor', $event.target.value)" />
        </div>
      </div>
    </div>
    </template>

    <div class="subtitle-setting-item subtitle-switch-line compact-switch main-setting-row">
      <span class="text-white subtitle-left-label">開啟外框</span>
      <div class="form-check form-switch m-0">
        <input class="form-check-input flex-shrink-0 cursor-pointer" type="checkbox" v-model="settings.enableOutline">
      </div>
    </div>
    <div v-if="settings.enableOutline" class="subtitle-subsetting">
      <div class="subtitle-setting-item main-setting-row">
        <label class="ctrl-label subtitle-left-label m-0">外框顏色</label>
        <div class="subtitle-color-editor">
          <input :value="getColorHex('outlineColor')" type="color" class="subtitle-setting-color" @input="onColorHexInput('outlineColor', $event.target.value)" />
          <input :value="getColorHex('outlineColor')" type="text" class="subtitle-hex-input" maxlength="7" @input="onColorHexInput('outlineColor', $event.target.value)" @change="onColorHexInput('outlineColor', $event.target.value)" @blur="onColorHexInput('outlineColor', $event.target.value)" />
        </div>
      </div>
      <div class="subtitle-setting-item">
        <label class="ctrl-label subtitle-left-label m-0">外框寬度 {{ settings.outlineWidth }}</label>
        <input v-model.number="settings.outlineWidth" type="range" min="0" max="10" step="0.5" class="subtitle-setting-range subtitle-subsetting-range" />
      </div>
    </div>
    </template>
  </div>
</template>

<script setup>
const props = defineProps({
  settings: { type: Object, required: true },
  enableHighlight: { type: Boolean, default: false },
  outputMode: { type: String, default: 'burn' },
})

defineEmits(['update:enableHighlight', 'update:outputMode'])

const normalizeHexColor = (value) => {
  const raw = String(value || '').trim()
  const body = raw.startsWith('#') ? raw.slice(1) : raw
  if (/^[0-9a-fA-F]{6}$/.test(body)) return `#${body.toUpperCase()}`
  if (/^[0-9a-fA-F]{3}$/.test(body)) {
    return `#${body.split('').map((ch) => ch + ch).join('').toUpperCase()}`
  }
  return '#000000'
}

const DEFAULT_COLORS = {
  color: '#FFFFFF',
  activeWordColor: '#FACC15',
  bgColor: '#000000',
  outlineColor: '#000000',
}

const getColorHex = (field) => normalizeHexColor(props.settings[field] || DEFAULT_COLORS[field] || '#000000')

const onColorHexInput = (field, value) => {
  const raw = String(value || '').trim()
  const body = raw.startsWith('#') ? raw.slice(1) : raw
  if (/^[0-9a-fA-F]{3}$/.test(body) || /^[0-9a-fA-F]{6}$/.test(body)) {
    props.settings[field] = normalizeHexColor(`#${body}`)
  }
}
</script>

<style scoped>
.subtitle-controls-panel {
  flex: 0 0 clamp(330px, 30%, 430px);
  width: clamp(330px, 30%, 430px);
  min-width: 330px;
  max-width: none;
  background: #0b1220;
  border-right: 1px solid #1e293b;
  padding: 14px 12px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  scrollbar-width: thin;
  scrollbar-color: #1e3a5f #0b1220;
}

.subtitle-setting-item,
.subtitle-color-editor,
.compact-switch {
  display: flex;
  align-items: center;
}

.subtitle-setting-item {
  justify-content: space-between;
  gap: 12px;
}

.main-setting-row {
  flex-direction: row;
  gap: 16px;
  flex-wrap: nowrap;
}

.ctrl-label,
.subtitle-left-label,
.subtitle-switch-line .text-white {
  color: #e2e8f0;
  font-size: 17px;
  font-weight: 700;
  white-space: nowrap;
  line-height: 1.2;
  text-align: left;
}

.subtitle-setting-input {
  width: 82px;
  min-height: 42px;
  background: #0f172a;
  border: 1px solid #334155;
  color: #e2e8f0;
  border-radius: 8px;
  padding: 4px 10px;
  font-size: 20px;
}

.subtitle-output-mode {
  width: 160px;
  font-size: 14px;
}


.subtitle-setting-color {
  width: 92px;
  height: 38px;
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 8px;
  padding: 2px;
}

.subtitle-color-editor { gap: 8px; }

.subtitle-hex-input {
  width: 94px;
  height: 32px;
  background: #0f172a;
  border: 1px solid #334155;
  color: #e2e8f0;
  border-radius: 8px;
  padding: 2px 8px;
  font-size: 13px;
}

.subtitle-subsetting {
  margin-left: 16px;
  padding-left: 8px;
  border-left: 2px solid #1e3a5f;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.subtitle-subsetting .ctrl-label {
  font-size: 14px;
  font-weight: 600;
}

.subtitle-subsetting .subtitle-setting-input {
  width: 64px;
  min-height: 40px;
  font-size: 14px;
}

.subtitle-subsetting .subtitle-setting-color {
  width: 84px;
  height: 32px;
}

.subtitle-setting-range { width: 100%; }

.subtitle-subsetting-range {
  width: 230px;
  margin-left: 10px;
}

.form-check {
  min-height: 0;
  padding-left: 0;
}

.form-check-input {
  width: 58px !important;
  height: 30px !important;
  margin: 0 !important;
  cursor: pointer;
  background-color: #9ca3af;
  border-color: transparent;
  background-size: 24px 24px;
  box-shadow: none;
}

.form-check-input:checked {
  background-color: #2486ff;
  border-color: #2486ff;
}

.form-check-input:focus {
  box-shadow: 0 0 0 3px rgba(36, 134, 255, 0.18);
}

@media (max-width: 980px) {
  .subtitle-controls-panel {
    min-width: 0;
    width: 100% !important;
    flex: 0 0 auto;
    border-right: 0;
    border-bottom: 1px solid #1e293b;
  }
}

@media (max-width: 600px) {
  .main-setting-row { gap: 8px; }

  .ctrl-label,
  .subtitle-switch-line .text-white { font-size: 15px; }

  .subtitle-setting-input {
    width: 76px;
    font-size: 18px;
  }

  .subtitle-setting-color { width: 74px; }
  .subtitle-hex-input { width: 86px; }
  .subtitle-subsetting-range { width: min(210px, 52vw); }
}
</style>
