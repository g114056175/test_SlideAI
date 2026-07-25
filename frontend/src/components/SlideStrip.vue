<template>
  <div class="slide-selector-wrapper" :style="{ width: width + 'px' }">
    <aside class="slide-selector">
      <div class="thumbnail-list">
        <button
          v-for="(slide, idx) in slides"
          :key="slide.id || idx"
          class="slide-item"
          :class="{ active: !selectedMergedPreview && selectedIndex === idx, rendered: showRenderState && (!!renderedPageVideos[idx] || variantCounts[idx] > 0) }"
          @click="$emit('update:selectedIndex', idx)"
        >
          <div class="slide-index">{{ idx + 1 }}</div>
          <div
            v-if="showRenderState && (renderedPageVideos[idx] || variantCounts[idx] > 0 || renderingPageStatus[idx] === 'running')"
            class="rendered-dot"
            :class="{ running: renderingPageStatus[idx] === 'running' }"
            :title="renderingPageStatus[idx] === 'running' ? '此頁渲染中' : `此頁已有 ${variantCounts[idx] || 1} 個產出影片`"
          >
            <span v-if="variantCounts[idx] > 1">{{ variantCounts[idx] }}</span>
          </div>
          <div class="thumb">
            <img v-if="slide.thumbnailUrl" :src="slide.thumbnailUrl" alt="thumb" loading="lazy" decoding="async" />
            <div v-else class="thumb-placeholder">{{ idx + 1 }}</div>
          </div>
        </button>
        <button
          v-if="hasMergedPreview"
          class="slide-item merged-item"
          :class="{ active: selectedMergedPreview }"
          @click="$emit('select-merged-preview')"
        >
          <div class="slide-index">ALL</div>
          <div class="thumb merged-thumb">
            <img v-if="mergedPreviewThumbnailUrl" :src="mergedPreviewThumbnailUrl" alt="merged-thumb" loading="lazy" decoding="async" />
            <div v-else class="thumb-placeholder">ALL</div>
          </div>
        </button>
      </div>

      <div v-if="slides.length > 0" class="presentation-footer mt-auto">
        <div class="slide-counter-wrapper w-100 justify-content-center">
          <div class="slide-counter" @click="openJumpInput" v-show="!showJumpInput">
            <template v-if="selectedMergedPreview">投影片 ALL / {{ totalCount }}</template>
            <template v-else>投影片 {{ selectedIndex + 1 }} / {{ totalCount }}</template>
          </div>
          <input
            v-show="showJumpInput"
            ref="jumpInputRef"
            type="number"
            class="form-control dark-input form-control-sm jump-input"
            v-model.number="jumpTarget"
            @blur="executeJump"
            @keyup.enter="executeJump"
            min="1"
            :max="slides.length"
          />
        </div>
      </div>
    </aside>
    <div class="resizer-x" @mousedown="$emit('resize-start', $event)"></div>
  </div>
</template>

<script setup>
import { computed, nextTick, ref, watch } from 'vue'

const props = defineProps({
  slides: { type: Array, default: () => [] },
  selectedIndex: { type: Number, default: 0 },
  width: { type: Number, default: 220 },
  showRenderState: { type: Boolean, default: false },
  renderedPageVideos: { type: Object, default: () => ({}) },
  renderingPageStatus: { type: Object, default: () => ({}) },
  variantCounts: { type: Object, default: () => ({}) },
  hasMergedPreview: { type: Boolean, default: false },
  mergedPreviewThumbnailUrl: { type: String, default: '' },
  selectedMergedPreview: { type: Boolean, default: false },
})

const emit = defineEmits(['update:selectedIndex', 'resize-start', 'select-merged-preview'])

const showJumpInput = ref(false)
const jumpTarget = ref(1)
const jumpInputRef = ref(null)
const totalCount = computed(() => props.slides.length)

const openJumpInput = async () => {
  showJumpInput.value = true
  jumpTarget.value = props.selectedIndex + 1
  await nextTick()
  jumpInputRef.value?.focus()
  jumpInputRef.value?.select()
}

const executeJump = () => {
  if (!showJumpInput.value) return
  showJumpInput.value = false
  const max = props.slides.length
  if (!max || typeof jumpTarget.value !== 'number') return
  const targetIdx = Math.max(0, Math.min(jumpTarget.value - 1, max - 1))
  emit('update:selectedIndex', targetIdx)
}

watch(() => props.selectedIndex, (idx) => {
  jumpTarget.value = idx + 1
})
</script>

<style scoped>
.slide-selector-wrapper {
  display: flex;
  flex-shrink: 0;
  height: 100%;
  max-width: min(45%, 450px);
  min-width: 0;
}

.slide-selector {
  flex: 1;
  background: #0f172a;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.thumbnail-list {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 10px 4px 10px 0;
}

.slide-item {
  width: 100%;
  background: transparent;
  border: none;
  font-family: inherit;
  padding: 4px;
  margin-bottom: 12px;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  cursor: pointer;
  transition: transform 0.1s;
  position: relative;
}

.slide-item:hover { transform: scale(1.02); }

.slide-index {
  font-size: 16px;
  font-weight: 700;
  color: #94a3b8;
  width: 26px;
  text-align: right;
  padding-top: 2px;
  flex-shrink: 0;
}

.slide-item.active .slide-index { color: #fca5a5; }

.thumb {
  flex: 1;
  aspect-ratio: 16 / 9;
  border: 2px solid transparent;
  border-radius: 4px;
  overflow: hidden;
  background: #1e293b;
  min-width: 0;
}

.slide-item.active .thumb { border-color: #ef4444; }
.slide-item.merged-item .slide-index {
  width: 34px;
  font-size: 13px;
  color: #7dd3fc;
}
.slide-item.merged-item.active .slide-index { color: #22d3ee; }
.slide-item.merged-item .thumb { border-color: rgba(34, 211, 238, 0.45); }
.slide-item.merged-item.active .thumb { border-color: #22d3ee; }

.rendered-dot {
  position: absolute;
  top: 8px;
  right: 10px;
  min-width: 14px;
  height: 14px;
  padding: 0 4px;
  border-radius: 999px;
  background: #22c55e;
  box-shadow: 0 0 0 2px rgba(15, 23, 42, 0.95);
  color: #052e16;
  font-size: 10px;
  font-weight: 900;
  line-height: 14px;
  text-align: center;
}
.rendered-dot.running { background: #f59e0b; }

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.thumb-placeholder {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  color: #93c5fd;
  font-weight: 700;
}

.presentation-footer {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  background: #0f172a;
  border-top: 1px solid #1e293b;
  padding: 8px 12px;
  font-size: 14px;
  font-weight: 600;
  color: #e2e8f0;
}

.slide-counter-wrapper {
  display: flex;
  align-items: center;
  background: transparent;
}

.slide-counter {
  cursor: pointer;
  padding: 4px 10px;
  transition: background 0.1s;
}

.slide-counter:hover {
  background: #334155;
  color: #f8fafc;
}

.jump-input {
  width: 60px;
  height: 26px;
  padding: 2px 4px;
  font-size: 13px;
  text-align: center;
  border: none;
  background: #0b1220;
  color: #f8fafc;
  -webkit-text-fill-color: #f8fafc;
  caret-color: #f8fafc;
}
.jump-input:focus {
  outline: none;
  box-shadow: none;
  background: #020617;
  color: #ffffff;
  -webkit-text-fill-color: #ffffff;
}

.resizer-x {
  width: 6px;
  cursor: col-resize;
  background-color: #334155;
  flex-shrink: 0;
  z-index: 10;
  transition: background-color 0.1s;
}
.resizer-x:hover,
.resizer-x:active { background-color: #3b82f6; }

@container (max-width: 760px) {
  .slide-selector-wrapper {
    width: 100% !important;
    max-width: none;
    height: 132px;
    flex: 0 0 auto;
  }

  .thumbnail-list {
    display: flex;
    overflow-x: auto;
    overflow-y: hidden;
    padding: 8px;
    gap: 8px;
  }

  .slide-item {
    width: 136px;
    flex: 0 0 136px;
    margin-bottom: 0;
  }

  .resizer-x { display: none; }
}

@media (max-width: 900px) {
  .slide-selector-wrapper {
    width: 100% !important;
    max-width: none;
    height: 132px;
    flex: 0 0 auto;
  }

  .slide-selector { border-bottom: 1px solid #1e293b; }

  .thumbnail-list {
    display: flex;
    overflow-x: auto;
    overflow-y: hidden;
    padding: 8px;
    gap: 8px;
  }

  .slide-item {
    width: 136px;
    flex: 0 0 136px;
    margin-bottom: 0;
  }

  .resizer-x { display: none; }
}
</style>
