# SlideAI 專案更新日誌

本文件記錄 SlideAI 的功能演進、目前定案架構、已知限制與後續規劃。

---

## 目前定案

### 字幕對齊主線
- **正式放棄 WhisperX 路線**
- 字幕對齊統一採用 **Qwen3-ASR + Qwen3-ForcedAligner**

### 對齊模式
- **有參考文稿**：`alignment_mode=scripted`（只做強制對齊）
- **沒有參考文稿**：`alignment_mode=auto_asr`（先 ASR 再對齊）
- `alignment_mode=auto`：有文字走 scripted，無文字走 auto_asr

### 簡繁轉換策略
- 送入 Qwen 前：`繁體 → 簡體`（OpenCC t2s）
- 回傳前端前：`簡體 → 繁體`（OpenCC s2t）
- 不改動使用者原始輸入欄位內容

### TTS 主線
- **VoxTTS（VoxCPM）** 取代 Qwen3 TTS 為主線
- 失敗時 fallback Edge-TTS（除非 `LOCAL_ONLY=true`）
- **Qwen3-TTS 固定四句切分預設**：送入 Qwen3 前，先以標點（`，。！？；：、,.!?;:`）切句，再每 4 句合成一段送入 TTS，最後合併音檔。目的：避免單段文本過長導致音色/韻律漂移。

### TTS Provider 介面（provider-neutral 命名）
| 舊欄位 | 新欄位 |
|---|---|
| `qwen_ref_text` | `reference_transcript` |
| `qwen_ref_audio_path` | `reference_audio` |
| `qwen_x_vector_only_mode` | `provider_options.clone_embedding_only` |

---

## 功能模塊現況

### 歷史專案管理
- 查看、存取過去所有專案；自動儲存 PDF 路徑、影片、腳本
- 首頁縮圖預覽（持久化快取）；支援刪除並清理關聯檔案

### 側邊欄
- ChatGPT 風格動態側邊欄，顯示所有簡報專案
- 多重刷新路徑：emitter、window.dispatchEvent、sidebarKey++

### 影片合成（字幕影片下載）
- 優先路徑：**Canvas Skia v3**（`backend/canvas_renderer/render-optimized-v2.mjs`）
- Fallback 順序：`canvas-skia-v3` → `remotion-v1` → `ass-discrete-v2`
- Canvas 字幕渲染特性：
  - **所有 layout 常數從 `shared/subtitle-layout/index.js → LAYOUT` 匯入**（前後端共用同一份）
  - 字體：`LAYOUT.FONT_WEIGHT (400) + LAYOUT.FONT_FAMILY`（weight 400 精確對應 Regular.ttc，避免 skia-canvas 與瀏覽器字重回退差異）
  - 文字對齊：`textBaseline='top'`，每行 y = lineTop + `(lineHeight - fz) / 2`（比 'middle' 在 CJK 更確定）
  - 底部 margin：`height × LAYOUT.BOTTOM_MARGIN_RATIO (0.03)` — 相對全畫面高度，與前端 `ch × 0.03` 完全一致
  - 事件切段渲染，非逐幀，降低 I/O
  - 背景圖預解碼為 rawvideo（yuv420p），減少重複解碼耗時


---

## 主工作區（`/` 與 `/video-abstract-lab`）

> 兩個網址等效，皆為可信任內網、免登入的正式工作區。

- **Mock 模式**（`VIDEO_ABSTRACT_MOCK_MODE`）：不消耗 LLM/TTS 額度
- **LOCAL_ONLY 模式**（`VIDEO_ABSTRACT_LOCAL_ONLY`）：禁止外部 API 呼叫

---

## 環境設定

### 後端（`backend/.env`）
```env
# 基本
api_key=YOUR_GOOGLE_API_KEY

# 測試模式
VIDEO_ABSTRACT_MOCK_MODE=true
VIDEO_ABSTRACT_LOCAL_ONLY=true

# VoxTTS / VoxCPM
VOXTTS_ENABLED=true
VOXTTS_MODEL_PATH=/path/to/VoxCPM2
VOXTTS_RUNTIME_PYTHON=/path/to/vox-venv/bin/python3
VOXTTS_OPTIMIZE=auto
VOXTTS_REFERENCE_AUDIO_PATH=/path/to/ref.wav
VOXTTS_ENABLE_DENOISER=false

# Subtitle alignment
QWEN3_ASR_RUNTIME_PYTHON=/path/to/qwen-venv/bin/python3
QWEN3_ASR_MODEL_PATH=/path/to/Qwen3-ASR-1.7B
QWEN3_ALIGNER_MODEL_PATH=/path/to/Qwen3-ForcedAligner-0.6B
QWEN3_ASR_DEVICE=cuda:0
QWEN3_ASR_DTYPE=bfloat16
QWEN3_ASR_ATTN_IMPL=eager
```

### 前端（`frontend/.env.development.local`，不 commit）
```env
VITE_API_BASE_URL=http://<backend-host>:8002
```

正式 Cloudflare / Worker 入口不設定 `VITE_API_BASE_URL`，前端預設使用同網域相對路徑 `/api`。

### 啟動方式
```bash
# 後端（從專案根目錄執行）
cd /path/to/SlideAI
VIDEO_ABSTRACT_MOCK_MODE=1 VIDEO_ABSTRACT_LOCAL_ONLY=false \
  backend/.venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8002

# 前端
cd frontend
VITE_API_BASE_URL=http://<backend-host>:8002 npm run build
node spa-server.cjs
```

> **注意**：後端必須從專案根目錄啟動，否則 `ModuleNotFoundError: No module named 'backend'`

---

## 使用者需自行補充的靜態資源

**路徑**：`backend/app/static/ref_voices/`

| 選單 Key | 檔案 |
|---|---|
| `female_zh` | `female_zh.wav` |
| `male_zh` | `male_zh.wav` |
| `female_en` | `female_en.wav` |

- `manifest.json`：定義預設音軌的「參考文字」，供前端自動帶入並鎖定唯讀

---

## 字幕對齊 Service

入口：`backend/app/services/subtitle_alignment.py` → `align_subtitles_from_audio_and_text`

```python
result = align_subtitles_from_audio_and_text(
    text="可選，有則走強制對齊，空則走 ASR",
    audio_bytes=b"...",
    audio_filename="input.wav",
    language="auto",
    alignment_mode="auto",
    split_min_chars=6,
    split_max_chars=24,
)
# result.segments → [{start, end, text, words:[{start,end,text}]}]
# result.srt      → SRT 字串
# result.backend  → "qwen3-forced-aligner" 等
```

- fail-fast：時間覆蓋率 < 15% 或文本相似度 < 0.35 時拋 HTTP 422

---

## Shared Subtitle Layout 模組

**路徑**：`shared/subtitle-layout/index.js`

前後端共用：

| 函式 / 常數 | 用途 |
|---|---|
| `normalizeSubtitleSegments` | 正規化 segments |
| `buildSubtitleStateTimeline` | 事件切段時間軸（後端渲染用） |
| `findActiveSegment` | 當前時間軸對應 segment |
| `buildSubtitleOverlayPieces` | 產生含高亮標記的 pieces 陣列 |
| `buildSmoothedWordTimeline` | 字詞時間軸平滑 |
| `DEFAULTS` | 共用常數（lead/linger/tolerance 等） |

---

## 已知限制

- **斷句切分**：中英混合專有詞仍可能在不理想位置斷句
- **字幕大小自適應**：尚未根據文字長度 / 行數自動調整字級
- **VoxTTS 常駐 worker**：目前每次推理都重新載模，高延遲

---

## 歷史備註（2026-05-04）

- 早期曾有 Skia/Canvas 與 Playwright/CSS 的多條渲染實驗線。
- 目前主線已改為 ASS 最終渲染 + Web 快速預覽，不再以變體頁作為日常流程。
- CSS 字幕做法在視覺精緻度上仍有潛力，後續若有時間可作為獨立分支再評估導回。

---

## 工作流與產物管理提案（2026-05-16）

### 目標
- 同時支援：
  - WebUI 手動操作（逐頁微調/重生成）
  - API 批次呼叫（一次跑多份 PDF）
- 所有中間產物可追蹤、可回滾、可重用，避免每次全量重算。

### Job 目錄與命名
- 每次任務建立一個 `job_id`（建議：`<pdf_name>_<timestamp>_<short_uuid>`）。
- 建議落地路徑：`/data/jobs/<job_id>/`
- 目錄結構：
  - `input/`：原始 PDF、參考音訊（若有）
  - `pages/<page_no>/`：每頁中間產物與版本
  - `output/`：每頁片段影片、最終合併影片
  - `meta/`：設定快照、狀態、manifest

### 每頁版本化產物（建議）
- `script_v1.txt`（該頁講稿）
- `tts_v1.wav`
- `align_v1.json`（word/segment timeline）
- `subtitle_v1.ass`
- `segment_v1.mp4`
- 重生成時新增 `v2/v3...`，不覆蓋舊檔。

### Job 級 metadata（建議）
- `meta/job.json`：建立時間、來源 PDF、總頁數、狀態。
- `meta/tts_settings.json`：TTS 參數快照。
- `meta/subtitle_settings.json`：字幕樣式快照。
- `meta/llm_settings.json`：模型與 endpoint（不存明文 API key）。
- `meta/manifest.json`：每頁目前採用版本（例如 page 3 使用 `segment_v2.mp4`）。

### API 流程（建議）
- `POST /api/jobs`：上傳 PDF 建立任務，回傳 `job_id`。
- `POST /api/jobs/{job_id}/run`：執行整份任務（可批次）。
- `GET /api/jobs/{job_id}`：查進度、狀態、錯誤。
- `POST /api/jobs/{job_id}/pages/{n}/regenerate`：只重刷指定頁，產新版本。
- `POST /api/jobs/{job_id}/pages/{n}/select-version/{v}`：切換該頁採用版本。
- `POST /api/jobs/{job_id}/merge`：依 `manifest` 合併輸出。

### WebUI 與 API 協作方式（建議）
- API 先批量生成多份影片。
- WebUI 開啟指定 `job_id` 進行人工審查。
- 對不滿意頁面執行局部重生成（保留舊版本可回退）。
- 最終按目前 `manifest` 合併匯出。
