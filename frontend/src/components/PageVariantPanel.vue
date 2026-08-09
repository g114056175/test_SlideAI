<template>
  <aside class="variant-panel">
    <div class="variant-header">
      <div>
        <div class="variant-kicker">{{ kicker || '產出影片' }}</div>
        <div class="variant-title">{{ title || `第 ${pageIndex + 1} 頁` }}</div>
      </div>
      <span class="variant-count">{{ variants.length }}</span>
    </div>

    <div v-if="!runId" class="variant-empty">
      尚未建立持久 run。重新上傳 PDF 後會自動啟用。
    </div>
    <div v-else-if="!variants.length" class="variant-empty">
      此頁尚無產出影片。
    </div>
    <div v-else class="variant-list">
      <button
        v-for="(variant, idx) in variants"
        :key="variant.variant_id"
        type="button"
        class="variant-item"
        :class="{ active: variant.variant_id === selectedVariantId }"
        @click="$emit('select', variant.variant_id)"
        :title="variant.variant_id === selectedVariantId ? '目前選用影片' : '點擊切換為此影片'"
      >
        <div class="video-line">
          <span class="variant-name">#{{ idx + 1 }}</span>
          <span class="variant-meta">{{ formatVariantTime(variant.created_at) }}</span>
          <span v-if="variant.variant_id === selectedVariantId" class="variant-main-badge">目前</span>
          <button
            type="button"
            class="variant-delete"
            title="刪除此影片"
            @click.stop="$emit('delete', variant.variant_id)"
          >×</button>
        </div>
      </button>
    </div>
    <details v-if="selectedChunks.length" class="chunk-repair">
      <summary>局部語音修正（{{ selectedChunks.length }} 段）</summary>
      <div class="chunk-list">
        <div v-for="chunk in selectedChunks" :key="chunk.index" class="chunk-item">
          <div class="chunk-label">第 {{ Number(chunk.index) + 1 }} 段 · {{ Number(chunk.duration || 0).toFixed(1) }} 秒</div>
          <div class="chunk-text">{{ chunk.text }}</div>
          <button
            type="button"
            class="chunk-retry"
            :disabled="chunkRegenerating || !String(chunk.text || '').trim()"
            @click="$emit('regenerate-chunk', {
              variantId: selectedVariantId,
              chunkIndex: Number(chunk.index),
              text: String(chunk.text || '').trim(),
            })"
          >{{ chunkRegenerating ? '處理中…' : '重生此段' }}</button>
        </div>
      </div>
    </details>
  </aside>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  runId: { type: String, default: '' },
  pageIndex: { type: Number, default: 0 },
  variants: { type: Array, default: () => [] },
  selectedVariantId: { type: String, default: '' },
  title: { type: String, default: '' },
  kicker: { type: String, default: '' },
  chunkRegenerating: { type: Boolean, default: false },
})

defineEmits(['select', 'delete', 'regenerate-chunk'])

const selectedVariant = computed(() => props.variants.find(
  (variant) => variant.variant_id === props.selectedVariantId,
) || null)
const selectedChunks = computed(() => selectedVariant.value?.tts?.chunks || [])

const formatVariantTime = (value) => {
  if (!value) return '無時間紀錄'
  try {
    return new Date(value).toLocaleString(undefined, {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
      hour12: false,
    })
  } catch {
    return String(value)
  }
}
</script>

<style scoped>
.variant-panel {
  width: 210px;
  flex: 0 0 210px;
  min-width: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 8px;
  background: #0b1220;
  border-right: 1px solid #1e293b;
  color: #e2e8f0;
  overflow: hidden;
}

.variant-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid #1e293b;
  padding-bottom: 8px;
}

.variant-kicker {
  color: #38bdf8;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.08em;
}

.variant-title {
  font-size: 15px;
  font-weight: 800;
}

.variant-count {
  min-width: 30px;
  height: 26px;
  padding: 0 8px;
  border-radius: 999px;
  display: grid;
  place-items: center;
  background: rgba(14, 165, 233, 0.16);
  color: #7dd3fc;
  font-weight: 900;
}

.variant-empty {
  border: 1px dashed #334155;
  border-radius: 10px;
  padding: 12px;
  color: #94a3b8;
  font-size: 13px;
  line-height: 1.5;
  text-align: center;
}

.variant-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  overflow-y: auto;
  padding-right: 2px;
}

.chunk-repair {
  border-top: 1px solid #1e293b;
  padding-top: 7px;
  color: #bae6fd;
  font-size: 12px;
}

.chunk-repair summary { cursor: pointer; font-weight: 800; }
.chunk-list { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; overflow-y: auto; }
.chunk-item { border: 1px solid #26364d; border-radius: 8px; padding: 6px; background: #0f172a; }
.chunk-label { color: #94a3b8; margin-bottom: 4px; }
.chunk-text {
  max-height: 72px; overflow-y: auto; border: 1px solid #334155;
  border-radius: 6px; padding: 5px; color: #e2e8f0; background: #020617;
  font-size: 11px; line-height: 1.45;
}
.chunk-retry {
  width: 100%; margin-top: 5px; border: 1px solid #0369a1; border-radius: 6px;
  padding: 5px; color: #e0f2fe; background: #0c4a6e;
}

.variant-item {
  width: 100%;
  text-align: left;
  border: 1px solid #26364d;
  border-radius: 9px;
  background: #111827;
  color: #e2e8f0;
  padding: 7px 8px;
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
}

.variant-item:hover { border-color: #38bdf8; }
.variant-item.active {
  border-color: #22c55e;
  background: rgba(21, 128, 61, 0.16);
}

.video-line {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.variant-name {
  font-size: 13px;
  font-weight: 800;
  color: #f8fafc;
  flex: 0 0 auto;
}

.variant-main-badge {
  font-size: 10px;
  font-weight: 900;
  color: #052e16;
  background: #86efac;
  padding: 1px 5px;
  border-radius: 999px;
  flex: 0 0 auto;
}

.variant-meta {
  font-size: 11px;
  color: #94a3b8;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1 1 auto;
  min-width: 0;
}

.variant-delete {
  width: 22px;
  height: 22px;
  line-height: 18px;
  display: grid;
  place-items: center;
  border-radius: 999px;
  border-color: rgba(248, 113, 113, 0.5);
  border: 1px solid rgba(248, 113, 113, 0.5);
  background: rgba(127, 29, 29, 0.18);
  color: #fecaca;
  font-size: 17px;
  font-weight: 900;
  padding: 0;
  flex: 0 0 auto;
}

@container (max-width: 1180px) {
  .variant-panel {
    width: 100%;
    flex: 0 0 auto;
    height: auto;
    max-height: 150px;
    border-right: 0;
    border-bottom: 1px solid #1e293b;
  }

  .variant-list {
    flex-direction: row;
    overflow-x: auto;
    overflow-y: hidden;
  }

  .variant-item {
    min-width: 150px;
    flex: 0 0 150px;
  }
}
</style>
