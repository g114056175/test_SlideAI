<template>
  <div class="waveform-player" :class="{ compact, loading, error: hasError }">
    <div class="player-top">
      <div class="meta">
        <span class="title">音訊預覽</span>
        <span class="time">{{ formatTime(currentTime) }} / {{ formatTime(duration) }}</span>
      </div>
      <div class="actions">
        <button v-if="src" class="icon-btn" type="button" title="下載音檔" aria-label="下載音檔" @click="downloadAudio">⤓</button>
        <button v-if="showDelete" class="icon-btn danger" type="button" title="刪除音檔" aria-label="刪除音檔" @click="emit('delete')">✕</button>
      </div>
    </div>

    <div ref="waveRef" class="wave"></div>

    <div v-if="showViewport" ref="viewportRef" class="viewport" @mousedown.prevent="onViewportTrackDown">
      <div class="viewport-window" :style="viewportWindowStyle" @mousedown.stop.prevent="onViewportWindowDown"></div>
    </div>

    <div class="control-deck" :class="{ 'no-speed': !showSpeed }">
      <button class="play" :disabled="loading || hasError" @click="togglePlay">
        <span v-if="loading">...</span>
        <span v-else-if="isPlaying">暫停</span>
        <span v-else>播放</span>
      </button>
      <button class="pill volume-pill" :class="{ active: volumeOpen }" @click="togglePanel('volume')">音量</button>
      <div
        v-if="volumeOpen || speedOpen"
        class="control-adjust-row"
        :class="{ 'align-volume': volumeOpen && !speedOpen, 'align-speed': speedOpen && !volumeOpen }"
      >
        <div v-if="volumeOpen" class="cluster">
          <span>{{ volumeIcon }}</span>
          <input v-model.number="volume" type="range" min="0" max="1" step="0.05" @input="applyVolume" />
          <span>{{ volumePercent }}%</span>
        </div>
        <div v-if="showSpeed && speedOpen" class="cluster speed">
          <input v-model.number="rate" type="range" min="0.5" max="2" step="0.05" @input="applyRate" />
          <input v-model.number="rate" class="rate-input" type="number" min="0.5" max="2" step="0.05" @change="applyRate" />
          <span>{{ rate.toFixed(2) }}x</span>
        </div>
      </div>
      <button v-if="showSpeed" class="pill speed-pill" :class="{ active: speedOpen }" @click="togglePanel('speed')">速度</button>
    </div>

    <div v-if="hasError" class="error-text">無法載入音訊</div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import WaveSurfer from 'wavesurfer.js'

const emit = defineEmits(['delete'])

const props = defineProps({
  src: { type: String, default: '' },
  downloadName: { type: String, default: '' },
  showSpeed: { type: Boolean, default: true },
  showDelete: { type: Boolean, default: false },
  compact: { type: Boolean, default: false },
})

const waveRef = ref(null)
const viewportRef = ref(null)
let ws = null
let resizeObs = null
let dragCleanup = null

const loading = ref(false)
const hasError = ref(false)
const isPlaying = ref(false)
const currentTime = ref(0)
const duration = ref(0)
const volume = ref(0.8)
const rate = ref(1)
const volumeOpen = ref(false)
const speedOpen = ref(false)
const viewportRatio = ref(0)
const viewportVisibleRatio = ref(1)
const canScroll = ref(false)
const MAX_VISIBLE_SECONDS = 30

const clamp = (value, min = 0, max = 1) => Math.max(min, Math.min(max, Number(value) || 0))
const volumePercent = computed(() => Math.round((Number(volume.value) || 0) * 100))
const showViewport = computed(() => canScroll.value && duration.value > MAX_VISIBLE_SECONDS)
const volumeIcon = computed(() => (volume.value <= 0.01 ? '🔇' : volume.value < 0.45 ? '🔈' : '🔊'))

const viewportWindowStyle = computed(() => {
  const visible = clamp(viewportVisibleRatio.value || 1, 0.04, 1)
  const maxLeft = 1 - visible
  return {
    width: `${visible * 100}%`,
    left: `${maxLeft > 0 ? viewportRatio.value * maxLeft * 100 : 0}%`,
  }
})

const resetState = () => {
  loading.value = false
  hasError.value = false
  isPlaying.value = false
  currentTime.value = 0
  duration.value = 0
  canScroll.value = false
  viewportRatio.value = 0
  viewportVisibleRatio.value = 1
}

const destroy = () => {
  clearDrag()
  if (resizeObs) {
    resizeObs.disconnect()
    resizeObs = null
  }
  if (ws) {
    ws.destroy()
    ws = null
  }
  resetState()
}

const computeZoom = () => {
  const dur = Number(duration.value) || 0
  const width = waveRef.value?.clientWidth || 900
  if (dur > 0 && dur <= MAX_VISIBLE_SECONDS) {
    // Fit short clips fully inside the visible wave area; avoid WaveSurfer's
    // internal horizontal scrollbar for 3-10s reference clips.
    return Math.max(1, (width - 12) / dur)
  }
  const visibleSeconds = Math.min(Math.max(dur, 1), MAX_VISIBLE_SECONDS)
  return Math.max(12, width / visibleSeconds)
}

const getWrapper = () => {
  if (!ws) return null
  return typeof ws.getWrapper === 'function' ? ws.getWrapper() : waveRef.value
}

const getWaveMetrics = () => {
  const wrapper = getWrapper()
  const width = typeof ws?.getWidth === 'function' ? ws.getWidth() : waveRef.value?.clientWidth || 0
  const maxScroll = Math.max(0, (wrapper?.scrollWidth || 0) - width)
  return { wrapper, width, maxScroll }
}

const syncViewport = () => {
  const { wrapper, width, maxScroll } = getWaveMetrics()
  if (!ws || !wrapper) return
  const scrollLeft = typeof ws.getScroll === 'function' ? ws.getScroll() : wrapper.scrollLeft || 0
  const longEnoughForViewport = Number(duration.value || 0) > MAX_VISIBLE_SECONDS
  canScroll.value = maxScroll > 8 && longEnoughForViewport
  viewportVisibleRatio.value = wrapper.scrollWidth > 0 ? clamp(width / wrapper.scrollWidth, 0.04, 1) : 1
  viewportRatio.value = maxScroll > 0 ? clamp(scrollLeft / maxScroll) : 0
  if (!longEnoughForViewport) wrapper.scrollLeft = 0
}

const applyViewport = () => {
  const { wrapper, maxScroll } = getWaveMetrics()
  if (!ws || !wrapper) return
  if (typeof ws.setScroll === 'function') ws.setScroll(maxScroll * viewportRatio.value)
}

const setViewportFromClientX = (clientX, center = false) => {
  const track = viewportRef.value
  if (!track) return
  const rect = track.getBoundingClientRect()
  const visible = clamp(viewportVisibleRatio.value || 1, 0.04, 1)
  const maxLeftPx = rect.width * (1 - visible)
  let leftPx = clientX - rect.left
  if (center) leftPx -= (rect.width * visible) / 2
  leftPx = Math.max(0, Math.min(maxLeftPx, leftPx))
  viewportRatio.value = maxLeftPx > 0 ? clamp(leftPx / maxLeftPx) : 0
  applyViewport()
}

const clearDrag = () => {
  if (dragCleanup) {
    dragCleanup()
    dragCleanup = null
  }
}

const startDrag = (onMove) => {
  clearDrag()
  const move = (event) => onMove(event.clientX)
  const up = () => clearDrag()
  window.addEventListener('mousemove', move)
  window.addEventListener('mouseup', up, { once: true })
  dragCleanup = () => {
    window.removeEventListener('mousemove', move)
    window.removeEventListener('mouseup', up)
  }
}

const onViewportTrackDown = (event) => {
  setViewportFromClientX(event.clientX, true)
  startDrag((clientX) => setViewportFromClientX(clientX, true))
}

const onViewportWindowDown = (event) => {
  const startX = event.clientX
  const startRatio = viewportRatio.value
  const track = viewportRef.value
  if (!track) return
  const rect = track.getBoundingClientRect()
  const visible = clamp(viewportVisibleRatio.value || 1, 0.04, 1)
  const maxLeftPx = rect.width * (1 - visible)
  startDrag((clientX) => {
    const deltaRatio = maxLeftPx > 0 ? (clientX - startX) / maxLeftPx : 0
    viewportRatio.value = clamp(startRatio + deltaRatio)
    applyViewport()
  })
}

const build = async () => {
  destroy()
  await nextTick()
  if (!props.src || !waveRef.value) return

  loading.value = true
  ws = WaveSurfer.create({
    container: waveRef.value,
    waveColor: '#38bdf8',
    progressColor: '#f59e0b',
    cursorColor: '#dbeafe',
    cursorWidth: 2,
    barWidth: 3,
    barGap: 2,
    barRadius: 999,
    height: props.compact ? 54 : 70,
    normalize: true,
    interact: true,
    dragToSeek: true,
    minPxPerSec: 30,
    scrollParent: true,
    backend: 'MediaElement',
  })

  ws.on('ready', () => {
    loading.value = false
    duration.value = ws.getDuration()
    applyVolume()
    applyRate()
    nextTick(() => {
      ws.zoom(computeZoom())
      requestAnimationFrame(syncViewport)
      if (!resizeObs && waveRef.value) {
        resizeObs = new ResizeObserver(() => {
          if (!ws || !duration.value) return
          ws.zoom(computeZoom())
          requestAnimationFrame(syncViewport)
        })
        resizeObs.observe(waveRef.value)
      }
    })
  })

  const syncTime = () => { currentTime.value = ws.getCurrentTime() }
  ws.on('audioprocess', syncTime)
  ws.on('timeupdate', syncTime)
  ws.on('seeking', syncTime)
  ws.on('seek', syncTime)
  ws.on('interaction', syncTime)
  ws.on('scroll', syncViewport)
  ws.on('play', () => { isPlaying.value = true })
  ws.on('pause', () => { isPlaying.value = false })
  ws.on('finish', () => {
    isPlaying.value = false
    currentTime.value = duration.value
  })
  ws.on('error', () => {
    loading.value = false
    hasError.value = true
  })

  ws.load(props.src)
}

const togglePlay = () => {
  if (ws) ws.playPause()
}

const downloadAudio = async () => {
  if (!props.src) return
  const name = props.downloadName || 'audio.wav'
  try {
    const res = await fetch(props.src)
    if (!res.ok) throw new Error(`download failed: ${res.status}`)
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = name
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (_) {
    const a = document.createElement('a')
    a.href = props.src
    a.download = name
    a.target = '_blank'
    document.body.appendChild(a)
    a.click()
    a.remove()
  }
}

const togglePanel = (panel) => {
  const openVolume = panel === 'volume' && !volumeOpen.value
  const openSpeed = props.showSpeed && panel === 'speed' && !speedOpen.value
  volumeOpen.value = openVolume
  speedOpen.value = openSpeed
}

const applyVolume = () => {
  volume.value = clamp(volume.value)
  if (ws) ws.setVolume(volume.value)
}

const applyRate = () => {
  rate.value = clamp(rate.value || 1, 0.5, 2)
  if (!ws) return
  ws.setPlaybackRate(rate.value)
  try {
    const media = ws.getMediaElement()
    media.preservesPitch = true
    media.mozPreservesPitch = true
    media.webkitPreservesPitch = true
  } catch (_) {}
}

const formatTime = (sec) => {
  const value = Number(sec)
  if (!Number.isFinite(value) || value <= 0) return '0:00'
  return `${Math.floor(value / 60)}:${Math.floor(value % 60).toString().padStart(2, '0')}`
}

watch(() => props.src, build, { immediate: true })

onBeforeUnmount(destroy)
</script>

<style scoped>
.waveform-player {
  width: 100%;
  min-width: 0;
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border-radius: 14px;
  border: 1px solid rgba(56, 189, 248, 0.22);
  background:
    linear-gradient(180deg, rgba(10, 24, 45, 0.98), rgba(7, 18, 34, 0.98)),
    radial-gradient(circle at top left, rgba(56, 189, 248, 0.12), transparent 34%);
}

.player-top,
.control-adjust-row,
.meta,
.actions,
.cluster {
  display: flex;
  align-items: center;
}

.player-top {
  justify-content: space-between;
  gap: 10px;
}

.meta {
  min-width: 0;
  gap: 10px;
}

.title {
  color: #e5e7eb;
  font-size: 12px;
  font-weight: 800;
}

.time {
  color: #93c5fd;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 30px;
  min-height: 28px;
  color: #dbeafe;
  background: rgba(15, 23, 42, 0.35);
  text-decoration: none;
  border: 1px solid rgba(148, 163, 184, 0.28);
  border-radius: 8px;
  padding: 2px 8px;
  cursor: pointer;
}

.actions {
  gap: 6px;
  flex: 0 0 auto;
}

.icon-btn.danger {
  color: #fecaca;
  border-color: rgba(248, 113, 113, 0.42);
}

.icon-btn:hover {
  background: rgba(59, 130, 246, 0.18);
}

.wave {
  min-height: 82px;
  border-radius: 12px;
  background: #07111f;
  overflow: hidden;
}

.wave :deep(*) {
  scrollbar-width: none;
}

.wave :deep(*::-webkit-scrollbar) {
  width: 0;
  height: 0;
}

.compact .wave {
  min-height: 62px;
}

.viewport {
  position: relative;
  height: 12px;
  border-radius: 999px;
  background: rgba(15, 23, 42, 0.92);
  border: 1px solid rgba(148, 163, 184, 0.18);
  cursor: pointer;
}

.viewport-window {
  position: absolute;
  top: 1px;
  bottom: 1px;
  border-radius: 999px;
  background: linear-gradient(90deg, #38bdf8, #f59e0b);
  box-shadow: 0 0 0 1px rgba(248, 250, 252, 0.16);
  cursor: grab;
}

.viewport-window:active {
  cursor: grabbing;
}

.control-deck {
  display: grid;
  grid-template-columns: max-content max-content minmax(180px, 1fr) max-content;
  grid-template-areas: "play volume adjust speed";
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.control-deck.no-speed {
  grid-template-columns: max-content max-content minmax(180px, 1fr);
  grid-template-areas: "play volume adjust";
}

.play { grid-area: play; }
.volume-pill { grid-area: volume; }
.speed-pill { grid-area: speed; }
.control-adjust-row { grid-area: adjust; }

.control-adjust-row {
  gap: 8px;
  min-width: 0;
  flex-wrap: nowrap;
  padding: 4px 8px;
  border: 1px solid rgba(59, 130, 246, 0.18);
  border-radius: 12px;
  background: rgba(15, 23, 42, 0.55);
}

.control-adjust-row.align-volume {
  justify-content: flex-start;
}

.control-adjust-row.align-speed {
  justify-content: flex-end;
}

button {
  border: 0;
  cursor: pointer;
  font: inherit;
}

.play,
.pill {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 54px;
  min-height: 36px;
  border-radius: 999px;
  padding: 6px 12px;
  color: #e5e7eb;
  background: #1e293b;
  white-space: nowrap;
  line-height: 1;
  writing-mode: horizontal-tb;
  word-break: keep-all;
  overflow: hidden;
}

.play {
  background: linear-gradient(135deg, #0ea5e9, #2563eb);
  color: #fff;
  font-weight: 800;
}

.pill.active {
  background: rgba(14, 165, 233, 0.24);
  color: #7dd3fc;
}

.cluster {
  min-width: 0;
  flex: 0 1 260px;
  max-width: 320px;
  gap: 8px;
  color: #cbd5e1;
  font-size: 12px;
}

.cluster input[type='range'] {
  flex: 1 1 130px;
  min-width: 90px;
  max-width: 180px;
  width: auto;
}

.rate-input {
  flex: 0 0 68px;
  width: 68px;
  border: 1px solid #334155;
  border-radius: 8px;
  padding: 4px 6px;
  color: #e5e7eb;
  background: #0f172a;
}

.error-text {
  color: #fecaca;
  font-size: 12px;
}

@container (max-width: 430px) {
  .control-deck {
    grid-template-columns: auto auto 1fr auto;
    grid-template-areas:
      "play volume . speed"
      "adjust adjust adjust adjust";
  }

  .control-deck.no-speed {
    grid-template-columns: auto auto 1fr;
    grid-template-areas:
      "play volume ."
      "adjust adjust adjust";
  }

  .control-adjust-row,
  .control-adjust-row.align-volume,
  .control-adjust-row.align-speed {
    justify-content: center;
    flex-wrap: wrap;
  }

  .cluster {
    flex-basis: 100%;
    max-width: none;
  }

  .cluster input[type='range'] {
    flex: 1 1 auto;
    max-width: none;
    width: auto;
  }
}

@media (max-width: 560px) {
  .play,
  .pill {
    min-width: 50px;
    min-height: 34px;
    padding: 6px 10px;
  }
}
</style>
