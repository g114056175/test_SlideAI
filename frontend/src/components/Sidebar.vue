<template>
  <aside class="sidebar">
    <!-- Header: toggle + brand -->
    <div class="sidebar-header">
      <button class="btn-icon" @click="$emit('toggle')" aria-label="Close sidebar" title="Close sidebar">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="3" y1="12" x2="21" y2="12" />
          <line x1="3" y1="6"  x2="21" y2="6"  />
          <line x1="3" y1="18" x2="21" y2="18" />
        </svg>
      </button>
      <span class="brand">SlideAI</span>
    </div>

    <!-- New Project -->
    <div class="sidebar-action">
      <button class="btn-new-project" @click="$emit('new-project')">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5"  y1="12" x2="19" y2="12" />
        </svg>
        New Project
      </button>
    </div>

    <!-- Project History -->
    <div class="sidebar-section-label">Recent Projects</div>
    <div class="sidebar-list">
      <div v-if="loadingRuns" class="sidebar-empty">Loading...</div>
      <div v-else-if="videoRuns.length === 0" class="sidebar-empty">No projects yet.</div>
      <div
        v-for="run in videoRuns"
        :key="run.run_id"
        class="sidebar-item run-item"
        :class="{ editing: editingRunId === run.run_id, active: activeRunId === run.run_id }"
        :title="run.display_name || run.original_filename || run.run_id"
        @click="$emit('select-project', { ...run, is_video_run: true })"
      >
        <template v-if="editingRunId === run.run_id">
          <input
            v-model="editingRunName"
            class="run-rename-input"
            maxlength="120"
            @click.stop
            @keydown.enter.prevent="saveRunName(run)"
            @keydown.esc.prevent="cancelRunRename"
          />
          <button class="btn-run-action" @click.stop="saveRunName(run)" aria-label="Save run name">✓</button>
          <button class="btn-run-action" @click.stop="cancelRunRename" aria-label="Cancel rename">×</button>
        </template>
        <template v-else>
          <span class="item-name">{{ run.display_name || run.original_filename || run.run_id }}</span>
          <span class="item-date">{{ run.rendered_pages || 0 }}/{{ run.page_count || 0 }}</span>
          <a
            class="btn-run-action"
            :href="getRunPdfUrl(run)"
            target="_blank"
            rel="noopener"
            download
            title="下載此專案 PDF"
            aria-label="Download project PDF"
            @click.stop
          >↓</a>
          <button class="btn-run-action" @click.stop="startRunRename(run)" aria-label="Rename run">✎</button>
          <button class="btn-delete" @click.stop="deleteVideoRun(run)" aria-label="Delete run">x</button>
        </template>
      </div>
    </div>

    <!-- Footer -->
    <div class="sidebar-footer">
      <div class="footer-email" :title="userEmail">{{ userEmail }}</div>
      <button class="btn-logout" @click="logout">Logout</button>
    </div>
  </aside>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { apiRequest, getApiEndpoint, clearEndpointCache } from '../config/api.js'
import { emitter } from '../config/events.js'

const props = defineProps({
  userEmail: { type: String, default: '' },
  activeProjectId: { type: Number, default: null },
  activeRunId: { type: String, default: '' },
})

const emit = defineEmits(['toggle', 'new-project', 'select-project', 'project-deleted'])

const router = useRouter()
const videoRuns = ref([])
const loadingRuns = ref(false)
const editingRunId = ref('')
const editingRunName = ref('')
let refreshTimer = null

const fetchVideoRuns = async () => {
  clearEndpointCache('/api/video-runs')
  loadingRuns.value = true
  try {
    const data = await apiRequest('/api/video-runs')
    videoRuns.value = Array.isArray(data?.runs) ? data.runs : []
  } catch (e) {
    videoRuns.value = []
  } finally {
    loadingRuns.value = false
  }
}

const scheduleFetchVideoRuns = () => {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    fetchVideoRuns()
  }, 120)
}

const deleteVideoRun = async (run) => {
  const confirmed = window.confirm(
    `Delete project "${run.display_name || run.original_filename || run.run_id}"?\n\nThis will permanently remove its PDF copy, audio, subtitles, rendered videos, and manifest from data/video_runs.`
  )
  if (!confirmed) return
  try {
    const token = localStorage.getItem('token')
    const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(run.run_id)}`), {
      method: 'DELETE',
      headers: token ? { Authorization: 'Bearer ' + token } : {},
    })
    if (res.ok) {
      videoRuns.value = videoRuns.value.filter((item) => item.run_id !== run.run_id)
      clearEndpointCache('/api/video-runs')
      emitter.emit('refresh-video-runs')
      emit('project-deleted', run.run_id)
    }
  } catch (e) {
    // silently ignore network errors
  }
}

const startRunRename = (run) => {
  editingRunId.value = run.run_id
  editingRunName.value = run.display_name || run.original_filename || run.run_id
}

const getRunPdfUrl = (run) => getApiEndpoint(`/api/video-runs/${encodeURIComponent(run.run_id)}/pdf`)

const cancelRunRename = () => {
  editingRunId.value = ''
  editingRunName.value = ''
}

const saveRunName = async (run) => {
  const nextName = String(editingRunName.value || '').trim()
  if (!nextName) return
  try {
    const token = localStorage.getItem('token')
    const res = await fetch(getApiEndpoint(`/api/video-runs/${encodeURIComponent(run.run_id)}`), {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: 'Bearer ' + token } : {}),
      },
      body: JSON.stringify({ display_name: nextName }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || 'Rename failed')
    videoRuns.value = videoRuns.value.map((item) =>
      item.run_id === run.run_id ? { ...item, display_name: data.display_name || nextName } : item
    )
    cancelRunRename()
    clearEndpointCache('/api/video-runs')
    emitter.emit('refresh-video-runs')
  } catch (e) {
    // Keep edit mode so the user can retry.
  }
}

const logout = () => {
  localStorage.removeItem('token')
  router.push('/login')
}

// Format ISO date string as a readable relative/short form
const formatDate = (iso) => {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    const now = new Date()
    const diffMs = now - d
    const diffDays = Math.floor(diffMs / 86400000)
    if (diffDays === 0) return 'Today'
    if (diffDays === 1) return 'Yesterday'
    if (diffDays < 7) return `${diffDays} days ago`
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
  } catch (e) {
    return ''
  }
}

onMounted(() => {
  console.log('Sidebar Component Mounted - Fetching Video Runs')
  fetchVideoRuns()
  emitter.on('refresh-video-runs', scheduleFetchVideoRuns)
  window.addEventListener('refresh-sidebar', scheduleFetchVideoRuns)
})

onUnmounted(() => {
  if (refreshTimer) clearTimeout(refreshTimer)
  emitter.off('refresh-video-runs', scheduleFetchVideoRuns)
  window.removeEventListener('refresh-sidebar', scheduleFetchVideoRuns)
})

// Expose at the very end so every function above is already declared
const refresh = () => {
  fetchVideoRuns()
}
defineExpose({ refresh, fetchVideoRuns })
</script>

<style scoped>
/* ---- Root ---- */
.sidebar {
  width: 260px;
  min-width: 260px;
  height: 100vh;
  background: #202123;
  display: flex;
  flex-direction: column;
  border-right: 1px solid rgba(255, 255, 255, 0.06);
  color: #ececec;
  font-family: 'Inter', 'Segoe UI', system-ui, sans-serif;
  overflow: hidden;
  user-select: none;
}

/* ---- Header ---- */
.sidebar-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.brand {
  font-size: 1.1rem;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #fff;
}

/* ---- New Project button ---- */
.sidebar-action {
  padding: 12px 12px 4px;
}

.btn-new-project {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 10px 14px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  color: #ececec;
  font-size: 0.9rem;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.18s, border-color 0.18s;
}

.btn-new-project:hover {
  background: rgba(58, 141, 222, 0.18);
  border-color: #3a8dde;
  color: #fff;
}

/* ---- Section label ---- */
.sidebar-section-label {
  padding: 14px 16px 4px;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: #6b7280;
  font-weight: 600;
}

/* ---- List ---- */
/* Reset any browser-default ul/ol/li padding that creates stray left space */
.sidebar-list ul,
.sidebar-list ol {
  list-style: none;
  margin: 0;
  padding: 0;
}

.sidebar-list {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 4px 8px 8px;
  scrollbar-width: thin;
  scrollbar-color: #3a3b3d transparent;
}

.local-runs-label {
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  margin-top: 4px;
}

.local-runs-list {
  flex: 0 0 auto;
  max-height: 190px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

.run-item .item-date {
  color: #7dd3fc;
  font-weight: 800;
}

.run-rename-input {
  flex: 1;
  min-width: 0;
  height: 28px;
  border: 1px solid rgba(96, 165, 250, 0.65);
  border-radius: 6px;
  background: #111827;
  color: #f8fafc;
  padding: 4px 7px;
  font-size: 0.82rem;
  outline: none;
}

.btn-run-action {
  flex-shrink: 0;
  display: none;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border: 1px solid rgba(148, 163, 184, 0.35);
  border-radius: 5px;
  background: rgba(15, 23, 42, 0.8);
  color: #cbd5e1;
  cursor: pointer;
  font-size: 0.75rem;
  line-height: 1;
}

.run-item:hover .btn-run-action,
.run-item:focus-within .btn-run-action,
.run-item.editing .btn-run-action {
  display: inline-flex;
}

.btn-run-action:hover {
  border-color: #60a5fa;
  color: #fff;
}

.sidebar-list::-webkit-scrollbar {
  width: 4px;
}
.sidebar-list::-webkit-scrollbar-thumb {
  background: #3a3b3d;
  border-radius: 4px;
}

.sidebar-empty {
  padding: 16px;
  font-size: 0.85rem;
  color: #6b7280;
  text-align: center;
}

/* ---- List items: nuclear CSS reset to prevent any browser default spacing ---- */
.sidebar-list,
.sidebar-list *,
.sidebar-list *::before,
.sidebar-list *::after {
  box-sizing: border-box;
}

.sidebar-list ul,
.sidebar-list ol,
.sidebar-list li {
  list-style: none !important;
  margin: 0 !important;
  padding: 0 !important;
}

.sidebar-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px 8px 12px;
  border-radius: 7px;
  cursor: pointer;
  transition: background 0.15s;
  position: relative;
  min-width: 0;
  margin: 0;
  list-style: none;
  border: none;
  background: transparent;
  width: 100%;
  text-align: left;
  color: inherit;
  box-sizing: border-box;
}

/* Force-override any inheritance that could create blank space */
.project-item {
  display: flex !important;
  align-items: center !important;
  justify-content: space-between !important;
  text-align: left !important;
  padding: 8px 6px 8px 12px !important;
  margin: 0 !important;
}

.sidebar-item:hover {
  background: rgba(255, 255, 255, 0.07);
}

.sidebar-item.active {
  background: rgba(37, 99, 235, 0.24);
  box-shadow: inset 3px 0 0 #60a5fa;
}

.sidebar-item.active .item-name {
  color: #fff;
}

.sidebar-item.active .item-date {
  color: #bae6fd;
}

.item-icon {
  color: #6b7280;
  flex-shrink: 0;
  display: flex;
  align-items: center;   /* center vertically with the text line */
  justify-content: center;
  width: 16px;
  height: 16px;
}

.item-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.item-name {
  flex: 1;
  font-size: 0.875rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: #d1d5db;
  line-height: 1.3;
  min-width: 0;
}

.item-meta {
  display: flex;
  align-items: center;
  gap: 4px;
}

.item-date {
  font-size: 0.72rem;
  color: #6b7280;
}

.badge-processing {
  font-size: 0.65rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: #f59e0b;
  background: rgba(245, 158, 11, 0.12);
  border: 1px solid rgba(245, 158, 11, 0.3);
  border-radius: 4px;
  padding: 1px 5px;
  text-transform: uppercase;
}

.btn-delete {
  flex-shrink: 0;
  display: none;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: #6b7280;
  cursor: pointer;
  padding: 3px 6px;
  border-radius: 4px;
  font-size: 0.75rem;
  line-height: 1;
  transition: color 0.15s, background 0.15s;
  margin-left: auto;
}

.sidebar-item:hover .btn-delete {
  display: flex;
}

.btn-delete:hover {
  color: #f87171;
  background: rgba(248, 113, 113, 0.12);
}

/* ---- Footer ---- */
.sidebar-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 14px 14px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.footer-email {
  font-size: 0.8rem;
  color: #9ca3af;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.btn-logout {
  font-size: 0.8rem;
  padding: 5px 12px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 6px;
  color: #d1d5db;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}

.btn-logout:hover {
  background: rgba(248, 113, 113, 0.15);
  border-color: #f87171;
  color: #f87171;
}

/* ---- Generic icon button ---- */
.btn-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  background: none;
  border: none;
  color: #9ca3af;
  cursor: pointer;
  padding: 6px;
  border-radius: 6px;
  transition: color 0.15s, background 0.15s;
}

.btn-icon:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.08);
}
</style>
