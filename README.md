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

## 效能概覽：5090 32GB 開發實測

以下是長中文文本、模型預熱後的約略 RTF（處理秒數／音訊或影片秒數）。不含首次載入、PDF 轉圖與排隊；文本、音色、驅動與顯存壓力都會影響結果。

| 模組／模式 | 約略 RTF | 說明 |
|---|---:|---|
| Qwen3-TTS 1.7B | `0.9–1.0` | 中英文及專有名詞較穩定，但速度不足以作預設。 |
| 官方 VoxCPM2 | `~0.3` | 環境較單純，適合作為一般設備預設。 |
| VoxCPM2 + Nano-vLLM | `0.11–0.13` | 速度主力；需要額外 CUDA／FlashAttention 相依。 |
| Qwen3 ForcedAligner | `~0.1` | 僅輸出 SRT／燒錄字幕時執行。 |
| ASS 字幕燒錄 | `~0.06` | 不需要內嵌字幕可略過。 |
| Nano 主流程 | `~0.25–0.35` | TTS、對齊、燒錄順序執行的常見總體級距。 |

Nano-vLLM 預設可使用 `VOXTTS_NANO_GPU_MEMORY_UTILIZATION=0.50`；16GB VRAM 約以 8GB 為目標預留工作空間，但能否運行仍取決於模型、驅動與其他 GPU 程序。worker 閒置會自動卸載。

## Agent API

不開 WebUI 也可提交 PDF、參考音檔與 JSON 工作單。範例在
[docs/agent-job.example.json](docs/agent-job.example.json)，完整格式、輪詢與輸出說明見
[deploy/README.md](deploy/README.md)。

<details>
<summary><strong>詳細說明：架構、設計取捨與開發紀錄</strong></summary>

> 本區預設收起。一般部署只需閱讀上方快速開始；替換模型、理解效能瓶頸或查看專案開發成果時，再展開子章節。

<details>
<summary><strong>系統模組與處理流程</strong></summary>

```text
PDF ─→ run manifest / 頁面影像 ─→ 手動或 LLM 逐頁講稿
                                      │
參考音檔／內建音色 ───────────────────┤
                                      ▼
             VoxCPM2（官方 / Nano-vLLM）或選用 Qwen3-TTS
                                      ▼
             標點斷句 → 每四句一個可持久化 WAV chunk
                                      ▼
       Qwen3 ForcedAligner ─→ SRT sidecar / ASS 字幕 ─→ FFmpeg 單頁 MP4
                                      ▼
                  選用插入式轉場 → 合併 MP4 / SRT / ZIP
```

TTS 依中英文標點切句，並將四句合併為一個 chunk：一次生成太長時後段容易聲線漂移，逐句生成又會失去語調上下文。英文句點會排除版本號、小數、IP 與常見縮寫，避免把 `CUDA 13.0`、`e.g.` 等誤切。

局部修正時只重生指定 chunk，但會重新合併整頁音訊，並以「新的整頁講稿 + 新音訊」重作對齊、字幕與本頁影片。因新段長度可能不同，不能只挪動後方時間戳；整頁對齊是較安全的方式。

TTS、ASR、對齊皆保留 provider／command adapter 邊界。替換本地模型或商用 API 時不需要重寫前端；格式見 [docs/SPEECH_ADAPTERS.md](docs/SPEECH_ADAPTERS.md)。

</details>

<details>
<summary><strong>前端、字幕與資料設計</strong></summary>

前端採 Vue。Gradio 的現成音訊元件不足以容納內建音色、上傳聲音、波形、下載、variant 切換與 chunk 修正，因此以 WaveSurfer.js 搭配自訂控制實作，讓試聽與修正留在同一工作區。

字幕曾評估 HTML/CSS、Canvas／Skia 與 Remotion。HTML/CSS 適合即時預覽，但長片擷取與字型一致性不足；Canvas 彈性高但逐幀成本與維護成本較高；Remotion 適合特殊動畫但流程較重。正式主流程選 **ASS + FFmpeg**，因其描邊、底色、逐字效果可控，且批次輸出穩定。

字幕以 TTS 原講稿做強制對齊，不直接把 ASR 結果當字幕，避免專有名詞被 ASR 改字。嚴重偏差才提示警告，避免正常「講稿 → TTS → 對齊」流程被干擾。

每次 TTS、對齊或渲染都建立 variant，使用者能比較、選用、刪除或局部重生，而不必每次從頭處理整份簡報。

</details>

<details>
<summary><strong>本地部署與可選公開展示</strong></summary>

程式主體以 Docker Compose 管理前端與 FastAPI 後端；模型使用主機路徑唯讀掛載，不把數十 GB 權重打進 image。TTS、ASR、對齊各自可使用相容 runtime，避免 Qwen TTS 與 ASR 的 Transformers 版本衝突。`slideai.sh` 建制精靈會詢問模型下載或既有路徑、Nano-vLLM 與 LLM／HF Token；公開 HF 模型可以匿名下載。

資料採 artifact-first：`data/video_runs/<run-id>/` 保存 input、`manifest.json`、逐頁 variants、chunk WAV、SRT、影片、job 與 merged output。這比把路徑全塞進 SQL 更容易人工檢查、備份、續跑與回復。manifest 寫入有程序鎖；SQLite 僅保留舊相容用途。若未來轉為多租戶、跨機 worker 或計費，再考慮 PostgreSQL／Redis。

本專案預設可信任內網免登入，不能直接視為公開 SaaS。公開展示曾掛載於 `https://awinlab-gate.g114056175.me/`，網址可能因免費期限或停用而失效，並非必要部署。展示鏈路概念為：

  ```text
  使用者 → 網域 → Cloudflare Proxy / Zero Trust 驗證
          → Worker 密碼閘門 → SlideAI 前後端
  ```

Cloudflare Access／真人驗證與 Worker 密碼閘門可降低掃描與未授權流量，但不取代應用層的來源限制、速率限制、上傳限制、日誌輪替與定期更新。若公開給未知使用者，應重新啟用登入、權限隔離與用量配額。

</details>

<details>
<summary><strong>效能、測試與後續方向</strong></summary>

### 已完成的主要調整

1. 比較 Qwen3-TTS、原始 VoxCPM2、Nano-vLLM VoxCPM2；採 VoxCPM2 為預設，Qwen3-TTS 作選用品質備援。
2. 加入中英文標點與四句 chunk，處理長文聲線漂移並支援局部修正。
3. 將工作流改為階段式：先完成 TTS 並釋放 worker，再載入對齊，降低顯存峰值。
4. 將字幕拆為無字幕、SRT、燒錄三模式；不需內嵌字幕時可跳過對齊與燒錄。
5. 加入可持久化 batch job、取消、續跑與 FIFO 排程。
6. 將全片 `xfade` 重編碼改為插入式短轉場；12 頁測試由約 25 秒降至約 4.5 秒。
7. 整理為單一 `slideai.sh`、模型環境檔與 portability／smoke test，方便 Git clone 後建制。

可執行 `scripts/portability_check.sh` 與 `scripts/smoke_test.sh`；`--full` 會實際載入模型並產生影片。後續方向包括 provider capability schema、結構化耗時紀錄、YouTube 章節文字、固定模板的 HTML 動態簡報，以及只在高可信 OCR 頁面啟用聚焦／Zoom。

</details>

</details>
