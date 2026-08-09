# SlideAI

SlideAI 是一套將 PDF 投影片轉成 AI 語音簡報影片的本地 Web 應用。使用者
可以生成或修改逐頁講稿、選擇參考聲音進行語音克隆、產生逐字時間軸，
最後輸出無字幕影片、SRT 字幕或燒錄字幕影片。

目前正式部署目標是 Ubuntu、NVIDIA GPU 與 Docker；模型權重、虛擬環境、
API key、資料庫和使用者產物不包含在 Git repository。

## 工作流程

```text
上傳 PDF
   ↓
PDF 逐頁影像與講稿（Gemini 或人工輸入）
   ↓
VoxCPM2 + Nano-vLLM 語音克隆
   ↓
Qwen3-ASR／Qwen3 ForcedAligner 產生逐字時間軸
   ↓
靜態投影片 + 語音 + ASS 字幕渲染
   ↓
逐頁版本管理 → 合併 MP4 → 下載影片／SRT
```

字幕有三種模式：

- 無字幕：略過 ASR／強制對齊，直接產出影片。
- 輸出 SRT：產生時間軸但不把字幕燒進影片。
- 燒錄字幕：輸出內嵌字幕 MP4，同時保留可下載 SRT。

TTS、ASR、強制對齊採階段式載入；切換到下一階段會釋放上一個 GPU worker，
並另有 60–120 秒閒置卸載機制作為保險，避免模型同時堆疊顯存。

「渲染全部」採單一後端程序的持久化 FIFO GPU queue。多人同時送出時只有
一個批次會進行 TTS → 對齊 → 渲染，其餘任務可看到前方任務數、目前工作站
階段及自己的逐頁進度；後端重啟後會依建立時間恢復尚未完成的任務。外部
LLM 呼叫則由 `LLM_MAX_CONCURRENCY` 設定全站共用併發上限，預設為 3。

## 系統需求

- Ubuntu 24.04（目前主要測試平台）
- NVIDIA GPU；建議至少 16GB VRAM
- 可使用 GPU 的 NVIDIA driver
- Docker Engine、Docker Compose v2
- NVIDIA Container Toolkit
- 約 25GB 以上的 Docker／模型可用磁碟空間，實際依模型和映像快取而定

目前 Docker 映像鎖定 Linux x86-64、Python 3.12、CUDA 13 系列 PyTorch。
Windows 並非目前正式支援的直接部署平台。

`slideai.sh` 是專案唯一的管理與建制入口。它會先檢查 Linux/x86-64、Docker、
NVIDIA GPU/runtime，再分別詢問是否建置前後端、下載或指定 TTS、ASR、
強制對齊模型，以及設定 LLM。Ubuntu/Debian 可由腳本協助安裝 Docker；其他
Linux 發行版只要事先備妥 Docker Engine、Compose v2、NVIDIA driver 與
Container Toolkit，仍可使用相同 Docker 映像。ARM、AMD GPU 與純 CPU
推論目前不屬於完整語音工作流的支援範圍。

若 Ubuntu/Debian 已有可用 NVIDIA driver、但 Docker 尚未註冊 GPU runtime，
建制精靈會在確認後依 [NVIDIA 官方指南](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
安裝 Container Toolkit 並設定 Docker；它不會自行更新或更換主機 kernel／driver。

```bash
chmod +x slideai.sh
./slideai.sh
```

LLM 可選 Gemini、OpenAI、Claude、OpenRouter、xAI、Groq，或自訂
OpenAI-compatible `chat/completions` URL。本地 llama.cpp 等無驗證 endpoint
可以留空 API key；實際憑證寫入權限為 `0600` 的 `backend/.env`。

## 下載與首次建制

```bash
git clone https://github.com/g114056175/test_SlideAI.git
cd test_SlideAI
chmod +x slideai.sh
./slideai.sh
```

不帶參數會顯示：

```text
1) 啟動
2) 關閉
3) 重新啟動
4) 狀態（服務、模型與環境）
5) 設定
6) 建制（首次安裝精靈）
0) 離開
```

第一次使用建議先選擇 `6 建制`。精靈會逐步檢查：

- 前後端 Docker 環境；
- NVIDIA driver 與 NVIDIA Container Toolkit；
- VoxCPM2、Qwen3-ASR、Qwen3 ForcedAligner；
- Gemini、OpenAI、Claude、OpenRouter、xAI、Groq 或自訂 LLM API。

每一個安裝或下載步驟都會先詢問。已有模型時可以選擇指定現有路徑，
不必重新下載；暫時只想確認前後端時，也可以跳過模型與 LLM。建制完成後
選擇 `1 啟動`，終端會印出本機與區網 WebUI 網址。

`deploy/models.env` 可直接填寫既有模型的絕對路徑；模型不需要搬入專案。
只有設定的路徑不存在時，腳本才會詢問是否依下載來源取得模型。使用者可
拒絕並自行修改。

已有可用的 Ubuntu venv 時可設定 `SLIDEAI_DEPLOY_MODE=native`，直接重用
模型與 runtime，不重新下載十多 GB 的 Torch/CUDA 套件。乾淨新主機則用
`SLIDEAI_DEPLOY_MODE=docker` 建立可攜映像。

啟動完成後：

- 免登入工作區：`http://localhost:5174/video-abstract-lab`
- Backend API：`http://localhost:8002`
- OpenAPI 文件：`http://localhost:8002/docs`

`5174/8002` 是預設起始 port。啟動時若其中一個已被占用，腳本會從該數字
開始向後尋找可用的四位數 port，例如 `5175/8003`，並讓前端代理自動連到
實際後端。最後選用的本機及區網網址會印在終端，亦會暫存在
`runtime/ports.env`，供 `status`、`stop`、`restart` 與 smoke test 使用；
該狀態檔不會提交至 Git。

目前內部版的主要工作流不需要註冊或登入；`/login`、`/register` 與舊管理
頁面是保留的相容路由，不是使用 `/video-abstract-lab` 的必要步驟。
瀏覽器的 API 請求使用同源 `/api`，由前端服務代理至 FastAPI；使用者端
不需要能直接連線後端 port，也不會把特定主機 IP 寫死在前端 bundle。

一般使用者直接使用數字選單即可。自動化環境亦可使用下列命令：

```bash
./slideai.sh start
./slideai.sh stop
./slideai.sh restart
./slideai.sh status
./slideai.sh settings
./slideai.sh build
```

`5 設定` 可在建制後修改部署模式、port、模型路徑、LLM 或語音 runtime
參數。`4 狀態` 只做檢查，不會下載模型或修改環境。

## WebUI 使用流程

1. 開啟終端顯示的 `/video-abstract-lab` 網址並上傳 PDF。
2. 選擇不使用 LLM，或讓 LLM 產生逐頁講稿；進入工作區後仍可人工修改。
3. 在「語音設定」選擇參考聲音、語速等參數。
4. 在「字幕設定」選擇無字幕、輸出 SRT 或燒錄字幕。
5. 可先渲染單頁試聽；確認後再執行「渲染全部」。
6. 選擇 `ALL` 預覽合併結果，並下載 MP4、SRT 或完整輸出。

多人同時渲染全部時，工作會依 FIFO 排隊；畫面會顯示前方任務數、目前
TTS／對齊／影片階段與逐頁進度。瀏覽器重新整理後也會接回尚未完成的任務。

## 模型

| 功能 | 預設模型 | Hugging Face |
|---|---|---|
| 主力 TTS／語音克隆 | VoxCPM2 2B | [openbmb/VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) |
| Nano TTS runtime | nano-vllm-voxcpm 2.0.3 | [a710128/nanovllm-voxcpm](https://github.com/a710128/nanovllm-voxcpm) |
| 參考聲音辨識 | Qwen3-ASR 1.7B | [Qwen/Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) |
| 逐字強制對齊 | Qwen3 ForcedAligner 0.6B | [Qwen/Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) |
| 選用品質備援 TTS | Qwen3-TTS 1.7B Base | [Qwen/Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) |

模型路徑、provider 與下載來源位於 `deploy/models.env`。第一次執行會由
`deploy/models.env.example` 建立實際設定檔。

`*_MODEL_HOST_PATH` 必須是下載完成後的本機目錄；Hugging Face repo ID
則填在 `*_SOURCE`：

```dotenv
VOXTTS_MODEL_HOST_PATH=./models/tts/VoxCPM2
VOXCPM2_SOURCE=hf:openbmb/VoxCPM2

QWEN3_ASR_MODEL_HOST_PATH=./models/asr/Qwen3-ASR-1.7B
QWEN3_ASR_SOURCE=hf:Qwen/Qwen3-ASR-1.7B
```

缺少模型時，腳本會先詢問是否下載。若主機沒有 `hf` CLI，Docker 模式會
使用一次性 Python 容器下載。模型也可放在其他磁碟，只要將
`*_MODEL_HOST_PATH` 改成對應絕對路徑。

LLM API key 與模型名稱位於 `backend/.env`。`backend/.env` 與
`deploy/models.env` 都不會提交到 Git。

## 5090 實測效能

測試環境：

- NVIDIA RTX 5090 32GB
- VoxCPM2 Nano-vLLM、`inference_timesteps=12`
- `gpu_memory_utilization=0.50`
- 單併發、長文本、依標點／四句分段

| 階段 | 長文本觀察值 | 說明 |
|---|---:|---|
| VoxCPM2 Nano TTS | 約 0.11–0.13 RTF | 約 125–155 秒語音需 14–19 秒；冷啟動另加數秒 |
| Qwen3 強制對齊 | 約 0.08–0.12 RTF | 短音檔會因模型啟動使表面 RTF 偏高 |
| ASS 字幕燒錄 | 約 0.04–0.08 RTF | 靜態投影片；解析度、字型與字幕效果會影響速度 |
| 無字幕影片輸出 | 通常低於字幕燒錄 | 可完全略過 ASR／強制對齊 |

RTF 是處理時間除以輸出音訊時間；數值越低越快。以上是目前開發機的
觀察範圍，不是所有 GPU、驅動、文本或聲線的保證值。短文本應另計模型
冷啟動時間。

在 32GB 5090 上以 `gpu_memory_utilization=0.25`，已成功模擬 16GB GPU
設為 `0.50` 的同等約 8GB Nano 預算並完成實際語音生成。因此 16GB 卡可先
從 `0.50` 測試，但必須避免其他程式同時大量佔用 VRAM。

## 資料與目錄

```text
backend/                 FastAPI、LLM、語音與影片服務
frontend/                Vue WebUI
shared/                  前後端共用字幕版面邏輯
docker/                  前後端 Dockerfile 與 Nginx 設定
deploy/                  模型來源與部署說明
models/                  預設模型掛載位置；權重不進 Git
data/database/           SQLite／資料庫持久資料；不進 Git
data/video_runs/         PDF、逐頁版本、SRT、MP4；不進 Git
slideai.sh               唯一管理入口
```

詳細設定請參閱 [deploy/README.md](deploy/README.md)。

## 驗證部署

部署後可先執行不產生資料的檢查；若有測試 PDF，可執行完整產線檢查，
測試 run 會自動刪除：

```bash
scripts/smoke_test.sh
scripts/smoke_test.sh --pdf /path/to/test.pdf
scripts/smoke_test.sh --full --pdf /path/to/test.pdf
```

第一個命令檢查前端、後端、歷史專案 API 與 CORS；加上 `--pdf` 會額外
驗證免登入 PDF 上傳、run 建立與投影片影像，且不需要模型。`--full` 才會
檢查 LLM、TTS、ASR、強制對齊、SRT、燒錄字幕／無字幕影片與合併輸出。

## 公開部署注意事項

預設 `VIDEO_ABSTRACT_LOCAL_ONLY=true` 適合內部網路與單機測試。若要直接
開放到 Internet，請改為 `false`、更換強隨機 `SECRET_KEY`、設定 HTTPS、
限制 CORS、配置正式資料庫，並重新檢查帳號、權限、用量與檔案保存政策。
