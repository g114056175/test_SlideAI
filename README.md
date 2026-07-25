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

## 系統需求

- Ubuntu 24.04（目前主要測試平台）
- NVIDIA GPU；建議至少 16GB VRAM
- 可使用 GPU 的 NVIDIA driver
- Docker Engine、Docker Compose v2
- NVIDIA Container Toolkit
- 約 25GB 以上的 Docker／模型可用磁碟空間，實際依模型和映像快取而定

目前 Docker 映像鎖定 Linux x86-64、Python 3.12、CUDA 13 系列 PyTorch。
Windows 並非目前正式支援的直接部署平台。

## 一鍵啟動

```bash
git clone https://github.com/g114056175/test_SlideAI.git
cd test_SlideAI
```

準備 LLM 設定：

```bash
cp backend/.env.example backend/.env
# 編輯 backend/.env，填入 api_key、模型名稱與安全的 SECRET_KEY
```

然後執行：

```bash
chmod +x slideai.sh
./slideai.sh
```

不帶參數會顯示：

```text
1) 啟動
2) 建置 Docker
3) 停止
4) 重新啟動
5) 狀態
6) 環境檢查
7) 部署設定
8) 查看日誌
0) 離開
```

第一次選擇「啟動」時會依序檢查：

- Ubuntu、Docker Engine 與 Docker Compose；
- NVIDIA 驅動與 NVIDIA Container Toolkit；
- VoxCPM2、Qwen3-ASR、Qwen3 ForcedAligner 模型；
- `backend/.env` 與模型下載設定。

`deploy/models.env` 可直接填寫既有模型的絕對路徑；模型不需要搬入專案。
只有設定的路徑不存在時，腳本才會詢問是否依下載來源取得模型。使用者可
拒絕並自行修改。

已有可用的 Ubuntu venv 時可設定 `SLIDEAI_DEPLOY_MODE=native`，直接重用
模型與 runtime，不重新下載十多 GB 的 Torch/CUDA 套件。乾淨新主機則用
`SLIDEAI_DEPLOY_MODE=docker` 建立可攜映像。

啟動完成後：

- WebUI：`http://localhost:5174`
- Backend API：`http://localhost:8002`
- OpenAPI 文件：`http://localhost:8002/docs`

自動化環境仍可明確指定命令：

```bash
./slideai.sh start
./slideai.sh build
./slideai.sh stop
./slideai.sh restart
./slideai.sh status
./slideai.sh logs
./slideai.sh check
```

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
scripts/smoke_test.sh --full --pdf /path/to/test.pdf
```

完整檢查包含 PDF、LLM、TTS、ASR、強制對齊、SRT、燒錄字幕／無字幕影片
與合併輸出。

## 公開部署注意事項

預設 `VIDEO_ABSTRACT_LOCAL_ONLY=true` 適合內部網路與單機測試。若要直接
開放到 Internet，請改為 `false`、更換強隨機 `SECRET_KEY`、設定 HTTPS、
限制 CORS、配置正式資料庫，並重新檢查帳號、權限、用量與檔案保存政策。
