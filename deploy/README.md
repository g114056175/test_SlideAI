# Ubuntu deployment

使用者只需要操作專案根目錄的 `slideai.sh`。`docker-compose.yml` 描述服務；
`docker/` 內則是後端、前端映像的建置檔與 Nginx 設定，並不是另一套部署。

## 第一次設定

```bash
cp deploy/models.env.example deploy/models.env
# 可保留官方 Hugging Face 來源，或改成你另外發布的模型網址。
./slideai.sh
```

模型設定支援：

```dotenv
SLIDEAI_DEPLOY_MODE=docker
VOXTTS_MODEL_HOST_PATH=/mnt/models/VoxCPM2
QWEN3_ASR_MODEL_HOST_PATH=/mnt/models/Qwen3-ASR-1.7B
QWEN3_ALIGNER_MODEL_HOST_PATH=/mnt/models/Qwen3-ForcedAligner-0.6B
QWEN3_TTS_MODEL_HOST_PATH=/mnt/models/Qwen3-TTS-12Hz-1.7B-Base

# 只有上方路徑不存在時才會詢問使用下載來源：
VOXCPM2_SOURCE=hf:openbmb/VoxCPM2
QWEN3_ASR_SOURCE=hf:Qwen/Qwen3-ASR-1.7B
```

`*_MODEL_HOST_PATH` 不能直接填 Hugging Face 網址或 repo ID；它代表 Docker
要唯讀掛載的本機目錄。repo ID 請填入對應的 `*_SOURCE`。若主機沒有
`hf` CLI，Docker 模式會使用一次性 Python 容器下載，不污染主機 Python。

若主機已經有經過驗證的兩套語音環境，可避免 Docker 內重複安裝：

```dotenv
SLIDEAI_DEPLOY_MODE=native
VOXTTS_RUNTIME_PYTHON_HOST_PATH=/opt/voxcpm/.venv/bin/python
QWEN_SPEECH_RUNTIME_PYTHON_HOST_PATH=/opt/qwen-speech/.venv/bin/python
```

必要模型位置：

```text
models/
├── tts/VoxCPM2/
├── asr/Qwen3-ASR-1.7B/
└── alignment/Qwen3-ForcedAligner-0.6B/
```

Qwen3-TTS 是選用的品質備援，不會阻止主流程啟動。它與 Qwen3-ASR 使用
不同的 Transformers 版本，因此 Docker 會在 `QWEN3_TTS_INSTALL=1` 時建立
獨立環境。模型不必搬進專案；
四個 `*_MODEL_HOST_PATH` 可以各自指向不同磁碟。Docker 會將它們分別
唯讀掛載到容器內固定位置。

## Docker 結構

只有一份 Compose：

```text
docker-compose.yml
docker/
├── backend/Dockerfile
└── frontend/
    ├── Dockerfile
    └── nginx.conf
```

後端映像預設有三個隔離環境：

- 精簡 FastAPI backend；
- CUDA 13／Torch 2.11／官方 VoxCPM；
- Torch 2.11／Qwen ASR、TTS 與 ForcedAligner。

只有在 `slideai.sh` 建制精靈明確選擇 Nano-vLLM 時，才會額外建立 Torch
2.9／FlashAttention／Nano-vLLM 環境。一般設備不需要這一層。

模型不會寫入映像，而是從主機唯讀掛載，因此更新程式不需重新複製模型。
服務日誌由 Docker 管理，每個容器最多保留 3 份、每份 10MB；前端 access
log 與後端逐請求 access log 已關閉。服務摘要可由 `./slideai.sh` 選單的
「4 狀態」查看；需要容器細節時可執行 `docker compose logs --tail=200`。
專案目錄不再產生 PID、startup、shutdown 等零碎檔案。

## 替換模型或商用 API

TTS、ASR、強制對齊各自有 provider。若改用商用服務，把對應 provider
設為 `command`，並實作 `docs/SPEECH_ADAPTERS.md` 的 JSON stdin/stdout
介面即可，不需修改前端。

## 系統條件

- 建議 Ubuntu 24.04；
- 可使用 NVIDIA GPU 的驅動；
- Docker Engine、Compose v2；
- NVIDIA Container Toolkit；
- 官方 VoxCPM 映像使用 PyTorch cu130／Torch 2.11；選用 Nano-vLLM 時另加
  Torch 2.9 與對應 FlashAttention wheel，目前皆鎖定 Linux x86-64。

4060 Ti 等較舊架構仍可使用 cu130 wheel，但主機驅動必須足夠新。GPU
driver 由 NVIDIA Container Toolkit 注入；映像不再重複包含完整 CUDA
development toolkit。
