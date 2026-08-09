<template>
  <div class="shell-root" :class="{ 'overlay-sidebar': isOverlaySidebar, 'sidebar-open': !collapsed }">
    <transition name="sidebar-slide">
      <div v-if="!props.disableSidebar" v-show="!collapsed" class="sidebar-wrapper" aria-label="Side navigation">
        <Sidebar
          :key="sidebarKey"
          ref="sidebarRef"
          :user-email="userEmail"
          :active-project-id="activeProjectId"
          :active-run-id="activeRunId"
          @toggle="toggleSidebar"
          @new-project="handleNewProject"
          @select-project="handleSelectProject"
          @project-deleted="$emit('project-deleted', $event)"
        />
      </div>
    </transition>

    <div
      v-if="!props.disableSidebar && isOverlaySidebar && !collapsed"
      class="sidebar-backdrop"
      aria-hidden="true"
      @click="closeSidebar"
    ></div>

    <!-- Hamburger shown only when sidebar is collapsed -->
    <button
      v-if="!props.disableSidebar && collapsed"
      class="shell-hamburger"
      @click="openSidebar"
      aria-label="Open sidebar"
      title="Open sidebar"
    >
      <span class="bar"></span>
      <span class="bar"></span>
      <span class="bar"></span>
    </button>

    <main class="main-wrapper">
      <slot />
    </main>
  </div>
</template>

<script setup>
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import { apiRequest, API_ENDPOINTS } from '../config/api.js'
import Sidebar from './Sidebar.vue'

const props = defineProps({
  activeProjectId: { type: Number, default: null },
  activeRunId: { type: String, default: '' },
  disableSidebar: { type: Boolean, default: false },
})

const emit = defineEmits(['new-project', 'select-project', 'project-deleted'])
const OVERLAY_BREAKPOINT = 1100
const viewportWidth = ref(typeof window !== 'undefined' ? window.innerWidth : 1440)
const isOverlaySidebar = computed(() => viewportWidth.value < OVERLAY_BREAKPOINT)
const collapsed = ref(true)
const userEmail = ref('')
const sidebarKey = ref(0) // increment to force-remount Sidebar

let lastOverlayMode = isOverlaySidebar.value

const checkBreakpoint = () => {
  if (props.disableSidebar) {
    collapsed.value = true
    return
  }
  viewportWidth.value = window.innerWidth
  if (isOverlaySidebar.value !== lastOverlayMode) {
    // Resize should never open the sidebar by itself. Moving into overlay mode
    // closes it to avoid squeezing the workspace; moving back keeps user state.
    if (isOverlaySidebar.value) collapsed.value = true
    lastOverlayMode = isOverlaySidebar.value
  }
}

const toggleSidebar = () => {
  collapsed.value = !collapsed.value
}

const openSidebar = () => {
  collapsed.value = false
}

const closeSidebar = () => {
  collapsed.value = true
}

const handleNewProject = () => {
  emit('new-project')
  if (isOverlaySidebar.value) closeSidebar()
}

const handleSelectProject = (project) => {
  emit('select-project', project)
  if (isOverlaySidebar.value) closeSidebar()
}

const fetchUser = async () => {
  try {
    const me = await apiRequest(API_ENDPOINTS.ME)
    userEmail.value = me.email || ''
  } catch (e) {
    // silently ignore; sidebar will show empty email
  }
}

const sidebarRef = ref(null)

onMounted(() => {
  checkBreakpoint()
  if (!props.disableSidebar) {
    window.addEventListener('resize', checkBreakpoint)
    fetchUser()
  }
})

onBeforeUnmount(() => {
  if (!props.disableSidebar) {
    window.removeEventListener('resize', checkBreakpoint)
  }
})

// Expose refresh so VideoAbstract can call shellRef.value.refresh()
// Also increments sidebarKey to force a full Sidebar remount as a last resort.
const refresh = () => {
  console.log('[AppShell] Forwarding refresh to Sidebar, sidebarRef:', sidebarRef.value)
  sidebarKey.value++          // force remount — Sidebar's onMounted will call fetchProjects
}
defineExpose({ refresh })
</script>

<style scoped>
.shell-root {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: #181c24;
  position: relative;
}

.sidebar-backdrop {
  position: fixed;
  inset: 0;
  z-index: 250;
  background: rgba(0, 0, 0, 0.58);
  backdrop-filter: blur(1px);
}

/* Sidebar column */
.sidebar-wrapper {
  width: 260px;
  min-width: 260px;
  flex-shrink: 0;
  overflow: hidden;
  z-index: 300;
}

/* Slide transition */
.sidebar-slide-enter-active,
.sidebar-slide-leave-active {
  transition: width 0.25s ease, min-width 0.25s ease, opacity 0.2s ease;
}
.sidebar-slide-enter-from,
.sidebar-slide-leave-to {
  width: 0;
  min-width: 0;
  opacity: 0;
}

.overlay-sidebar .sidebar-wrapper {
  position: fixed;
  inset: 0 auto 0 0;
  width: min(86vw, 320px);
  min-width: 0;
  max-width: 320px;
  height: 100vh;
  box-shadow: 18px 0 48px rgba(0, 0, 0, 0.38);
}

.overlay-sidebar .sidebar-wrapper :deep(.sidebar) {
  width: 100%;
  min-width: 0;
}

.overlay-sidebar .sidebar-slide-enter-active,
.overlay-sidebar .sidebar-slide-leave-active {
  transition: transform 0.22s ease, opacity 0.18s ease;
}

.overlay-sidebar .sidebar-slide-enter-from,
.overlay-sidebar .sidebar-slide-leave-to {
  width: min(86vw, 320px);
  min-width: 0;
  transform: translateX(-100%);
  opacity: 0;
}

/* Main content column */
.main-wrapper {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  height: 100vh;
  display: flex;
  flex-direction: column;
}

/* Hamburger button shown when sidebar is collapsed */
.shell-hamburger {
  position: fixed;
  top: 14px;
  left: 14px;
  z-index: 240;
  display: flex;
  flex-direction: column;
  gap: 5px;
  background: #2d2f34;
  border: none;
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
  transition: background 0.2s;
}

.shell-hamburger:hover {
  background: #3a8dde;
}

.shell-hamburger .bar {
  display: block;
  width: 20px;
  height: 2px;
  background: #fff;
  border-radius: 2px;
}
</style>
