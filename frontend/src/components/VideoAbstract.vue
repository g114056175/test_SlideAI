
<template>
  <AppShell
    :disable-sidebar="labMode"
    :active-project-id="projectId"
    @new-project="resetProject"
    @select-project="onSelectProject"
    @project-deleted="onProjectDeleted"
    ref="shellRef"
  >
    <div class="va-bg">
        <div class="container">
            <div class="va-card card shadow-lg p-4" style="max-width: 1200px; width: 100%; margin: 40px auto 0 auto;">
                <h2 class="mb-3 text-center va-title">AI語音簡報</h2>
                <p class="text-center mb-4 va-desc">請上傳一份PDF檔案，AI會自動生成語音簡報。</p>

                <div v-if="step === 1">
                        <!-- 使用次數顯示 已移除 -->

                    <form @submit.prevent="handleUpload" class="mb-3">
                        <label class="va-upload-area w-100 mb-3" @dragover.prevent @drop.prevent="onDrop">
                            <input type="file" accept="application/pdf" @change="onFileChange" class="d-none" ref="fileInput" />
                            <div class="va-upload-content text-center">
                                <div v-if="!pdfFile">
                                    <span class="va-upload-icon">📄</span>
                                    <div class="va-upload-text">點擊或拖曳PDF檔案到這裡</div>
                                    <div class="va-upload-tip">僅支援 PDF，最大 20MB</div>
                                </div>
                                <div v-else>
                                    <span class="va-upload-icon">✅</span>
                                    <div class="va-upload-text">{{ pdfFile.name }}</div>
                                </div>
                            </div>
                        </label>
                        <div class="mb-3">
                            <label for="language-voice" class="form-label">選擇語音</label>
                            <select id="language-voice" v-model="combinedSelection" class="form-select voice-select">
                                <option value="zh:zh-TW-YunJheNeural">中文（台灣）</option>
                                <option value="en:en-US-GuyNeural">English (US Male)</option>
                                <option value="en:en-US-AriaNeural">English (US Female)</option>
                            </select>
                        </div>
                        <button class="btn btn-dark-main w-100 py-2" type="submit" :disabled="!pdfFile || loading">
                            <span v-if="loading" class="spinner-border spinner-border-sm me-2"></span>
                            {{ loading ? '上傳中...' : '上傳' }}
                        </button>
                    </form>
                    <div v-if="error" class="alert alert-danger mt-3 text-center va-result">{{ error }}</div>
                </div>

                                <div v-else-if="step === 2">
                                        <h4 class="mb-3 text-center">AI 產生的簡報稿（可編輯）</h4>
                                        <form @submit.prevent="handleGenerateVideo">
                                                <div id="pages-accordion">
                                                    <div v-for="(text, idx) in aiTexts" :key="idx" class="accordion-item mb-2">
                                                        <h2 class="accordion-header" :id="`heading-${idx}`">
                                                            <button
                                                                class="accordion-button"
                                                                :class="{ collapsed: !expandedPages[idx] }"
                                                                type="button"
                                                                @click="togglePage(idx)"
                                                                :aria-expanded="expandedPages[idx] ? 'true' : 'false'"
                                                                :aria-controls="`collapse-${idx}`"
                                                            >
                                                                    <div class="d-flex flex-column align-items-center w-100">
                                                                        <strong class="page-number">第 {{ idx + 1 }} 頁</strong>
                                                                        <small class="text-muted mt-1">（{{ text.trim().split('\n').filter(Boolean).length }} 行）</small>
                                                                        <!-- thumbnails are fetched automatically when entering step 2 -->
                                                                    </div>
                                                            </button>
                                                        </h2>
                                                        <div :id="`collapse-${idx}`" class="accordion-collapse collapse" :class="{ show: expandedPages[idx] }" :aria-labelledby="`heading-${idx}`">
                                                            <div class="accordion-body">
                                                                <div class="d-flex flex-column">
                                                                    <div class="mb-0">
                                                                        <div class="thumbnail-box border rounded text-center">
                                                                            <img v-if="pageThumbnails[idx]" :src="pageThumbnails[idx]" alt="thumbnail" />
                                                                            <div v-else class="w-100">
                                                                                <svg width="100%" height="220" xmlns="http://www.w3.org/2000/svg">
                                                                                    <rect width="100%" height="100%" fill="#f8f9fa" stroke="#e9ecef" />
                                                                                    <text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" fill="#6c757d" font-size="20">第 {{ idx + 1 }} 頁</text>
                                                                                </svg>
                                                                                <div class="small text-muted mt-1">縮圖暫不可用</div>
                                                                            </div>
                                                                        </div>
                                                                        <div class="thumbnail-separator" aria-hidden="true"></div>
                                                                    </div>
                                                                    <div>
                                                                        <label :for="`page-${idx}`">文字編輯</label>
                                                                        <textarea
                                                                            :id="`page-${idx}`"
                                                                            v-model="aiTexts[idx]"
                                                                            class="form-control page-textarea"
                                                                            style="resize: vertical; width: 100%; overflow:auto;"
                                                                            @input="(e) => onTextareaInput(e, idx)"
                                                                        ></textarea>
                                                                        <div class="page-separator" aria-hidden="true"></div>
                                                                    </div>
                                                                </div>
                                                            </div>
                                                        </div>
                                                    </div>
                                                </div>
                                                <div>
                                                    <button class="btn btn-dark-main w-100 py-2 mt-3" type="submit" :disabled="videoLoading || aiTexts.length === 0">
                                                        <span v-if="videoLoading" class="spinner-border spinner-border-sm me-2"></span>
                                                        {{ videoLoading ? '產生並下載中...' : '產生並下載影片' }}
                                                    </button>
                                                </div>
                                        </form>
                                                <div v-if="videoError" class="alert alert-danger mt-3 text-center va-result">{{ videoError }}</div>
                                        
                                        </div>

                                <!-- Step 3: Completed project preview -->
                                <div v-else-if="step === 3">
                                    <div class="va-step3-header">
                                        <div class="va-step3-title">{{ activeProjectName }}</div>
                                        <div class="va-step3-subtitle">This project was completed. You can edit the script below and re-generate the video.</div>
                                    </div>
                                    <form @submit.prevent="handleGenerateVideo">
                                        <div id="pages-accordion-s3">
                                            <div v-for="(text, idx) in aiTexts" :key="idx" class="accordion-item mb-2">
                                                <h2 class="accordion-header" :id="`s3-heading-${idx}`">
                                                    <button
                                                        class="accordion-button"
                                                        :class="{ collapsed: !expandedPages[idx] }"
                                                        type="button"
                                                        @click="togglePage(idx)"
                                                        :aria-expanded="expandedPages[idx] ? 'true' : 'false'"
                                                    >
                                                        <div class="d-flex flex-column align-items-center w-100">
                                                            <strong class="page-number">Page {{ idx + 1 }}</strong>
                                                            <small class="text-muted mt-1">（{{ text.trim().split('\n').filter(Boolean).length }} lines）</small>
                                                        </div>
                                                    </button>
                                                </h2>
                                                <div class="accordion-collapse collapse" :class="{ show: expandedPages[idx] }">
                                                    <div class="accordion-body">
                                                        <div class="d-flex flex-column">
                                                            <!-- Thumbnail -->
                                                            <div class="mb-0">
                                                                <div class="thumbnail-box border rounded text-center">
                                                                    <img v-if="pageThumbnails[idx]" :src="pageThumbnails[idx]" alt="thumbnail" />
                                                                    <div v-else class="w-100">
                                                                        <svg width="100%" height="220" xmlns="http://www.w3.org/2000/svg">
                                                                            <rect width="100%" height="100%" fill="#f8f9fa" stroke="#e9ecef" />
                                                                            <text x="50%" y="45%" dominant-baseline="middle" text-anchor="middle" fill="#6c757d" font-size="18">Page {{ idx + 1 }}</text>
                                                                            <text x="50%" y="60%" dominant-baseline="middle" text-anchor="middle" fill="#adb5bd" font-size="13">Thumbnail not available</text>
                                                                        </svg>
                                                                    </div>
                                                                </div>
                                                                <div class="thumbnail-separator" aria-hidden="true"></div>
                                                            </div>
                                                            <!-- Script editor -->
                                                            <div>
                                                                <label :for="`s3-page-${idx}`">Script</label>
                                                                <textarea
                                                                    :id="`s3-page-${idx}`"
                                                                    v-model="aiTexts[idx]"
                                                                    class="form-control page-textarea"
                                                                    style="resize: vertical; width: 100%; overflow:auto;"
                                                                    @input="(e) => onTextareaInput(e, idx)"
                                                                ></textarea>
                                                                <div class="page-separator" aria-hidden="true"></div>
                                                            </div>
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                        <div class="mt-3 d-flex gap-2">
                                            <button class="btn btn-dark-main flex-grow-1 py-2" type="submit" :disabled="videoLoading || aiTexts.length === 0">
                                                <span v-if="videoLoading" class="spinner-border spinner-border-sm me-2"></span>
                                                {{ videoLoading ? 'Generating...' : 'Re-generate and Download' }}
                                            </button>
                                            <button class="btn btn-outline-secondary py-2 px-3" type="button" @click="resetProject">New Project</button>
                                        </div>
                                    </form>
                                    <div v-if="videoError" class="alert alert-danger mt-3 text-center va-result">{{ videoError }}</div>
                                </div>
            </div>
        </div>
    </div>
  </AppShell>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { apiRequest, API_ENDPOINTS, getApiEndpoint } from '../config/api.js'
import AppShell from './AppShell.vue'
import { emitter } from '../config/events.js'

const props = defineProps({
    labMode: { type: Boolean, default: false },
})

const shellRef = ref(null)

const step = ref(1) // 1: 上傳, 2: 編輯 (下載按鈕已整合到 step 2)
const pdfFile = ref(null)
const aiTexts = ref([])
const pdfId = ref(null)
const projectId = ref(null) // project_id returned from Phase 1 upload
const videoUrl = ref('')
const objectUrls = ref([]) // track created object URLs to revoke later
const srtPath = ref(null)
const srtUrl = ref(null)
const embedLogPath = ref(null)
const embedLogUrl = ref(null)
const embedMethod = ref(null)
const debugMode = ref(false)
const error = ref('')
const loading = ref(false)
const videoLoading = ref(false)
const videoError = ref('')
const fileInput = ref(null)
const isAdmin = ref(false)
const contentLanguage = ref('zh') // 內容語言：'zh' 或 'en'
const selectedVoice = ref('zh-TW-YunJheNeural')
const combinedSelection = ref('zh:zh-TW-YunJheNeural') // 單一選項，同步語言與預設語音

// UI state for accordion + thumbnails (step 2)
const expandedPages = ref([]) // boolean per-index
const pageThumbnails = ref([]) // data URLs or object URLs per-index
const voiceSelections = ref([]) // per-page voice choice, defaults initialized when aiTexts set

const togglePage = (idx) => {
    // toggle expanded state
    expandedPages.value[idx] = !expandedPages.value[idx]
    if (expandedPages.value[idx]) {
        // lazy fetch thumbnail when opening
        fetchThumbnail(idx).catch((e)=>console.warn('fetchThumbnail fail', e))
    }
}

const fetchThumbnail = async (idx) => {
    // Try to fetch a thumbnail image from backend. If backend doesn't provide it, this will gracefully do nothing.
    try {
        if (pageThumbnails.value[idx]) return
        const pdf_id = pdfId.value
        if (!pdf_id) {
            // no pdf id available yet
            return
        }
        const token = localStorage.getItem('token')
        // Endpoint convention (backend may not implement this yet): /api/video-abstract/thumbnail?pdf_id=...&page=1
        const endpoint = getApiEndpoint('/api/video-abstract/thumbnail') + `?pdf_id=${encodeURIComponent(pdf_id)}&page=${idx+1}`
        const res = await fetch(endpoint, {
            method: 'GET',
            headers: token ? { 'Authorization': 'Bearer ' + token } : {}
        })
        if (!res.ok) {
            // thumbnail not available or endpoint not implemented
            console.debug('thumbnail not available for page', idx+1, res.status)
            return
        }
        const blob = await res.blob()
        const url = URL.createObjectURL(blob)
        pageThumbnails.value[idx] = url
        // track for later revocation
        objectUrls.value.push(url)
    } catch (e) {
        console.warn('fetchThumbnail error', e)
    }
}

const fetchMe = async () => {
    try {
        const me = await apiRequest(API_ENDPOINTS.ME)
        isAdmin.value = !!me.is_admin
    } catch (error) {
        console.error('獲取用戶資訊失敗:', error)
    }
}

// 將「內容語言＋預設語音」合併為單一選擇，同步後端參數
const syncCombinedSelection = (val) => {
    const [lang, voice] = (val || 'zh:zh-TW-YunJheNeural').split(':')
    contentLanguage.value = lang || 'zh'
    selectedVoice.value = voice || 'zh-TW-YunJheNeural'
}

watch(combinedSelection, (val) => {
    syncCombinedSelection(val)
})

// 初始化同步一次
syncCombinedSelection(combinedSelection.value)

// Robust PDF validation: prefer MIME type, fallback to extension, and enforce max size (20MB)
const MAX_PDF_SIZE = 20 * 1024 * 1024
const isPdfFile = (file) => {
    if (!file) return false
    const mimeOk = file.type === 'application/pdf'
    const extOk = /\.pdf$/i.test(file.name)
    const sizeOk = file.size <= MAX_PDF_SIZE
    return (mimeOk || extOk) && sizeOk
}

const onFileChange = (e) => {
    const file = e.target.files?.[0]
    if (isPdfFile(file)) {
        pdfFile.value = file
    } else {
        pdfFile.value = null
        alert('僅支援 PDF 檔案，且大小不得超過 20MB')
    }
}

const onDrop = (e) => {
    const files = e.dataTransfer.files
    if (files && files.length > 0) {
        const file = files[0]
        if (isPdfFile(file)) {
            pdfFile.value = file
        } else {
            pdfFile.value = null
            alert('僅支援 PDF 檔案，且大小不得超過 20MB')
        }
    }
}

const createObjectUrl = (blob) => {
    // revoke previous URLs to avoid memory leaks
    objectUrls.value.forEach((u) => {
        try { URL.revokeObjectURL(u) } catch (e) {}
    })
    objectUrls.value = []
    const url = URL.createObjectURL(blob)
    objectUrls.value.push(url)
    return url
}

// Auto-resize helpers for textareas: adjust height to fit content
const autosizeTextarea = (el) => {
    if (!el) return
    try {
        el.style.height = 'auto'
        // small padding to avoid scrollbar flicker
        el.style.height = (el.scrollHeight + 2) + 'px'
    } catch (e) {
        // ignore
    }
}

const autosizeAll = () => {
    nextTick(() => {
        const els = document.querySelectorAll('.page-textarea')
        els.forEach((el) => autosizeTextarea(el))
    })
}

const onTextareaInput = (e, idx) => {
    // update model already via v-model; just resize
    autosizeTextarea(e.target)
}

onBeforeUnmount(() => {
    objectUrls.value.forEach((u) => {
        try { URL.revokeObjectURL(u) } catch (e) {}
    })
    objectUrls.value = []
})

const handleUpload = async () => {
    if (!pdfFile.value) return
    loading.value = true
    error.value = ''
    aiTexts.value = []
    const formData = new FormData()
    formData.append('file', pdfFile.value)
    // include content language and selected voice
    formData.append('content_language', contentLanguage.value)
    formData.append('voice', selectedVoice.value)
    try {
        const token = localStorage.getItem('token')
        const url = getApiEndpoint(API_ENDPOINTS.VIDEO_ABSTRACT)
        const res = await fetch(url, {
            method: 'POST',
            headers: {
                'Authorization': 'Bearer ' + token
            },
            body: formData
        })
        const ctype = res.headers.get('content-type') || ''
        // helper: try parse json safely
        const tryParseJson = async (r) => {
            try { return await r.clone().json() } catch (e) { return null }
        }

        if (ctype.includes('application/json')) {
            const data = await tryParseJson(res)
            if (!res.ok) {
                // try to extract message
                const msg = data?.detail || data?.error || data?.message || JSON.stringify(data) || '分析失敗'
                throw new Error(`(${res.status}) ${msg}`)
            }
            // 假設 data.texts 是 AI 產生的每頁文字陣列
                if (Array.isArray(data?.texts)) {
                aiTexts.value = data.texts
                pdfId.value = data.pdf_id || null
                projectId.value = data.project_id ?? null
                    // initialize UI arrays for pages — expand all pages so previews and editors are visible
                    expandedPages.value = Array(aiTexts.value.length).fill(true)
                    pageThumbnails.value = Array(aiTexts.value.length).fill(null)
                    // initialize voice selections with default voice
                    voiceSelections.value = Array(aiTexts.value.length).fill(selectedVoice.value)
                // capture metadata if provided
                srtPath.value = data.srt_path || srtPath.value
                srtUrl.value = data.srt_url || srtUrl.value
                embedLogPath.value = data.embed_log_path || embedLogPath.value
                embedLogUrl.value = data.embed_log_url || embedLogUrl.value
                embedMethod.value = data.embed_method_tried || embedMethod.value
                    step.value = 2 // Stay on step 2
                    // Signal sidebar to refresh via both event paths
                    console.log('[VideoAbstract] Triggering Sidebar Refresh (Phase 1)...')
                    emitter.emit('refresh-projects')
                    window.dispatchEvent(new CustomEvent('refresh-sidebar'))
            } else {
                // maybe the endpoint returned info but not texts
                throw new Error('後端回傳 JSON，但無 texts 欄位')
            }
        } else if (ctype.includes('video/mp4') || ctype.includes('application/octet-stream')) {
            // 若直接回傳影片
            const blob = await res.blob()
            videoUrl.value = createObjectUrl(blob)
                // Keep user on step 2 and provide download button
                // step.value = 3
            // try to read metadata: JSON body or fallback to percent-encoded headers
            const maybeJson = await tryParseJson(res)
            if (maybeJson) {
                srtPath.value = maybeJson.srt_path || srtPath.value
                srtUrl.value = maybeJson.srt_url || srtUrl.value
                embedLogPath.value = maybeJson.embed_log_path || embedLogPath.value
                embedLogUrl.value = maybeJson.embed_log_url || embedLogUrl.value
                embedMethod.value = maybeJson.embed_method_tried || embedMethod.value
            } else {
                const hSrt = res.headers.get('x-srt-path')
                const hEmbedLog = res.headers.get('x-embed-log-path')
                const hEmbedMethod = res.headers.get('x-embed-method')
                if (hSrt) srtPath.value = decodeURIComponent(hSrt)
                if (hEmbedLog) embedLogPath.value = decodeURIComponent(hEmbedLog)
                if (hEmbedMethod) embedMethod.value = decodeURIComponent(hEmbedMethod)
            }
        } else {
            // fallback: try text body for error/debug
            const text = await res.text()
            if (!res.ok) throw new Error(`(${res.status}) ${text.slice(0, 200)}`)
            throw new Error('未知回應格式')
        }
        // usage-status update removed
    } catch (e) {
        error.value = e.message
    }
    loading.value = false
}

const handleGenerateVideo = async () => {
    videoLoading.value = true
    videoError.value = ''
    // helper: trigger a browser download from a Blob
    const triggerDownloadFromBlob = (blob, filename) => {
        try {
            const url = createObjectUrl(blob)
            const a = document.createElement('a')
            a.href = url
            a.download = filename || (pdfFile.value?.name ? pdfFile.value.name.replace(/\.pdf$/i, '.mp4') : 'video.mp4')
            document.body.appendChild(a)
            a.click()
            a.remove()
            // revoke after short delay
            setTimeout(() => {
                try { URL.revokeObjectURL(url) } catch (e) {}
            }, 4000)
        } catch (e) {
            console.warn('download trigger failed', e)
        }
    }

    // helper: fetch a remote video URL (signed or otherwise) and download as blob
    const fetchAndDownload = async (videoUrlStr, filename) => {
        const token = localStorage.getItem('token')
        const r = await fetch(videoUrlStr, { headers: token ? { 'Authorization': 'Bearer ' + token } : {} })
        if (!r.ok) throw new Error(`下載失敗: ${r.status}`)
        const b = await r.blob()
        triggerDownloadFromBlob(b, filename)
    }

    try {
        const token = localStorage.getItem('token')
        const url = getApiEndpoint(API_ENDPOINTS.VIDEO_ABSTRACT)
        const res = await fetch(url, {
            method: 'POST',
            headers: {
                'Authorization': 'Bearer ' + token,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ 
                texts: aiTexts.value, 
                pdf_id: pdfId.value,
                project_id: projectId.value,
                content_language: contentLanguage.value,
                language: contentLanguage.value,
                voice: selectedVoice.value 
            })
        })
        const ctype = res.headers.get('content-type') || ''
        const tryParseJson = async (r) => {
            try { return await r.clone().json() } catch (e) { return null }
        }

        if (!res.ok) {
            // try parse json or text for better error
            const data = await tryParseJson(res)
            const txt = data?.detail || data?.error || data?.message || await res.text().catch(()=>'')

            // If server says PDF not found (temp file cleaned) and we still have the original PDF in the browser,
            // try a FormData fallback that re-uploads the PDF + texts so generation can proceed and then download.
            const notFoundMsg = String(txt || '').toLowerCase()
            if (res.status === 400 && (notFoundMsg.includes('找不到') || notFoundMsg.includes('pdf not found')) && pdfFile.value) {
                try {
                    const fd = new FormData()
                    fd.append('file', pdfFile.value)
                    fd.append('texts', JSON.stringify(aiTexts.value))
                    fd.append('voice', selectedVoice.value)

                    const res2 = await fetch(url, {
                        method: 'POST',
                        headers: token ? { 'Authorization': 'Bearer ' + token } : {},
                        body: fd
                    })

                    const ctype2 = res2.headers.get('content-type') || ''
                    const maybeJson2 = await tryParseJson(res2)

                    if (!res2.ok) {
                        const txt2 = maybeJson2?.detail || maybeJson2?.error || maybeJson2?.message || await res2.text().catch(()=>'')
                        throw new Error(`(${res2.status}) ${txt2 || '影片產生失敗 (re-upload)'}`)
                    }

                    // handle binary
                    if (ctype2.includes('video/mp4') || ctype2.includes('application/octet-stream')) {
                        const blob = await res2.blob()
                        triggerDownloadFromBlob(blob)
                        if (maybeJson2) {
                            srtPath.value = maybeJson2.srt_path || srtPath.value
                            srtUrl.value = maybeJson2.srt_url || srtUrl.value
                            embedLogPath.value = maybeJson2.embed_log_path || embedLogPath.value
                            embedLogUrl.value = maybeJson2.embed_log_url || embedLogUrl.value
                            embedMethod.value = maybeJson2.embed_method_tried || embedMethod.value
                        }
                        videoLoading.value = false
                        return
                    }

                    // handle json link
                    if (ctype2.includes('application/json')) {
                        const data2 = maybeJson2
                        // If backend returned an explicit error message, surface it.
                        const serverErr = data2?.detail || data2?.error || data2?.message
                        if (serverErr) {
                            throw new Error(`(${res2.status}) ${serverErr}`)
                        }
                        if (data2?.video_url) {
                            await fetchAndDownload(data2.video_url, pdfFile.value?.name ? pdfFile.value.name.replace(/\.pdf$/i, '.mp4') : 'video.mp4')
                        } else if (data2?.pdf_id) {
                            // The re-upload returned a pdf_id but did not generate video synchronously.
                            // Call the generate endpoint with the new pdf_id (no further re-upload fallback) and download the result.
                            const res3 = await fetch(url, {
                                method: 'POST',
                                headers: {
                                    'Authorization': token ? 'Bearer ' + token : undefined,
                                    'Content-Type': 'application/json'
                                },
                                body: JSON.stringify({ texts: aiTexts.value, pdf_id: data2.pdf_id, voice: selectedVoice.value })
                            })
                            const ctype3 = res3.headers.get('content-type') || ''
                            if (!res3.ok) {
                                const maybe3 = await tryParseJson(res3)
                                const errMsg3 = maybe3?.detail || maybe3?.error || maybe3?.message || await res3.text().catch(()=>'')
                                throw new Error(`(${res3.status}) ${errMsg3 || '影片產生失敗 (after upload)'} `)
                            }
                            if (ctype3.includes('video/mp4') || ctype3.includes('application/octet-stream')) {
                                const blob3 = await res3.blob()
                                triggerDownloadFromBlob(blob3)
                                const maybeJson3 = await tryParseJson(res3)
                                if (maybeJson3) {
                                    srtPath.value = maybeJson3.srt_path || srtPath.value
                                    srtUrl.value = maybeJson3.srt_url || srtUrl.value
                                    embedLogPath.value = maybeJson3.embed_log_path || embedLogPath.value
                                    embedLogUrl.value = maybeJson3.embed_log_url || embedLogUrl.value
                                    embedMethod.value = maybeJson3.embed_method_tried || embedMethod.value
                                }
                                videoLoading.value = false
                                return
                            }
                            if (ctype3.includes('application/json')) {
                                const data3 = await tryParseJson(res3)
                                const serverErr3 = data3?.detail || data3?.error || data3?.message
                                if (serverErr3) throw new Error(`(${res3.status}) ${serverErr3}`)
                                if (data3?.video_url) {
                                    await fetchAndDownload(data3.video_url, pdfFile.value?.name ? pdfFile.value.name.replace(/\.pdf$/i, '.mp4') : 'video.mp4')
                                } else {
                                    throw new Error('後端回傳 JSON，但找不到 video 或 二進位資料 (after upload)')
                                }
                                srtPath.value = data3.srt_path || srtPath.value
                                srtUrl.value = data3.srt_url || srtUrl.value
                                embedLogPath.value = data3.embed_log_path || embedLogPath.value
                                embedLogUrl.value = data3.embed_log_url || embedLogUrl.value
                                embedMethod.value = data3.embed_method_tried || embedMethod.value
                                videoLoading.value = false
                                return
                            }
                            throw new Error('未知回應格式 (after upload)')
                        } else {
                            throw new Error('後端回傳 JSON，但找不到 video 或 二進位資料 (re-upload)')
                        }
                        srtPath.value = data2.srt_path || srtPath.value
                        srtUrl.value = data2.srt_url || srtUrl.value
                        embedLogPath.value = data2.embed_log_path || embedLogPath.value
                        embedLogUrl.value = data2.embed_log_url || embedLogUrl.value
                        embedMethod.value = data2.embed_method_tried || embedMethod.value
                        videoLoading.value = false
                        return
                    }

                    const txt2 = await res2.text()
                    throw new Error(`未知回應格式 (re-upload): ${txt2.slice(0,200)}`)
                } catch (reErr) {
                    throw new Error(reErr.message || `(${res.status}) ${txt || '影片產生失敗'}`)
                }
            }

            throw new Error(`(${res.status}) ${txt || '影片產生失敗'}`)
        }

        // success path for primary request
        if (ctype.includes('video/mp4') || ctype.includes('application/octet-stream')) {
            const blob = await res.blob()
            triggerDownloadFromBlob(blob)
            // Transition to step 3 (completed-project preview) and update sidebar
            step.value = 3
            videoLoading.value = false
            console.log('[VideoAbstract] Triggering Sidebar Refresh (Phase 2)...')
            emitter.emit('refresh-projects')
            window.dispatchEvent(new CustomEvent('refresh-sidebar'))
            const maybeJson = await tryParseJson(res)
            if (maybeJson) {
                srtPath.value = maybeJson.srt_path || srtPath.value
                srtUrl.value = maybeJson.srt_url || srtUrl.value
                embedLogPath.value = maybeJson.embed_log_path || embedLogPath.value
                embedLogUrl.value = maybeJson.embed_log_url || embedLogUrl.value
                embedMethod.value = maybeJson.embed_method_tried || embedMethod.value
            } else {
                const hSrt = res.headers.get('x-srt-path')
                const hEmbedLog = res.headers.get('x-embed-log-path')
                const hEmbedMethod = res.headers.get('x-embed-method')
                if (hSrt) srtPath.value = decodeURIComponent(hSrt)
                if (hEmbedLog) embedLogPath.value = decodeURIComponent(hEmbedLog)
                if (hEmbedMethod) embedMethod.value = decodeURIComponent(hEmbedMethod)
            }
        } else if (ctype.includes('application/json')) {
            const data = await tryParseJson(res)
            if (data?.mock_completed) {
                step.value = 3
                console.log('[VideoAbstract] Mock mode generation completed')
                emitter.emit('refresh-projects')
                window.dispatchEvent(new CustomEvent('refresh-sidebar'))
                return
            }
            const serverErr = data?.detail || data?.error || data?.message
            if (serverErr) {
                throw new Error(`(${res.status}) ${serverErr}`)
            }
            if (data?.video_url) {
                await fetchAndDownload(data.video_url, pdfFile.value?.name ? pdfFile.value.name.replace(/\.pdf$/i, '.mp4') : 'video.mp4')
            } else {
                throw new Error('後端回傳 JSON，但找不到 video 或 二進位資料')
            }
            srtPath.value = data.srt_path || srtPath.value
            srtUrl.value = data.srt_url || srtUrl.value
            embedLogPath.value = data.embed_log_path || embedLogPath.value
            embedLogUrl.value = data.embed_log_url || embedLogUrl.value
            embedMethod.value = data.embed_method_tried || embedMethod.value
        } else {
            const txt = await res.text()
            throw new Error(`未知回應格式: ${txt.slice(0,200)}`)
        }
    } catch (e) {
        videoError.value = e.message
    }
    videoLoading.value = false
}

// ---------- Sidebar event handlers ----------
const resetProject = () => {
    step.value = 1
    pdfFile.value = null
    aiTexts.value = []
    pdfId.value = null
    projectId.value = null
    error.value = ''
    videoError.value = ''
    videoUrl.value = ''
    expandedPages.value = []
    pageThumbnails.value = []
    // revoke stale object URLs
    objectUrls.value.forEach((u) => { try { URL.revokeObjectURL(u) } catch (e) {} })
    objectUrls.value = []
    if (fileInput.value) fileInput.value.value = ''
}

const activeProjectName = ref('')


const onSelectProject = (project) => {
    // Handle processing projects: notify and do nothing else
    if (project.status === 'processing') {
        alert('This project is still being generated. Please check back later.')
        return
    }

    // Completed project with a script: load into Step 3
    if (project.status === 'completed' && project.script_json) {
        try {
            const parsed = JSON.parse(project.script_json)
            if (Array.isArray(parsed) && parsed.length > 0) {
                activeProjectName.value = project.project_name || 'Project'
                aiTexts.value = parsed
                // Use the pdf_id stored in the database (set at upload time).
                // The persistent user_thumbnails/<pdf_id>/ folder will be used
                // by the thumbnail endpoint regardless of whether the temp PDF exists.
                pdfId.value = project.pdf_id || null
                projectId.value = project.id
                expandedPages.value = Array(parsed.length).fill(true)
                pageThumbnails.value = Array(parsed.length).fill(null)
                voiceSelections.value = Array(parsed.length).fill(selectedVoice.value)
                error.value = ''
                videoError.value = ''
                step.value = 3
                return
            }
        } catch (e) {
            // JSON parse error: fall through to reset
        }
    }

    // Completed with no usable script, or deleted: go to upload step
    resetProject()
}

const onProjectDeleted = (deletedId) => {
    // If the user is currently viewing the deleted project, go back to upload step
    if (projectId.value === deletedId) {
        resetProject()
    }
    // Signal sidebar to refresh via both event paths
    console.log('[VideoAbstract] Triggering Sidebar Refresh (delete)...')
    emitter.emit('refresh-projects')
    window.dispatchEvent(new CustomEvent('refresh-sidebar'))
    // Fallback: key-remount via AppShell after 400ms (covers edge cases)
    setTimeout(() => { shellRef.value?.refresh() }, 400)
}
// ---------- End sidebar handlers ----------

onMounted(async () => {
    if (!props.labMode) {
        await fetchMe()
    }
})

// Fetch thumbnails whenever step 2 or step 3 is entered with pages
watch([step, aiTexts], async ([newStep, newAiTexts]) => {
    if ((newStep === 2 || newStep === 3) && Array.isArray(newAiTexts) && newAiTexts.length > 0) {
        // ensure arrays are initialized and expand all pages so editors and thumbnails are visible
        expandedPages.value = Array(newAiTexts.length).fill(true)
        pageThumbnails.value = Array(newAiTexts.length).fill(null)
        voiceSelections.value = Array(newAiTexts.length).fill(selectedVoice.value)
        for (let i = 0; i < newAiTexts.length; i++) {
            // kick off fetch but don't await sequentially to avoid blocking UI
            fetchThumbnail(i).catch((e) => console.debug('thumbnail fetch failed', i, e))
        }
            // initialize voice selections from the global selection
            voiceSelections.value = Array(newAiTexts.length).fill(selectedVoice.value)
            // autosize textareas after they are rendered
            autosizeAll()
    }
})

// keep voiceSelections in sync when user changes global selection
watch(selectedVoice, (v) => {
    if (Array.isArray(aiTexts.value) && aiTexts.value.length > 0) {
        voiceSelections.value = Array(aiTexts.value.length).fill(v)
    }
})
</script>

<style scoped>
.va-bg {
    background: #181c24;
    min-height: 100%;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    box-sizing: border-box;
    overflow: auto;
    padding: 40px 0;
}

.va-card {
    border-radius: 1.2rem;
    background: #232b3a;
    color: #fff;
    /* keep card from growing beyond viewport; allow internal scroll */
    max-height: calc(100vh - 120px);
    overflow: auto;
    padding: 2.5rem; /* increase inner padding */
}

.va-title {
    color: #3a8dde;
    font-weight: bold;
    letter-spacing: 1px;
}

.va-title {
    font-size: 2.1rem;
}

.va-desc {
    color: #b0bed9;
    font-size: 1.1rem;
}

.va-desc {
    font-size: 1.25rem;
}

/* Per-page title sizing inside the accordion header */
.accordion-button .page-number {
    display: block;
    text-align: center;
    font-size: 1.9rem;
    font-weight: 800;
    color: #ffffff;
    line-height: 1;
}

@media (min-width: 992px) {
    .accordion-button .page-number {
        font-size: 2.2rem;
    }
}

.va-upload-area {
    background: #232b3a;
    border: 2px dashed #3a8dde;
    border-radius: 1rem;
    padding: 40px 20px;
    cursor: pointer;
    transition: border 0.2s;
    margin-bottom: 0.5rem;
}

/* Dark styling for voice select */
.voice-select {
    background: #1f2731; /* darker than card background */
    color: #ffffff;
    border: 1px solid rgba(255,255,255,0.06);
}
.voice-select option {
    background: #1f2731;
    color: #ffffff;
}

.va-upload-area:hover {
    border: 2px solid #3a8dde;
    background: #202634;
}

.va-upload-content {
    color: #b0bed9;
}

.va-upload-icon {
    font-size: 3rem;
    display: block;
    margin-bottom: 8px;
}

.va-upload-text {
    font-size: 1.1rem;
    font-weight: bold;
}

.va-upload-tip {
    font-size: 0.95rem;
    color: #6c7a92;
    margin-top: 4px;
}

.va-result {
    font-size: 1.1rem;
}

.btn-dark-main {
    background: linear-gradient(90deg, #232b3a 0%, #3a8dde 100%);
    color: #fff;
    border: none;
    font-weight: bold;
    border-radius: 0.9rem;
    box-shadow: 0 6px 20px #232b3a66;
    transition: box-shadow 0.2s, background 0.2s;
    font-size: 1.05rem;
    padding: 0.9rem 1.2rem;
}

.btn-dark-main:hover {
    background: linear-gradient(90deg, #3a8dde 0%, #232b3a 100%);
    color: #fff;
    box-shadow: 0 4px 16px #3a8dde44;
}

.btn-dark-main:disabled {
    background: #6c7a92;
    cursor: not-allowed;
    box-shadow: none;
}

/* Larger thumbnail and textarea sizes for better preview/edit experience */
.thumbnail-box {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 400px; /* default for small/medium screens */
    padding: 0; /* remove internal padding so image touches box edges */
    line-height: 0; /* remove inline gap under images/SVG */
}
.page-textarea {
    /* allow autosize script to control height; limit growth with max-height and enable scrolling once reached */
    font-size: 1.05rem;
    line-height: 1.5;
    max-height: 60vh; /* textarea grows with content up to this limit */
    overflow: auto; /* show scrollbar when content exceeds max-height */
}

.page-separator {
    height: 2rem; /* 32px spacing */
}

@media (min-width: 992px) {
    .page-separator {
        height: 5rem; /* 48px on larger screens */
    }
}

.thumbnail-separator {
    height: 1rem; /* smaller gap under thumbnail */
}

@media (min-width: 992px) {
    .thumbnail-separator {
        height: 1.5rem;
    }
}

.thumbnail-box img,
.thumbnail-box svg {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
}

@media (min-width: 992px) {
    /* On larger screens, make thumbnails tall; textarea uses max-height instead of min-height */
    .thumbnail-box {
        min-height: 60vh; /* occupy most of the viewport height */
    }
}

@media (max-width: 576px) {
    .thumbnail-box {
        min-height: 30vh;
    }
    /* on small screens reduce textarea max height so it doesn't dominate the viewport */
    .page-textarea {
        max-height: 40vh;
    }
}

@media (max-width: 600px) {
    .va-card {
        padding: 1.2rem;
        width: 98%;
    }

    .va-upload-area {
        padding: 20px 4px;
    }
}

/* ---- Step 3: completed project header ---- */
.va-step3-header {
    margin-bottom: 1.5rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
}

.va-step3-title {
    font-size: 1.4rem;
    font-weight: 700;
    color: #fff;
    margin-bottom: 0.35rem;
    word-break: break-word;
}

.va-step3-subtitle {
    font-size: 0.9rem;
    color: #9ca3af;
}

.btn-outline-secondary {
    background: transparent;
    border: 1.5px solid rgba(255,255,255,0.2);
    color: #9ca3af;
    border-radius: 0.9rem;
    font-size: 0.9rem;
    transition: border-color 0.18s, color 0.18s;
}

.btn-outline-secondary:hover {
    border-color: #9ca3af;
    color: #fff;
}
</style>
