<template>
  <div class="render-controls">
    <div class="preview-actions-row">
      <button class="btn btn-success" :disabled="!hasSelectedSlide" @click="$emit('render-current')">
        {{ rendering ? '渲染中...' : (hasRenderedVideo ? '重新渲染' : '渲染此頁') }}
      </button>
      <button class="btn btn-dark render-all-btn" :disabled="!slidesCount || renderingAll" @click="$emit('render-all')">
        {{ renderingAll ? '全部渲染中...' : '渲染全部' }}
      </button>
      <button v-if="showStopAll" class="btn btn-danger" @click="$emit('stop-all')">
        終止渲染全部
      </button>
      <button
        v-for="pageIdx in cancellableSinglePages"
        :key="`cancel-page-${pageIdx}`"
        class="btn btn-outline-danger"
        @click="$emit('stop-page', pageIdx)"
      >
        終止: 第{{ pageIdx + 1 }}頁
      </button>
      <button class="btn btn-outline-light merge-btn" :disabled="!renderedCount" @click="$emit('merge')">
        合併匯出
      </button>
      <button v-if="hasSelectedSrt" class="btn btn-outline-info" @click="$emit('download-srt')">
        下載本頁 SRT
      </button>
    </div>
    <div class="alert py-2 mb-0 render-status-bar" :class="message ? 'alert-info' : 'alert-secondary'">
      {{ message || '待命：尚未開始渲染。' }}
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  hasSelectedSlide: { type: Boolean, default: false },
  hasRenderedVideo: { type: Boolean, default: false },
  renderedCount: { type: Number, default: 0 },
  slidesCount: { type: Number, default: 0 },
  rendering: { type: Boolean, default: false },
  renderingAll: { type: Boolean, default: false },
  queueLength: { type: Number, default: 0 },
  cancellableSinglePages: { type: Array, default: () => [] },
  message: { type: String, default: '' },
  hasSelectedSrt: { type: Boolean, default: false },
})

defineEmits(['render-current', 'render-all', 'stop-all', 'stop-page', 'merge', 'download-srt'])

const showStopAll = computed(() => props.rendering || props.renderingAll || props.queueLength > 0)
</script>

<style scoped>
.render-controls {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.preview-actions-row {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 8px;
  padding-left: 14px;
  padding-right: 14px;
  flex-wrap: wrap;
  row-gap: 8px;
}

.render-all-btn {
  background: #0ea5e9;
  border-color: #0284c7;
  color: #ffffff;
}

.render-all-btn:hover:not(:disabled) {
  background: #0284c7;
  border-color: #0369a1;
  color: #ffffff;
}

.merge-btn {
  margin-left: auto;
}

.render-status-bar {
  min-height: 42px;
  display: flex;
  align-items: center;
  margin-left: 10px;
  margin-right: 10px;
}

@media (max-width: 900px) {
  .preview-actions-row {
    padding-left: 8px;
    padding-right: 8px;
  }

  .preview-actions-row .btn {
    flex: 0 1 auto;
  }

  .merge-btn {
    margin-left: 0;
  }
}
</style>
