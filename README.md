# SlideAI

將 PDF 投影片、逐頁講稿與參考聲音，轉為可下載的語音簡報影片。本專案以
**Linux 本地部署、可信任內網使用與可人工修正**為主，不把模型權重、簡報內容
或 API key 提交到 Git。

## 快速開始

建議環境：Ubuntu 24.04、NVIDIA GPU、Docker Compose v2、NVIDIA Container
Toolkit；完整語音工作流建議至少 16GB VRAM。

```bash
git clone https://github.com/g114056175/test_SlideAI.git
cd test_SlideAI
chmod +x slideai.sh
./slideai.sh
```

第一次請選 `6 建制`，依精靈選擇 Docker、模型來源／本機路徑及選用的 LLM。
完成後選 `1 啟動`；終端會顯示本次實際使用的 WebUI 與 API port。

```text
1) 啟動    2) 關閉    3) 重新啟動
4) 狀態    5) 設定    6) 建制
0) 離開
```

## 使用流程

1. 上傳 PDF，手動撰寫講稿或使用 LLM 產生逐頁講稿。
2. 選擇內建／自訂參考音色、語音速度及字幕模式。
3. 先渲染單頁試聽；可直接修改四句 chunk 的文字後局部重生。
4. 渲染全部，在 `ALL` 預覽合成影片，下載 MP4、SRT 或完整壓縮檔。

字幕模式：`無字幕` 最快且不做對齊；`輸出 SRT` 產生時間軸但不燒字；`燒錄字幕`
產生內嵌字幕影片並保留 SRT。多人同時使用時，渲染工作會依 FIFO 排隊並顯示階段
與頁面進度。

## 預設模型

| 功能 | 預設模型 |
|---|---|
| TTS／語音克隆 | [VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) |
| ASR | [Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) |
| 強制對齊 | [Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) |
| 選用備援 TTS | [Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) |

預設使用官方 VoxCPM2，優先相容性；建制精靈可選 Nano-vLLM 加速，會增加 CUDA／
FlashAttention 相依與顯存保留。公開 Hugging Face 模型可匿名下載，Token 僅在使用
私人或 gated 模型時需要。

## Agent API

不開 WebUI 也可提交 PDF、參考音檔與 JSON 工作單。範例在
[docs/agent-job.example.json](docs/agent-job.example.json)，完整格式、輪詢與輸出說明見
[deploy/README.md](deploy/README.md)。

<details>
<summary><strong>詳細說明（部署、架構與開發紀錄）</strong></summary>

<details>
<summary><strong>系統模組與處理流程</strong></summary>

```text
PDF + 講稿 + 聲音設定
        ↓
TTS（VoxCPM2 / Nano-vLLM / 可替換 provider）
        ↓
Qwen3 ForcedAligner → SRT / ASS
        ↓
FFmpeg 單頁影片 → 插入式轉場 → 合併 MP4
```

- TTS 以標點切句並預設四句為一個 chunk，兼顧長文穩定性與局部重生能力。
- 局部重生會替換單一 chunk，重新合併整頁音訊，再以整頁新講稿重做強制對齊、字幕與
  該頁影片；因此修改後的音長不會讓 SRT 時間軸錯位。
- 轉場採「插入式」做法：保留既有單頁影片，只渲染 `n-1` 段短轉場後 concat，避免為
  全片重新編碼。
- TTS、ASR、對齊皆有 provider／command adapter 邊界；更換本地模型或商用 API 時，
  不必重寫前端。介面格式見 [docs/SPEECH_ADAPTERS.md](docs/SPEECH_ADAPTERS.md)。

</details>

<details>
<summary><strong>前端、字幕與資料設計</strong></summary>

- 前端為 Vue；音訊預覽不是直接套用 Gradio 元件，而是以 WaveSurfer.js 搭配自訂控制、
  下載與波形顯示，讓語音設定與局部重生能融入同一個工作區。
- 字幕曾評估 CSS／Canvas／Remotion 等路徑。正式主流程採 **ASS + FFmpeg**：外觀參數
  可控、預覽／輸出一致，且比逐幀 Canvas 或瀏覽器擷取更適合長影片批次輸出。
- 專案資料以明文 artifact manifest 儲存於 `data/video_runs/`：每個 run 保留 PDF、講稿、
  音訊、chunks、對齊檔、影片與 variants，便於檢查、續跑與回復。此本地單機工作流暫不
  以 SQL 作為核心狀態來源；`data/database/` 僅保留舊相容／應用資料用途。

</details>

<details>
<summary><strong>本地部署與可選公開展示</strong></summary>

- 程式主體透過 Docker Compose 管理前端與後端；模型以主機路徑唯讀掛載，避免把大型權重
  打進 image。模型位置、LLM 與 provider 設定位於未進 Git 的環境檔。
- 專案可在可信任內網以免登入模式使用。若公開到 Internet，應另行啟用 HTTPS、身分驗證、
  來源限制、速率限制與上傳大小限制。
- 開發展示曾掛載於 `https://awinlab-gate.g114056175.me/`。此網址可能因免費網域期限或
  關閉而失效，並不是必要部署條件。展示鏈路可概念化為：

  ```text
  使用者 → 網域 → Cloudflare Proxy / Zero Trust 驗證
          → Worker 密碼閘門 → SlideAI 前後端
  ```

  Cloudflare Access／真人驗證與 Worker 閘門可減少自動掃描與未授權流量；但不取代應用層
  的權限、更新與日誌管理。

</details>

<details>
<summary><strong>效能、測試與後續方向</strong></summary>

- 5090 32GB 的 Nano-vLLM VoxCPM2 長文本實測約 **0.11–0.13 RTF**；實際數字會受模型、
  參考音、chunk 數量與 GPU 驅動影響。
- 可執行 `scripts/portability_check.sh` 與 `scripts/smoke_test.sh` 檢查安裝與前後端。
  `--full` 會實際載入模型並產生影片。
- 後續可擴充：HTML 動態簡報輸入、Agent API 批次工作單、YouTube 章節檔匯出，以及在高
  可信度 OCR／版面判讀下加入局部聚焦效果。

</details>

</details>
