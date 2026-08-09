# SlideAI

SlideAI 是將 PDF 投影片、逐頁講稿與參考聲音轉成語音簡報影片的本地 Web
應用。它支援語音克隆、SRT、字幕燒錄、逐頁重生、版本預覽與合併輸出，亦
提供不需要開啟 WebUI 的 Agent API。

## 快速開始

完整語音工作流目前支援 Linux x86-64、NVIDIA GPU、Docker Compose v2 與
NVIDIA Container Toolkit；主要測試平台為 Ubuntu 24.04，建議至少 16GB VRAM。

```bash
git clone https://github.com/g114056175/test_SlideAI.git
cd test_SlideAI
chmod +x slideai.sh
./slideai.sh
```

第一次使用請選擇 `6 建制`，依精靈準備 Docker、模型路徑及 LLM。完成後
選擇 `1 啟動`，終端會顯示 WebUI 與 API 網址。

```text
1) 啟動    2) 關閉    3) 重新啟動
4) 狀態    5) 設定    6) 建制
0) 離開
```

模型與 API key 不會提交到 Git。設定分別保存在 `deploy/models.env` 與權限
為 `0600` 的 `backend/.env`。

## TTS 選擇

建制精靈會詢問 VoxCPM2 執行方式：

| 模式 | 適合情況 | 取捨 |
|---|---|---|
| 官方 VoxCPM2（預設） | 一般設備、優先相容與穩定 | 環境較單純、顯存配置較自然，但速度較慢 |
| Nano-vLLM（選用） | 已確認 CUDA／GPU 相容且重視速度 | 5090 長文本約 0.11–0.13 RTF；會增加 Torch、FlashAttention 與 CUDA 環境，映像更大並會預留顯存 |

一般部署不會安裝 Nano-vLLM。只有使用者在精靈中明確確認後，Docker build
才會加入該加速環境。之後可由 `5 設定` 切換；切換 Docker 模式後需重新建置。

Qwen3 TTS 也是選用備援。它與 Qwen3 ASR 鎖定不同的 Transformers 版本，
因此選用時會建立獨立環境；預設 VoxCPM2 工作流不會下載這套環境與模型。

官方 VoxCPM2 的參考音訊降噪為選用功能。SlideAI 預設不載入額外的
ZipEnhancer，以避免第一次合成時臨時連線 ModelScope；只有參考音檔本身
帶有明顯噪音時，才需在 `deploy/models.env` 開啟
`VOXTTS_ENABLE_DENOISER` 與 `VOXTTS_DENOISE_REFERENCE`。

## WebUI 使用

1. 上傳 PDF，選擇 AI 生成講稿或自行填寫逐頁講稿。
2. 選擇參考聲音、語速，以及無字幕、輸出 SRT 或燒錄字幕。
3. 先渲染單頁試聽，確認後再「渲染全部」。
4. 在 `ALL` 預覽並下載 MP4、SRT 或完整壓縮檔。

多人同時渲染時會依 FIFO 排隊，畫面會顯示等待數量及 TTS、對齊、渲染進度。

## Agent API

Agent 可提交 PDF、參考音檔與 JSON，後端會自動建立專案、排隊、渲染所有頁
並合併影片。JSON 必須提供與 PDF 頁數相同且不可留空的 `scripts`：

可直接複製 [docs/agent-job.example.json](docs/agent-job.example.json) 修改：

```json
{
  "scripts": [
    "第一頁的完整講稿。",
    "第二頁的完整講稿。"
  ],
  "reference_text": "參考聲音檔案中實際念出的逐字稿。",
  "label": "agent-demo",
  "subtitle_mode": "burn",
  "tts_speed": 1.0,
  "selected_voice_key": "custom",
  "split_min_chars": 10,
  "split_max_chars": 32,
  "transitions_enabled": false,
  "subtitle_settings": {
    "font_size": 52,
    "margin_v": 90,
    "enable_background": true,
    "bg_color": "#000000",
    "bg_opacity": 55,
    "enable_highlight": false
  }
}
```

`subtitle_mode` 可使用 `none`、`srt` 或 `burn`。提交範例：

```bash
API=http://127.0.0.1:5174

curl -sS -X POST "$API/api/agent/video-jobs" \
  -F "pdf=@slides.pdf;type=application/pdf" \
  -F "reference_audio=@voice.wav;type=audio/wav" \
  -F "config_json=<job.json"
```

回應會包含 `run_id`、`job_id` 與 `status_url`。輪詢狀態：

```bash
curl -sS "$API/api/agent/video-jobs/<run_id>/<job_id>"
```

完成時 `status` 為 `completed`，`result` 會提供 `video_url`、`srt_url` 與
`bundle_url`；將相對路徑接在同一個 `API` 後即可下載。失敗時則會在
`error` 回傳原因。OpenAPI 文件位於 `http://127.0.0.1:8002/docs`。

## 預設模型

| 功能 | 模型 |
|---|---|
| TTS／語音克隆 | [openbmb/VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) |
| ASR | [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) |
| 強制對齊 | [Qwen/Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) |
| 選用 TTS | [Qwen/Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) |

模型可放在任意磁碟，再由建制精靈指定路徑；也可以讓精靈從 Hugging Face
下載到專案的 `models/`。詳細部署與替換 provider 方法請參閱
[deploy/README.md](deploy/README.md) 與 [docs/SPEECH_ADAPTERS.md](docs/SPEECH_ADAPTERS.md)。
預設模型都是公開倉庫，不需要 Hugging Face token；只有改用 gated 或私人模型時
才需要 token。建制精靈首次會提供匿名／token 選項，之後可由 `5 設定` 修改；
token 僅保存在權限為 `0600` 且不進 Git 的 `runtime/hf.env`。

## 資料與安全

- `data/video_runs/`：PDF、逐頁語音、SRT 與影片，不進 Git。
- `data/database/`：本機 SQLite，不進 Git。
- `models/`：模型掛載位置，權重不進 Git。
- `backend/.env`、`deploy/models.env`：本機設定，不進 Git。

預設是可信任內網使用的免登入模式。若要公開到 Internet，需另外啟用驗證、
HTTPS、來源限制、用量控制並重新審查檔案存取權限。

## 驗證

```bash
scripts/smoke_test.sh
scripts/smoke_test.sh --pdf /path/to/test.pdf
```

第一個命令檢查前後端與 API；第二個會額外驗證 PDF 上傳、持久化專案與頁面
影像。完整 GPU 產線可使用 `--full`，但會實際載入模型並產生影片。
