#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.yml"
MODEL_CONFIG="${PROJECT_ROOT}/deploy/models.env"
MODEL_CONFIG_EXAMPLE="${PROJECT_ROOT}/deploy/models.env.example"
BACKEND_ENV="${PROJECT_ROOT}/backend/.env"
BACKEND_ENV_EXAMPLE="${PROJECT_ROOT}/backend/.env.example"
NONINTERACTIVE="${SLIDEAI_NONINTERACTIVE:-0}"
DOCKER=(docker)

cd "${PROJECT_ROOT}"

say() { printf '%s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
fail() { printf '錯誤：%s\n' "$*" >&2; exit 1; }

confirm() {
  local prompt="$1"
  [[ "${NONINTERACTIVE}" == "1" ]] && return 1
  read -r -p "${prompt} [y/N] " answer
  [[ "${answer,,}" == "y" || "${answer,,}" == "yes" ]]
}

usage() {
  cat <<'EOF'
SlideAI Ubuntu 管理工具

  ./slideai.sh start    檢查環境、引導模型準備並啟動
  ./slideai.sh build    明確重建 Docker 映像
  ./slideai.sh stop     停止 SlideAI
  ./slideai.sh restart  重新啟動
  ./slideai.sh status   顯示容器與 GPU 狀態
  ./slideai.sh logs     追蹤服務紀錄
  ./slideai.sh setup    只進行環境與模型設定
  ./slideai.sh check    唯讀檢查，不安裝或下載

不帶參數時會顯示數字選單。
EOF
}

choose_command() {
  if [[ ! -t 0 ]]; then
    usage
    fail "非互動環境請明確指定指令，例如：./slideai.sh start"
  fi
  cat >&2 <<'EOF'
SlideAI 管理選單

  1) 啟動
  2) 建置 Docker
  3) 停止
  4) 重新啟動
  5) 狀態
  6) 環境檢查
  7) 部署設定
  8) 查看日誌
  0) 離開
EOF
  local choice
  read -r -p "請輸入選項 [0-8]：" choice
  case "${choice}" in
    1) printf 'start' ;;
    2) printf 'build' ;;
    3) printf 'stop' ;;
    4) printf 'restart' ;;
    5) printf 'status' ;;
    6) printf 'check' ;;
    7) printf 'setup' ;;
    8) printf 'logs' ;;
    0) printf 'exit' ;;
    *) fail "無效選項：${choice}" ;;
  esac
}

load_model_config() {
  local readonly="${1:-0}"
  if [[ ! -f "${MODEL_CONFIG}" ]]; then
    if [[ "${readonly}" == "1" ]]; then
      warn "deploy/models.env 尚未建立；以下使用範例中的預設路徑檢查。"
      set -a
      # shellcheck disable=SC1090
      source "${MODEL_CONFIG_EXAMPLE}"
      set +a
    else
      cp "${MODEL_CONFIG_EXAMPLE}" "${MODEL_CONFIG}"
      say "已建立 deploy/models.env；可填入你提供的模型網址。"
    fi
  fi
  set -a
  # shellcheck disable=SC1090
  source "$([[ -f "${MODEL_CONFIG}" ]] && printf '%s' "${MODEL_CONFIG}" || printf '%s' "${MODEL_CONFIG_EXAMPLE}")"
  set +a
  local variable value
  for variable in \
    VOXTTS_MODEL_HOST_PATH \
    QWEN3_ASR_MODEL_HOST_PATH \
    QWEN3_ALIGNER_MODEL_HOST_PATH \
    QWEN3_TTS_MODEL_HOST_PATH; do
    value="${!variable:-}"
    [[ -n "${value}" ]] || fail "${variable} 未設定。"
    if [[ "${value}" != /* ]]; then
      value="${PROJECT_ROOT}/${value#./}"
    fi
    printf -v "${variable}" '%s' "${value}"
    export "${variable}"
  done
  SLIDEAI_DEPLOY_MODE="${SLIDEAI_DEPLOY_MODE:-docker}"
  export SLIDEAI_DEPLOY_MODE
}

compose_cmd() {
  local options=(compose)
  [[ -f "${MODEL_CONFIG}" ]] && options+=(--env-file "${MODEL_CONFIG}")
  options+=(-f "${COMPOSE_FILE}")
  "${DOCKER[@]}" "${options[@]}" "$@"
}

ensure_app_config() {
  if [[ ! -f "${BACKEND_ENV}" ]]; then
    cp "${BACKEND_ENV_EXAMPLE}" "${BACKEND_ENV}"
    say "已建立 backend/.env；使用 LLM 前請自行填入 API key。"
  fi
}

is_ubuntu() {
  [[ -r /etc/os-release ]] || return 1
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" || "${ID_LIKE:-}" == *ubuntu* ]]
}

install_docker_ubuntu() {
  confirm "未找到 Docker，是否以 Ubuntu 套件安裝？" || return 1
  sudo apt-get update
  sudo apt-get install -y docker.io
  if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
    sudo apt-get install -y docker-compose-v2
  else
    sudo apt-get install -y docker-compose-plugin
  fi
  sudo systemctl enable --now docker
}

check_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    install_docker_ubuntu || fail "請先安裝 Docker Engine 與 Compose v2。"
  fi
  docker compose version >/dev/null 2>&1 || fail "找不到 Docker Compose v2。"
  if ! docker info >/dev/null 2>&1; then
    warn "目前帳號無法連線 Docker daemon。"
    if confirm "是否暫時使用 sudo 執行 Docker？"; then
      sudo docker info >/dev/null
      sudo docker compose version >/dev/null
      DOCKER=(sudo docker)
      say "本次使用 sudo；長期建議將帳號加入 docker 群組後重新登入。"
    else
      say "可執行：sudo usermod -aG docker \"${USER}\""
      say "之後登出再登入；或請系統管理員授權 Docker。"
      return 1
    fi
  fi
}

check_gpu_runtime() {
  command -v nvidia-smi >/dev/null 2>&1 || {
    warn "找不到 nvidia-smi；完整 TTS/ASR 工作流需要 NVIDIA GPU 驅動。"
    return 1
  }
  nvidia-smi >/dev/null 2>&1 || {
    warn "NVIDIA 驅動目前無法正常存取 GPU。"
    return 1
  }
  if ! "${DOCKER[@]}" info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    warn "Docker 尚未註冊 NVIDIA runtime。"
    say "請依 NVIDIA Container Toolkit 官方安裝指南設定後重新執行："
    say "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    return 1
  fi
}

model_ready() {
  local target="$1"
  [[ -f "${target}/config.json" ]] || return 1
  find "${target}" -maxdepth 1 -type f \
    \( -name '*.safetensors' -o -name '*.pth' -o -name '*.bin' \) \
    -print -quit | grep -q .
}

download_http_archive() {
  local source="$1" target="$2"
  local temp_dir archive extract_root payload
  temp_dir="$(mktemp -d)"
  archive="${temp_dir}/bundle"
  extract_root="${temp_dir}/extract"
  mkdir -p "${extract_root}"
  trap '[[ -n "${temp_dir:-}" && "${temp_dir}" == /tmp/* ]] && rm -rf "${temp_dir}"' RETURN
  curl -fL --retry 3 --progress-bar "${source}" -o "${archive}"
  case "${source%%\?*}" in
    *.zip) command -v unzip >/dev/null || fail "下載 ZIP 需要 unzip"; unzip -q "${archive}" -d "${extract_root}" ;;
    *.tar.gz|*.tgz) tar -xzf "${archive}" -C "${extract_root}" ;;
    *.tar.zst|*.tzst) tar --zstd -xf "${archive}" -C "${extract_root}" ;;
    *.tar) tar -xf "${archive}" -C "${extract_root}" ;;
    *) fail "無法辨識模型壓縮格式：${source}" ;;
  esac
  payload="${extract_root}"
  if [[ "$(find "${extract_root}" -mindepth 1 -maxdepth 1 | wc -l)" == "1" ]]; then
    local only_entry
    only_entry="$(find "${extract_root}" -mindepth 1 -maxdepth 1 -print -quit)"
    [[ -d "${only_entry}" ]] && payload="${only_entry}"
  fi
  mkdir -p "${target}"
  cp -a "${payload}/." "${target}/"
}

download_model() {
  local label="$1" source="$2" target="$3"
  [[ -n "${source}" ]] || {
    warn "${label} 尚未設定下載來源；請編輯 deploy/models.env。"
    return 1
  }
  say "準備 ${label} -> ${target}"
  if [[ "${source}" == hf:* ]]; then
    local repo="${source#hf:}"
    mkdir -p "${target}"
    if command -v hf >/dev/null 2>&1; then
      hf download "${repo}" --local-dir "${target}"
    elif command -v docker >/dev/null 2>&1 && "${DOCKER[@]}" info >/dev/null 2>&1; then
      say "本機沒有 hf CLI，改用一次性 Docker 下載器。"
      "${DOCKER[@]}" run --rm \
        --user "$(id -u):$(id -g)" \
        -e HF_REPO="${repo}" \
        -e HF_TOKEN \
        -v "${target}:/download" \
        python:3.12-slim \
        sh -c 'python -m pip install --quiet --no-cache-dir huggingface_hub && hf download "$HF_REPO" --local-dir /download'
    else
      warn "Hugging Face 來源需要 hf CLI 或可用的 Docker daemon。"
      say "可安裝：python3 -m pip install --user huggingface_hub"
      return 1
    fi
  elif [[ "${source}" =~ ^https?:// ]]; then
    download_http_archive "${source}" "${target}"
  elif [[ -d "${source}" ]]; then
    mkdir -p "${target}"
    cp -a "${source}/." "${target}/"
  else
    warn "${label} 來源不存在或格式不支援：${source}"
    return 1
  fi
  model_ready "${target}" || {
    warn "${label} 下載完成，但找不到 config.json 或模型權重。"
    return 1
  }
}

prepare_one_model() {
  local label="$1" path_var="$2" source_var="$3" required="$4"
  local target="${!path_var}"
  local source="${!source_var:-}"
  if model_ready "${target}"; then
    say "[OK] ${label}"
    return 0
  fi
  if [[ "${required}" == "0" && -z "${source}" ]]; then
    say "[略過] ${label} 未設定，主工作流不受影響。"
    return 0
  fi
  warn "缺少 ${label}：${target}"
  if confirm "是否依 deploy/models.env 的來源下載 ${label}？"; then
    download_model "${label}" "${source}" "${target}" && return 0
  fi
  [[ "${required}" == "0" ]] && return 0
  return 1
}

check_models_only() {
  local failed=0 label path_var target
  while IFS='|' read -r label path_var; do
    target="${!path_var}"
    if model_ready "${target}"; then
      say "[OK] ${label}：${target}"
    else
      warn "缺少 ${label}：${target}"
      failed=1
    fi
  done <<'EOF'
VoxCPM2 TTS|VOXTTS_MODEL_HOST_PATH
Qwen3 ASR|QWEN3_ASR_MODEL_HOST_PATH
Qwen3 ForcedAligner|QWEN3_ALIGNER_MODEL_HOST_PATH
EOF
  return "${failed}"
}

prepare_models() {
  local failed=0
  prepare_one_model "VoxCPM2 TTS" VOXTTS_MODEL_HOST_PATH VOXCPM2_SOURCE 1 || failed=1
  prepare_one_model "Qwen3 ASR" QWEN3_ASR_MODEL_HOST_PATH QWEN3_ASR_SOURCE 1 || failed=1
  prepare_one_model "Qwen3 ForcedAligner" QWEN3_ALIGNER_MODEL_HOST_PATH QWEN3_ALIGNER_SOURCE 1 || failed=1
  prepare_one_model "Qwen3 TTS（選用）" QWEN3_TTS_MODEL_HOST_PATH QWEN3_TTS_SOURCE 0 || true
  return "${failed}"
}

preflight() {
  is_ubuntu || warn "目前不是已確認的 Ubuntu 環境；建議使用 Ubuntu 24.04。"
  check_docker
  check_gpu_runtime
  load_model_config
  ensure_app_config
}

check_native_runtimes() {
  local nano_python="${VOXTTS_RUNTIME_PYTHON_HOST_PATH:-}"
  local qwen_python="${QWEN_SPEECH_RUNTIME_PYTHON_HOST_PATH:-}"
  [[ -x "${nano_python}" ]] || {
    warn "Nano runtime Python 不存在：${nano_python:-<未設定>}"
    return 1
  }
  [[ -x "${qwen_python}" ]] || {
    warn "Qwen speech runtime Python 不存在：${qwen_python:-<未設定>}"
    return 1
  }
  "${nano_python}" -c "import nanovllm_voxcpm" >/dev/null 2>&1 || {
    warn "Nano runtime 無法匯入 nanovllm_voxcpm。"
    return 1
  }
  "${qwen_python}" -c "import qwen_asr" >/dev/null 2>&1 || {
    warn "Qwen runtime 無法匯入 qwen_asr。"
    return 1
  }
  say "[OK] 既有 Nano／Qwen speech runtimes"
}

stop_legacy_native() {
  # One-time migration cleanup for processes started by the removed native
  # launchers. Future deployments are managed exclusively by Docker Compose.
  pkill -f '(^|/)backend/\.venv/bin/python -m uvicorn backend\.app\.main:app .*--port 8002$' 2>/dev/null || true
  pkill -f '^node spa-server\.cjs$' 2>/dev/null || true
  pkill -f '^npm run preview --host 0\.0\.0\.0 --port 5174$' 2>/dev/null || true
  pkill -f 'node .*/vite\.js preview .*--port 5174' 2>/dev/null || true
}

start_app() {
  load_model_config
  ensure_app_config
  if [[ "${SLIDEAI_DEPLOY_MODE,,}" == "native" ]]; then
    prepare_models || fail "必要模型尚未就緒。可填寫 deploy/models.env，或手動放入 models/ 對應位置。"
    start_native
    return
  fi
  preflight
  prepare_models || fail "必要模型尚未就緒。可填寫 deploy/models.env，或手動放入 models/ 對應位置。"
  # Never take down the working native service before a first Docker build has
  # completed. A failed build must not leave the user without a WebUI.
  if ! "${DOCKER[@]}" image inspect slideai-backend slideai-frontend >/dev/null 2>&1; then
    say "首次啟動：先完成 Docker 映像建置，現有服務會保持運作。"
    compose_cmd build
  fi
  stop_legacy_native
  compose_cmd up -d --no-build
  say "等待服務健康檢查……"
  local i
  for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${FRONTEND_PORT:-5174}/" >/dev/null 2>&1; then
      say "SlideAI 已啟動：http://127.0.0.1:${FRONTEND_PORT:-5174}"
      return 0
    fi
    sleep 2
  done
  compose_cmd ps
  fail "服務未在預期時間內就緒，請執行 ./slideai.sh logs。"
}

start_native() {
  check_native_runtimes
  command -v npm >/dev/null 2>&1 || fail "Native 模式需要 Node.js/npm。"
  [[ -x "${PROJECT_ROOT}/backend/.venv/bin/python" ]] || fail "缺少 backend/.venv。"
  if curl -fsS http://127.0.0.1:8002/docs >/dev/null 2>&1 \
    && curl -fsS http://127.0.0.1:5174/ >/dev/null 2>&1; then
    say "SlideAI 已在 native 模式運作：http://127.0.0.1:5174"
    return
  fi
  stop_legacy_native
  (
    set -a
    # shellcheck disable=SC1090
    source "${BACKEND_ENV}"
    set +a
    # Preserve the original local-demo semantics from the previous launcher:
    # PDF/TTS workflow remains usable without an OAuth token on this internal
    # deployment, while explicit production deployments can set this false.
    export VIDEO_ABSTRACT_LOCAL_ONLY="${VIDEO_ABSTRACT_LOCAL_ONLY:-true}"
    export VIDEO_ABSTRACT_MOCK_MODE="${VIDEO_ABSTRACT_MOCK_MODE:-false}"
    export SLIDEAI_TTS_PROVIDER SLIDEAI_ASR_PROVIDER SLIDEAI_ALIGNMENT_PROVIDER
    export VOXTTS_NANO_TIMESTEPS VOXTTS_NANO_GPU_MEMORY_UTILIZATION
    export VOXTTS_NANO_IDLE_TIMEOUT_SEC QWEN3_ALIGNMENT_IDLE_TIMEOUT_SEC
    export VOXTTS_MODEL_PATH="${VOXTTS_MODEL_HOST_PATH}"
    export QWEN3_ASR_MODEL_PATH="${QWEN3_ASR_MODEL_HOST_PATH}"
    export QWEN3_ALIGNER_MODEL_PATH="${QWEN3_ALIGNER_MODEL_HOST_PATH}"
    export QWEN3_TTS_MODEL_PATH="${QWEN3_TTS_MODEL_HOST_PATH}"
    export VOXTTS_ENGINE=nano_vllm
    export VOXTTS_RUNTIME_PYTHON="${VOXTTS_RUNTIME_PYTHON_HOST_PATH}"
    export QWEN3_ASR_RUNTIME_PYTHON="${QWEN_SPEECH_RUNTIME_PYTHON_HOST_PATH}"
    export QWEN3_TTS_RUNTIME_PYTHON="${QWEN_SPEECH_RUNTIME_PYTHON_HOST_PATH}"
    export VOXTTS_NANO_WORKER_PATH="${PROJECT_ROOT}/backend/app/workers/nano_voxcpm_worker.py"
    export SLIDEAI_VIDEO_RUNS_DIR="${PROJECT_ROOT}/data/video_runs"
    local lan_ip
    lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    export FRONTEND_URL="http://127.0.0.1:5174,http://localhost:5174"
    [[ -n "${lan_ip}" ]] && export FRONTEND_URL="${FRONTEND_URL},http://${lan_ip}:5174"
    nohup setsid "${PROJECT_ROOT}/backend/.venv/bin/python" -m uvicorn \
      backend.app.main:app --host 0.0.0.0 --port 8002 \
      >/dev/null 2>&1 < /dev/null &
  )
  (
    cd "${PROJECT_ROOT}/frontend"
    npm run build >/dev/null
    nohup setsid npm run preview -- --host 0.0.0.0 --port 5174 \
      >/dev/null 2>&1 < /dev/null &
  )
  local i
  for i in $(seq 1 45); do
    if curl -fsS http://127.0.0.1:8002/docs >/dev/null 2>&1 \
      && curl -fsS http://127.0.0.1:5174/ >/dev/null 2>&1; then
      say "SlideAI 已以既有本機環境啟動：http://127.0.0.1:5174"
      return
    fi
    sleep 1
  done
  fail "Native 服務未能啟動。"
}

stop_app() {
  load_model_config 1
  stop_legacy_native
  if [[ "${SLIDEAI_DEPLOY_MODE,,}" == "native" ]]; then
    say "SlideAI 已停止。"
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    if docker info >/dev/null 2>&1; then
      DOCKER=(docker)
      compose_cmd down
    elif sudo -n docker info >/dev/null 2>&1; then
      DOCKER=(sudo docker)
      compose_cmd down
    else
      warn "Docker daemon 無權限存取；若仍有容器，請執行 sudo ./slideai.sh stop。"
    fi
  fi
  say "SlideAI 已停止。"
}

check_only() {
  local failed=0
  load_model_config 1
  is_ubuntu && say "[OK] Ubuntu" || { warn "非 Ubuntu"; failed=1; }
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    say "[OK] NVIDIA driver / GPU"
  else
    warn "NVIDIA 驅動或 GPU 無法使用"
    failed=1
  fi
  check_models_only || failed=1
  if [[ "${SLIDEAI_DEPLOY_MODE,,}" == "native" ]]; then
    say "[INFO] 部署模式：native（不會下載 Docker 語音環境）"
    check_native_runtimes || failed=1
    return "${failed}"
  fi
  command -v docker >/dev/null 2>&1 && say "[OK] Docker CLI" || { warn "缺少 Docker"; failed=1; }
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    say "[OK] Docker daemon"
    docker compose version >/dev/null 2>&1 && say "[OK] Compose v2" || { warn "缺少 Compose v2"; failed=1; }
    check_gpu_runtime && say "[OK] NVIDIA Container Toolkit" || failed=1
  else
    warn "Docker daemon 無法存取"
    say "處理方式：sudo usermod -aG docker \"${USER}\"，之後登出再登入。"
    failed=1
  fi
  return "${failed}"
}

setup_app() {
  load_model_config
  ensure_app_config
  if [[ "${SLIDEAI_DEPLOY_MODE,,}" == "native" ]]; then
    prepare_models || fail "仍缺少必要模型。"
    check_native_runtimes
  else
    preflight
    prepare_models || fail "仍缺少必要模型。"
  fi
  say "部署環境已準備完成。"
}

status_app() {
  load_model_config 1
  if [[ "${SLIDEAI_DEPLOY_MODE,,}" == "native" ]]; then
    printf 'Frontend 5174: '
    curl -fsS http://127.0.0.1:5174/ >/dev/null 2>&1 && say "running" || say "stopped"
    printf 'Backend 8002: '
    curl -fsS http://127.0.0.1:8002/docs >/dev/null 2>&1 && say "running" || say "stopped"
  else
    check_docker
    compose_cmd ps
  fi
  command -v nvidia-smi >/dev/null 2>&1 \
    && nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader || true
}

command_name="${1:-}"
[[ -n "${command_name}" ]] || command_name="$(choose_command)"
case "${command_name}" in
  start) start_app ;;
  build) preflight; prepare_models || fail "仍缺少必要模型。"; compose_cmd build ;;
  stop) stop_app ;;
  restart) stop_app; start_app ;;
  setup) setup_app ;;
  check) check_only ;;
  status) status_app ;;
  logs)
    load_model_config 1
    if [[ "${SLIDEAI_DEPLOY_MODE,,}" == "native" ]]; then
      say "Native 精簡模式不寫入持久 log；請用 ./slideai.sh status 檢查。"
    else
      check_docker
      compose_cmd logs -f --tail=100
    fi
    ;;
  help|-h|--help) usage ;;
  exit) say "已離開。" ;;
  *) usage; fail "未知指令：${command_name}" ;;
esac
