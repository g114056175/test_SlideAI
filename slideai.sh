#!/usr/bin/env bash
set -Eeuo pipefail

# SlideAI unified guided launcher and setup wizard.
# Linux x86_64 is the supported full-GPU deployment target; Ubuntu/Debian can
# install Docker automatically.  This one file owns setup and service control.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.yml"
MODEL_ENV="${PROJECT_ROOT}/deploy/models.env"
MODEL_ENV_EXAMPLE="${PROJECT_ROOT}/deploy/models.env.example"
BACKEND_ENV="${PROJECT_ROOT}/backend/.env"
BACKEND_ENV_EXAMPLE="${PROJECT_ROOT}/backend/.env.example"
PORT_STATE="${PROJECT_ROOT}/runtime/ports.env"
RUNTIME_DIR="${PROJECT_ROOT}/runtime"
HF_ENV="${RUNTIME_DIR}/hf.env"
DOCKER=(docker)

cd "${PROJECT_ROOT}"

say() { printf '%s\n' "$*"; }
info() { printf '[INFO] %s\n' "$*"; }
ok() { printf '[OK] %s\n' "$*"; }
skip() { printf '[略過] %s\n' "$*"; }
warn() { printf '警告：%s\n' "$*" >&2; }
die() { printf '錯誤：%s\n' "$*" >&2; exit 1; }

line() { printf '%s\n' '────────────────────────────────────────────────────────'; }

is_interactive() { [[ -t 0 && -t 1 ]]; }

pause_screen() {
  is_interactive || return 0
  read -r -p "按 Enter 返回主選單……" _
}

# Usage: confirm "Question" yes|no
confirm() {
  local prompt="$1" default="${2:-no}" answer suffix
  is_interactive || return 1
  [[ "${default}" == "yes" ]] && suffix='[Y/n]' || suffix='[y/N]'
  read -r -p "${prompt} ${suffix} " answer
  answer="${answer,,}"
  if [[ -z "${answer}" ]]; then
    [[ "${default}" == "yes" ]]
  else
    [[ "${answer}" == "y" || "${answer}" == "yes" ]]
  fi
}

ensure_config_files() {
  if [[ ! -f "${MODEL_ENV}" ]]; then
    cp "${MODEL_ENV_EXAMPLE}" "${MODEL_ENV}"
    info "已由範例建立 deploy/models.env。"
  fi
  if [[ ! -f "${BACKEND_ENV}" ]]; then
    cp "${BACKEND_ENV_EXAMPLE}" "${BACKEND_ENV}"
    info "已由範例建立 backend/.env。"
  fi
  chmod 600 "${BACKEND_ENV}"
}

env_value() {
  local file="$1" key="$2" value
  [[ -f "${file}" ]] || return 1
  value="$(awk -v key="${key}" '
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      sub("^[^=]*=[[:space:]]*", ""); found=$0
    }
    END { if (found != "") print found }
  ' "${file}")"
  [[ -n "${value}" ]] || return 1
  if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
    value="${value:1:${#value}-2}"
    value="${value//\\\"/\"}"
    value="${value//\\\$/\$}"
    value="${value//\\\\/\\}"
  elif [[ "${value}" == \'*\' && "${value}" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "${value}"
}

quote_env_value() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\\$}"
  value="${value//\`/\\\`}"
  printf '"%s"' "${value}"
}

set_env_value() {
  local file="$1" key="$2" value="$3" encoded temp
  [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "無效環境變數名稱：${key}"
  [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] || die "環境變數不可包含換行。"
  mkdir -p "$(dirname "${file}")"
  [[ -f "${file}" ]] || : > "${file}"
  encoded="$(quote_env_value "${value}")"
  temp="$(mktemp "${file}.tmp.XXXXXX")"
  awk -v key="${key}" -v replacement="${key}=${encoded}" '
    BEGIN { written=0 }
    $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      if (!written) print replacement
      written=1
      next
    }
    { print }
    END { if (!written) print replacement }
  ' "${file}" > "${temp}"
  mv "${temp}" "${file}"
  if [[ "${file}" == "${BACKEND_ENV}" ]]; then
    chmod 600 "${file}"
  fi
}

absolute_path() {
  local value="$1"
  if [[ "${value}" == /* ]]; then
    printf '%s' "${value}"
  else
    printf '%s/%s' "${PROJECT_ROOT}" "${value#./}"
  fi
}

load_model_settings() {
  ensure_config_files
  set -a
  # shellcheck disable=SC1090
  source "${MODEL_ENV}"
  set +a
}

load_hf_settings() {
  local inherited_token="${HF_TOKEN:-}"
  if [[ -f "${HF_ENV}" ]]; then
    # This file is written only by set_env_value below and is not committed.
    # shellcheck disable=SC1090
    source "${HF_ENV}"
  fi
  # An explicitly exported token takes precedence over the saved preference.
  [[ -n "${inherited_token}" ]] && HF_TOKEN="${inherited_token}"
  export HF_TOKEN="${HF_TOKEN:-}"
}

compose() {
  local args=(compose --env-file "${MODEL_ENV}" -f "${COMPOSE_FILE}")
  local frontend_port="${FRONTEND_PORT:-5174}"
  local backend_port="${BACKEND_PORT:-8002}"
  local source_revision="${SLIDEAI_SOURCE_REVISION:-$(source_revision)}"
  # sudo normally removes caller environment variables.  Pass the runtime
  # ports explicitly or Compose silently falls back to 5174/8002 while the
  # launcher health-checks a different pair.
  if [[ "${DOCKER[0]}" == "sudo" ]]; then
    sudo env \
      FRONTEND_PORT="${frontend_port}" \
      BACKEND_PORT="${backend_port}" \
      SLIDEAI_SOURCE_REVISION="${source_revision}" \
      docker "${args[@]}" "$@"
  else
    env \
      FRONTEND_PORT="${frontend_port}" \
      BACKEND_PORT="${backend_port}" \
      SLIDEAI_SOURCE_REVISION="${source_revision}" \
      "${DOCKER[@]}" "${args[@]}" "$@"
  fi
}

source_revision() {
  # Hash only inputs that can affect an image.  Using the whole Git commit
  # caused README/docs-only updates to re-export the multi-GB speech layer.
  local input_hashes
  input_hashes="$(git -C "${PROJECT_ROOT}" rev-parse \
    HEAD:backend HEAD:frontend HEAD:shared HEAD:docker HEAD:deploy \
    HEAD:docker-compose.yml 2>/dev/null)" || { printf 'unknown'; return; }
  printf '%s\n' "${input_hashes}" | sha256sum | awk '{print $1}'
}

image_revision() {
  "${DOCKER[@]}" image inspect \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
    "$1" 2>/dev/null || true
}

docker_images_current() {
  local expected backend_revision frontend_revision
  expected="$(source_revision)"
  [[ "${expected}" != "unknown" ]] || return 1
  backend_revision="$(image_revision slideai-backend)"
  frontend_revision="$(image_revision slideai-frontend)"
  [[ "${backend_revision}" == "${expected}" && "${frontend_revision}" == "${expected}" ]]
}

compose_service_running_here() {
  local service="$1" container_id working_dir running
  container_id="$(compose ps -q "${service}" 2>/dev/null | head -n 1)"
  [[ -n "${container_id}" ]] || return 1
  working_dir="$("${DOCKER[@]}" inspect \
    --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' \
    "${container_id}" 2>/dev/null || true)"
  running="$("${DOCKER[@]}" inspect --format '{{.State.Running}}' \
    "${container_id}" 2>/dev/null || true)"
  [[ "${running}" == "true" && "${working_dir}" == "${PROJECT_ROOT}" ]]
}

require_launcher_commands() {
  local missing=() command_name
  for command_name in curl python3; do
    command -v "${command_name}" >/dev/null 2>&1 || missing+=("${command_name}")
  done
  if (( ${#missing[@]} > 0 )); then
    warn "啟動工具缺少必要指令：${missing[*]}"
    warn "Ubuntu/Debian 可執行：sudo apt-get install -y curl python3"
    return 1
  fi
}

docker_access() {
  command -v docker >/dev/null 2>&1 || return 1
  docker compose version >/dev/null 2>&1 || return 1
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
    return 0
  fi
  if sudo -n docker info >/dev/null 2>&1; then
    DOCKER=(sudo docker)
    return 0
  fi
  return 1
}

read_port_state() {
  ACTIVE_FRONTEND_PORT=""
  ACTIVE_BACKEND_PORT=""
  ACTIVE_DEPLOY_MODE=""
  [[ -f "${PORT_STATE}" ]] || return 1
  local key value
  while IFS='=' read -r key value; do
    case "${key}" in
      FRONTEND_PORT) [[ "${value}" =~ ^[0-9]{4,5}$ ]] && ACTIVE_FRONTEND_PORT="${value}" ;;
      BACKEND_PORT) [[ "${value}" =~ ^[0-9]{4,5}$ ]] && ACTIVE_BACKEND_PORT="${value}" ;;
      DEPLOY_MODE) [[ "${value}" =~ ^(basic|native|docker)$ ]] && ACTIVE_DEPLOY_MODE="${value}" ;;
    esac
  done < "${PORT_STATE}"
  [[ -n "${ACTIVE_FRONTEND_PORT}" && -n "${ACTIVE_BACKEND_PORT}" ]]
}

save_port_state() {
  local mode="$1"
  mkdir -p "${RUNTIME_DIR}"
  {
    printf 'FRONTEND_PORT=%s\n' "${FRONTEND_PORT}"
    printf 'BACKEND_PORT=%s\n' "${BACKEND_PORT}"
    printf 'DEPLOY_MODE=%s\n' "${mode}"
  } > "${PORT_STATE}"
}

clear_port_state() {
  [[ -f "${PORT_STATE}" ]] && unlink "${PORT_STATE}" || true
}

port_is_available() {
  python3 - "$1" <<'PY'
import socket
import sys

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    try:
        sock.bind(("0.0.0.0", int(sys.argv[1])))
    except OSError:
        raise SystemExit(1)
PY
}

find_available_port() {
  local candidate="$1"
  while (( candidate <= 65535 )); do
    if port_is_available "${candidate}"; then
      printf '%s' "${candidate}"
      return 0
    fi
    ((candidate += 1))
  done
  return 1
}

select_runtime_ports() {
  local mode="$1" requested_frontend requested_backend reused=0
  requested_frontend="${FRONTEND_PORT:-5174}"
  requested_backend="${BACKEND_PORT:-8002}"
  [[ "${requested_frontend}" =~ ^[0-9]{4,5}$ ]] || die "FRONTEND_PORT 格式錯誤。"
  [[ "${requested_backend}" =~ ^[0-9]{4,5}$ ]] || die "BACKEND_PORT 格式錯誤。"

  if read_port_state \
    && [[ "${ACTIVE_DEPLOY_MODE}" == "${mode}" ]] \
    && curl -fsS "http://127.0.0.1:${ACTIVE_FRONTEND_PORT}/" >/dev/null 2>&1 \
    && curl -fsS "http://127.0.0.1:${ACTIVE_BACKEND_PORT}/api/health" >/dev/null 2>&1; then
    FRONTEND_PORT="${ACTIVE_FRONTEND_PORT}"
    BACKEND_PORT="${ACTIVE_BACKEND_PORT}"
    reused=1
  else
    FRONTEND_PORT="$(find_available_port "${requested_frontend}")" || die "找不到可用的前端 port。"
    BACKEND_PORT="$(find_available_port "${requested_backend}")" || die "找不到可用的後端 port。"
    if [[ "${FRONTEND_PORT}" == "${BACKEND_PORT}" ]]; then
      BACKEND_PORT="$(find_available_port "$((BACKEND_PORT + 1))")" || die "找不到可用的後端 port。"
    fi
  fi
  export FRONTEND_PORT BACKEND_PORT
  if (( reused == 0 )) \
    && [[ "${FRONTEND_PORT}" != "${requested_frontend}" || "${BACKEND_PORT}" != "${requested_backend}" ]]; then
    info "預設 port 已占用，自動改用 frontend ${FRONTEND_PORT} / backend ${BACKEND_PORT}。"
  fi
}

announce_runtime_ports() {
  local label="$1" lan_ip
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  ok "${label}"
  say "  WebUI：http://127.0.0.1:${FRONTEND_PORT}/"
  [[ -n "${lan_ip}" ]] && say "  區網：http://${lan_ip}:${FRONTEND_PORT}/"
  say "  API：http://127.0.0.1:${BACKEND_PORT}/docs"
}

stop_native_processes() {
  local backend_port="${BACKEND_PORT:-8002}" frontend_port="${FRONTEND_PORT:-5174}"
  local backend_pattern="${PROJECT_ROOT}/backend/\\.venv/bin/python -m uvicorn backend\\.app\\.main:app .*--port ${backend_port}"
  local frontend_pattern="${PROJECT_ROOT}/frontend/node_modules/.bin/vite preview .*--port ${frontend_port}"
  pkill -f "${backend_pattern}" 2>/dev/null || true
  pkill -f "${frontend_pattern}" 2>/dev/null || true
}

required_models_ready() {
  load_model_settings
  local failed=0 label key value path
  while IFS='|' read -r label key; do
    value="${!key:-}"
    path="$(absolute_path "${value}")"
    if model_ready "${path}"; then
      ok "${label}"
    else
      warn "缺少 ${label}：${path}"
      failed=1
    fi
  done <<'EOF'
VoxCPM2 TTS|VOXTTS_MODEL_HOST_PATH
Qwen3 ASR|QWEN3_ASR_MODEL_HOST_PATH
Qwen3 ForcedAligner|QWEN3_ALIGNER_MODEL_HOST_PATH
EOF
  (( failed == 0 )) || {
    warn "前後端仍可啟動，但缺少模型的 TTS／ASR／強制對齊功能將無法使用。"
    warn "可稍後從主選單執行『6 建制』或在『5 設定』指定既有模型。"
    return 1
  }
}

prepare_basic_runtime() {
  command -v python3 >/dev/null 2>&1 || die "需要 Python 3。"
  command -v npm >/dev/null 2>&1 || die "需要 Node.js/npm。"
  if [[ ! -x "${PROJECT_ROOT}/backend/.venv/bin/python" ]]; then
    info "建立 backend 虛擬環境……"
    python3 -m venv "${PROJECT_ROOT}/backend/.venv"
    "${PROJECT_ROOT}/backend/.venv/bin/pip" install --quiet --disable-pip-version-check \
      -r "${PROJECT_ROOT}/backend/requirements.txt"
  fi
  if [[ ! -d "${PROJECT_ROOT}/frontend/node_modules" ]]; then
    info "安裝 frontend 依賴……"
    npm --prefix "${PROJECT_ROOT}/frontend" ci --no-audit --no-fund --loglevel=error
  fi
}

start_basic_services() {
  ensure_config_files
  prepare_basic_runtime
  load_model_settings
  select_runtime_ports basic
  local backend_port="${BACKEND_PORT}" frontend_port="${FRONTEND_PORT}" lan_ip attempt
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  stop_native_processes
  (
    set -a
    # shellcheck disable=SC1090
    source "${BACKEND_ENV}"
    set +a
    export VIDEO_ABSTRACT_LOCAL_ONLY=true VIDEO_ABSTRACT_MOCK_MODE=true
    export FRONTEND_URL="http://127.0.0.1:${frontend_port},http://localhost:${frontend_port}"
    [[ -n "${lan_ip}" ]] && export FRONTEND_URL="${FRONTEND_URL},http://${lan_ip}:${frontend_port}"
    export SLIDEAI_VIDEO_RUNS_DIR="${PROJECT_ROOT}/data/video_runs"
    nohup setsid "${PROJECT_ROOT}/backend/.venv/bin/python" -m uvicorn \
      backend.app.main:app --host 0.0.0.0 --port "${backend_port}" --no-access-log \
      >/dev/null 2>&1 < /dev/null &
  )
  (
    cd "${PROJECT_ROOT}/frontend"
    VITE_API_BASE_URL=/api npm run build >/dev/null
    VITE_API_PROXY_TARGET="http://127.0.0.1:${backend_port}" \
      nohup setsid npm run preview -- --host 0.0.0.0 --port "${frontend_port}" --strictPort \
      >/dev/null 2>&1 < /dev/null &
  )
  for attempt in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${backend_port}/api/health" >/dev/null 2>&1 \
      && curl -fsS "http://127.0.0.1:${frontend_port}/" >/dev/null 2>&1; then
      save_port_state basic
      announce_runtime_ports "基本前後端已啟動（未載入語音模型）"
      return 0
    fi
    sleep 1
  done
  stop_native_processes
  die "基本前後端未能在預期時間內啟動。"
}

check_native_runtimes() {
  local nano_python="${VOXTTS_RUNTIME_PYTHON_HOST_PATH:-}"
  local qwen_python="${QWEN_SPEECH_RUNTIME_PYTHON_HOST_PATH:-}"
  local tts_engine="${VOXTTS_ENGINE:-original}" tts_module="voxcpm" tts_label="VoxCPM"
  if [[ "${tts_engine}" == "nano_vllm" ]]; then
    tts_module="nanovllm_voxcpm"
    tts_label="Nano-vLLM VoxCPM"
  fi
  [[ -x "${nano_python}" ]] || { warn "${tts_label} runtime Python 不存在：${nano_python:-<未設定>}"; return 1; }
  [[ -x "${qwen_python}" ]] || { warn "Qwen runtime Python 不存在：${qwen_python:-<未設定>}"; return 1; }
  "${nano_python}" -c "import ${tts_module}" >/dev/null 2>&1 \
    || { warn "${tts_label} runtime 無法匯入 ${tts_module}。"; return 1; }
  "${qwen_python}" -c 'import qwen_asr' >/dev/null 2>&1 \
    || { warn "Qwen runtime 無法匯入 qwen_asr。"; return 1; }
}

start_native_services() {
  if ! required_models_ready || ! check_native_runtimes; then
    warn "Native 語音環境未完整，改以基本前後端模式啟動。"
    start_basic_services
    return
  fi
  prepare_basic_runtime
  select_runtime_ports native
  local backend_port="${BACKEND_PORT}" frontend_port="${FRONTEND_PORT}" lan_ip attempt
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  stop_native_processes
  (
    set -a
    # shellcheck disable=SC1090
    source "${BACKEND_ENV}"
    source "${MODEL_ENV}"
    set +a
    export VIDEO_ABSTRACT_LOCAL_ONLY="${VIDEO_ABSTRACT_LOCAL_ONLY:-true}"
    export VIDEO_ABSTRACT_MOCK_MODE="${VIDEO_ABSTRACT_MOCK_MODE:-false}"
    export VOXTTS_MODEL_PATH="$(absolute_path "${VOXTTS_MODEL_HOST_PATH}")"
    export QWEN3_ASR_MODEL_PATH="$(absolute_path "${QWEN3_ASR_MODEL_HOST_PATH}")"
    export QWEN3_ALIGNER_MODEL_PATH="$(absolute_path "${QWEN3_ALIGNER_MODEL_HOST_PATH}")"
    export QWEN3_TTS_MODEL_PATH="$(absolute_path "${QWEN3_TTS_MODEL_HOST_PATH}")"
    export VOXTTS_RUNTIME_PYTHON="${VOXTTS_RUNTIME_PYTHON_HOST_PATH}"
    export QWEN3_ASR_RUNTIME_PYTHON="${QWEN_SPEECH_RUNTIME_PYTHON_HOST_PATH}"
    export QWEN3_TTS_RUNTIME_PYTHON="${QWEN_SPEECH_RUNTIME_PYTHON_HOST_PATH}"
    export VOXTTS_NANO_WORKER_PATH="${PROJECT_ROOT}/backend/app/workers/nano_voxcpm_worker.py"
    export SLIDEAI_VIDEO_RUNS_DIR="${PROJECT_ROOT}/data/video_runs"
    export FRONTEND_URL="http://127.0.0.1:${frontend_port},http://localhost:${frontend_port}"
    [[ -n "${lan_ip}" ]] && export FRONTEND_URL="${FRONTEND_URL},http://${lan_ip}:${frontend_port}"
    nohup setsid "${PROJECT_ROOT}/backend/.venv/bin/python" -m uvicorn \
      backend.app.main:app --host 0.0.0.0 --port "${backend_port}" --no-access-log \
      >/dev/null 2>&1 < /dev/null &
  )
  (
    cd "${PROJECT_ROOT}/frontend"
    VITE_API_BASE_URL=/api npm run build >/dev/null
    VITE_API_PROXY_TARGET="http://127.0.0.1:${backend_port}" \
      nohup setsid npm run preview -- --host 0.0.0.0 --port "${frontend_port}" --strictPort \
      >/dev/null 2>&1 < /dev/null &
  )
  for attempt in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${backend_port}/api/health" >/dev/null 2>&1 \
      && curl -fsS "http://127.0.0.1:${frontend_port}/" >/dev/null 2>&1; then
      save_port_state native
      announce_runtime_ports "SlideAI 已以 Native 模式啟動"
      return 0
    fi
    sleep 1
  done
  die "Native 服務未能在預期時間內啟動。"
}

start_docker_services() {
  required_models_ready || true
  docker_access || die "Docker daemon 或 Compose 無法使用；請先執行建制。"
  nvidia_runtime_ready || die "Docker 尚未備妥 NVIDIA Container Runtime。"
  select_runtime_ports docker
  if ! docker_images_current; then
    if "${DOCKER[@]}" image inspect slideai-backend slideai-frontend >/dev/null 2>&1; then
      info "程式碼已更新，正在重建 Docker 映像……"
    else
      info "首次啟動需建立 Docker 映像……"
    fi
    if ! compose build; then
      die "Docker 映像建置失敗；請修正上方錯誤後再重試。"
    fi
  fi
  stop_native_processes
  compose up -d --no-build
  local attempt
  for attempt in $(seq 1 90); do
    if curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/health" >/dev/null 2>&1 \
      && curl -fsS "http://127.0.0.1:${FRONTEND_PORT}/" >/dev/null 2>&1; then
      save_port_state docker
      announce_runtime_ports "SlideAI 已以 Docker 模式啟動"
      return 0
    fi
    sleep 2
  done
  compose ps || true
  die "Docker 服務未能在預期時間內通過健康檢查。"
}

start_services() {
  require_launcher_commands || die "請先補齊啟動工具後重試。"
  ensure_config_files
  load_model_settings
  case "${SLIDEAI_DEPLOY_MODE:-docker}" in
    native) start_native_services ;;
    docker) start_docker_services ;;
    *) die "未知部署模式：${SLIDEAI_DEPLOY_MODE}" ;;
  esac
}

stop_services() {
  read_port_state || true
  FRONTEND_PORT="${ACTIVE_FRONTEND_PORT:-${FRONTEND_PORT:-5174}}"
  BACKEND_PORT="${ACTIVE_BACKEND_PORT:-${BACKEND_PORT:-8002}}"
  export FRONTEND_PORT BACKEND_PORT
  stop_native_processes
  load_model_settings
  if [[ "${ACTIVE_DEPLOY_MODE:-}" == "docker" || "${SLIDEAI_DEPLOY_MODE:-docker}" == "docker" ]]; then
    if docker_access; then compose down; else warn "無法存取 Docker；可能仍有容器運作。"; fi
  fi
  clear_port_state
  ok "SlideAI 已停止"
}

restart_services() {
  stop_services
  start_services
}

host_preflight() {
  local kernel arch available_kb available_gb
  require_launcher_commands || return 1
  kernel="$(uname -s 2>/dev/null || true)"
  arch="$(uname -m 2>/dev/null || true)"
  [[ "${kernel}" == "Linux" ]] || {
    warn "目前建制映像只支援 Linux；偵測到：${kernel:-unknown}。"
    return 1
  }
  case "${arch}" in
    x86_64|amd64) ok "Linux x86_64 主機" ;;
    *)
      warn "目前 GPU Dockerfile 使用 x86_64 專用 Flash Attention wheel，不支援 ${arch:-unknown}。"
      return 1
      ;;
  esac

  available_kb="$(df -Pk "${PROJECT_ROOT}" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [[ "${available_kb}" =~ ^[0-9]+$ ]]; then
    available_gb=$((available_kb / 1024 / 1024))
    if (( available_gb < 25 )); then
      warn "可用磁碟約 ${available_gb}GB；完整 Docker 映像、模型與建置快取建議至少預留 25GB。"
    else
      ok "可用磁碟：約 ${available_gb}GB"
    fi
  fi

  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    local gpu_name driver_version gpu_memory_mb
    gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1)"
    driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1)"
    gpu_memory_mb="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n 1 | tr -d ' ')"
    ok "NVIDIA GPU：${gpu_name:-detected}（driver ${driver_version:-unknown}，VRAM ${gpu_memory_mb:-unknown}MB）"
    if [[ "${gpu_memory_mb}" =~ ^[0-9]+$ ]] && (( gpu_memory_mb < 14000 )); then
      warn "VRAM 低於約 14GB；VoxCPM、ASR 或強制對齊可能需要縮短文本並避免其他 GPU 工作同時運行。"
    fi
  else
    warn "未偵測到可用的 NVIDIA driver/GPU。"
    warn "前後端映像仍可建置，但目前 VoxCPM、Qwen ASR 與強制對齊不保證可在 CPU/AMD GPU 執行。"
  fi
}

install_docker() {
  [[ -r /etc/os-release ]] || { warn "無法辨識作業系統。"; return 1; }
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" || "${ID_LIKE:-}" == *debian* ]] || {
    warn "自動安裝目前只支援 Ubuntu/Debian。${PRETTY_NAME:+ 偵測到 ${PRETTY_NAME}。}"
    warn "其他 Linux 發行版請先依官方方式安裝 Docker Engine + Compose v2，再重新執行建制。"
    return 1
  }
  say "即將透過 Debian/Ubuntu 套件安裝 Docker Engine 與 Compose v2。"
  confirm "確定繼續？" yes || return 1
  sudo apt-get update
  sudo apt-get install -y docker.io
  if apt-cache show docker-compose-v2 >/dev/null 2>&1; then
    sudo apt-get install -y docker-compose-v2
  else
    sudo apt-get install -y docker-compose-plugin
  fi
  sudo systemctl enable --now docker
  if ! docker info >/dev/null 2>&1; then
    sudo usermod -aG docker "${USER}"
    DOCKER=(sudo docker)
    warn "已將 ${USER} 加入 docker 群組；重新登入後可不使用 sudo。"
  fi
}

nvidia_runtime_ready() {
  docker_access || return 1
  "${DOCKER[@]}" info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'
}

install_nvidia_container_toolkit() {
  [[ -r /etc/os-release ]] || { warn "無法辨識作業系統。"; return 1; }
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" || "${ID_LIKE:-}" == *debian* ]] || {
    warn "NVIDIA Container Toolkit 自動安裝目前只支援 Ubuntu/Debian。"
    return 1
  }
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1 || {
    warn "請先安裝並確認 NVIDIA driver；腳本不會自動更換主機 driver。"
    return 1
  }
  say "即將依 NVIDIA 官方 stable repository 安裝 Container Toolkit 1.19.1。"
  confirm "確定安裝並重新啟動 Docker daemon？" yes || return 1
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg2
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update
  local toolkit_version="1.19.1-1"
  sudo apt-get install -y \
    "nvidia-container-toolkit=${toolkit_version}" \
    "nvidia-container-toolkit-base=${toolkit_version}" \
    "libnvidia-container-tools=${toolkit_version}" \
    "libnvidia-container1=${toolkit_version}"
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  docker_access || return 1
  nvidia_runtime_ready || { warn "Toolkit 已安裝，但 Docker 尚未回報 nvidia runtime。"; return 1; }
  ok "NVIDIA Container Toolkit / Docker runtime"
}

ensure_docker_for_build() {
  if docker_access; then
    ok "Docker Engine / Compose v2"
    return 0
  fi
  warn "Docker 尚未安裝、daemon 未啟動，或目前帳號沒有權限。"
  confirm "是否現在部署 Docker 前後端環境？" yes || return 1
  command -v docker >/dev/null 2>&1 || install_docker || return 1
  if ! docker_access; then
    if sudo docker info >/dev/null 2>&1; then
      DOCKER=(sudo docker)
    else
      warn "Docker daemon 仍無法使用。"
      return 1
    fi
  fi
  ok "Docker Engine / Compose v2"
}

model_ready() {
  local target="$1"
  [[ -d "${target}" && -f "${target}/config.json" ]] || return 1
  find "${target}" -maxdepth 2 -type f \
    \( -name '*.safetensors' -o -name '*.pth' -o -name '*.bin' \) \
    -print -quit 2>/dev/null | grep -q .
}

hf_download() {
  local repository="$1" target="$2" downloader_venv="${RUNTIME_DIR}/hf-downloader"
  load_hf_settings
  mkdir -p "${target}"
  if [[ -z "${HF_TOKEN:-}" ]]; then
    info "未設定 HF_TOKEN；公開模型仍可匿名下載，但速率限制會較低。"
  fi
  if command -v hf >/dev/null 2>&1; then
    HF_HUB_DISABLE_TELEMETRY=1 hf download "${repository}" --local-dir "${target}"
    return
  fi
  if docker_access; then
    info "未找到 hf CLI，使用一次性 Docker 下載器。"
    "${DOCKER[@]}" run --rm \
      --user "$(id -u):$(id -g)" \
      -e HF_REPO="${repository}" \
      -e HF_TOKEN="${HF_TOKEN:-}" \
      -e HF_HUB_DISABLE_TELEMETRY=1 \
      -e HOME=/tmp \
      -e HF_HOME=/tmp/huggingface \
      -v "${target}:/download" \
      python:3.12-slim \
      sh -c 'python -m venv /tmp/hf-venv \
        && /tmp/hf-venv/bin/pip install --quiet --no-cache-dir huggingface_hub \
        && /tmp/hf-venv/bin/hf download "$HF_REPO" --local-dir /download'
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    info "Docker 目前不可用，建立專案內輕量 Hugging Face 下載器。"
    mkdir -p "${RUNTIME_DIR}"
    if [[ ! -x "${downloader_venv}/bin/hf" ]]; then
      if ! python3 -m venv "${downloader_venv}"; then
        warn "無法建立 HF downloader；Ubuntu 可先安裝 python3-venv。"
        return 1
      fi
      if ! "${downloader_venv}/bin/pip" install --quiet --no-cache-dir huggingface_hub; then
        warn "huggingface_hub 安裝失敗。"
        return 1
      fi
    fi
    HF_TOKEN="${HF_TOKEN:-}" HF_HUB_DISABLE_TELEMETRY=1 \
      "${downloader_venv}/bin/hf" download "${repository}" --local-dir "${target}"
    return
  fi

  warn "自動下載需要 Python 3、hf CLI 或可用的 Docker。"
  return 1
}

configure_hf_token() {
  local force="${1:-0}" choice token mode
  mkdir -p "${RUNTIME_DIR}"
  load_hf_settings
  mode="$(env_value "${HF_ENV}" HF_AUTH_MODE 2>/dev/null || true)"
  if [[ "${force}" != "1" && ( -n "${HF_TOKEN:-}" || "${mode}" == "anonymous" ) ]]; then
    if [[ -n "${HF_TOKEN:-}" ]]; then
      ok "Hugging Face token 已設定（內容不顯示）"
    else
      info "Hugging Face 使用匿名下載（預設公開模型可用）"
    fi
    return 0
  fi

  cat <<'EOF'
Hugging Face 下載設定
  預設 VoxCPM2、Qwen3 ASR 與 ForcedAligner 都是公開模型，不需要 token。
  Token 只用於提高下載限額，或存取 gated／私人模型。

  1) 匿名下載（建議／預設）
  2) 輸入或更新 HF token
  3) 清除 token 並改用匿名下載
  0) 返回
EOF
  read -r -p "請選擇 [0-3]：" choice
  case "${choice:-1}" in
    1)
      set_env_value "${HF_ENV}" HF_TOKEN ""
      set_env_value "${HF_ENV}" HF_AUTH_MODE anonymous
      chmod 600 "${HF_ENV}"
      unset HF_TOKEN
      ok "將使用匿名方式下載公開模型"
      ;;
    2)
      read -r -s -p "請輸入 HF token（輸入不會顯示）：" token
      printf '\n'
      [[ -n "${token}" ]] || { warn "Token 為空，未修改設定。"; return 1; }
      confirm "確認儲存至本機 runtime/hf.env？" yes || { skip "HF token 設定"; return 0; }
      set_env_value "${HF_ENV}" HF_TOKEN "${token}"
      set_env_value "${HF_ENV}" HF_AUTH_MODE token
      chmod 600 "${HF_ENV}"
      export HF_TOKEN="${token}"
      ok "Hugging Face token 已儲存（內容不顯示）"
      ;;
    3)
      confirm "確定清除已儲存的 HF token？" no || { skip "清除 HF token"; return 0; }
      set_env_value "${HF_ENV}" HF_TOKEN ""
      set_env_value "${HF_ENV}" HF_AUTH_MODE anonymous
      chmod 600 "${HF_ENV}"
      unset HF_TOKEN
      ok "HF token 已清除，改用匿名下載"
      ;;
    0) return 0 ;;
    *) warn "無效選項。"; return 1 ;;
  esac
}

download_model_source() {
  local source="$1" target="$2"
  if [[ "${source}" == hf:* ]]; then
    hf_download "${source#hf:}" "${target}"
  elif [[ -d "${source}" ]]; then
    mkdir -p "${target}"
    cp -a "${source}/." "${target}/"
  else
    warn "slideai.sh 自動安裝目前支援 hf:repo 或既有本機資料夾。"
    warn "目前來源：${source:-<未設定>}"
    return 1
  fi
}

model_wizard() {
  local label="$1" path_key="$2" source_key="$3" default_path="$4" optional="${5:-0}"
  load_model_settings
  local configured source target choice custom
  configured="${!path_key:-${default_path}}"
  source="${!source_key:-}"
  target="$(absolute_path "${configured}")"

  if model_ready "${target}"; then
    ok "${label}：${target}"
    return 0
  fi

  warn "${label} 尚未就緒：${target}"
  cat <<EOF
  1) 自動下載至預設位置：${default_path}
  2) 指定既有模型資料夾
  3) 暫時跳過
EOF
  read -r -p "請選擇 [1-3]：" choice
  case "${choice:-3}" in
    1)
      target="$(absolute_path "${default_path}")"
      say "來源：${source:-<未設定>}"
      say "位置：${target}"
      confirm "確認下載 ${label}？" yes || { skip "${label}"; return "${optional}"; }
      [[ -n "${source}" ]] || { warn "${source_key} 未設定。"; return 1; }
      download_model_source "${source}" "${target}" || return 1
      model_ready "${target}" || { warn "下載完成但模型結構檢查未通過。"; return 1; }
      set_env_value "${MODEL_ENV}" "${path_key}" "${target}"
      ok "${label} 已完成"
      ;;
    2)
      read -r -p "請輸入 ${label} 的完整資料夾路徑：" custom
      [[ -n "${custom}" ]] || { warn "未輸入路徑。"; return 1; }
      target="$(absolute_path "${custom}")"
      if ! model_ready "${target}"; then
        warn "路徑檢查失敗；需要 config.json 與模型權重：${target}"
        return 1
      fi
      set_env_value "${MODEL_ENV}" "${path_key}" "${target}"
      ok "${label} 已連結至 ${target}"
      ;;
    3|"") skip "${label}"; return 0 ;;
    *) warn "無效選項，已跳過 ${label}。"; return 0 ;;
  esac
}

infer_llm_provider() {
  local key="$1" lower="${1,,}"
  [[ -n "${key}" ]] || { printf '未設定'; return; }
  case "${lower}" in
    sk-ant*) printf 'Anthropic' ;;
    sk-or-v1*) printf 'OpenRouter' ;;
    xai-*) printf 'xAI' ;;
    gsk_*) printf 'Groq' ;;
    sk*) printf 'OpenAI' ;;
    ai*|aq.*) printf 'Google Gemini' ;;
    *) printf '未知前綴' ;;
  esac
}

clear_llm_api_keys() {
  local env_key
  for env_key in api_key GOOGLE_API_KEY GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY OPENROUTER_API_KEY XAI_API_KEY GROQ_API_KEY CUSTOM_LLM_API_KEY EXTERNAL_LLM_API_KEY; do
    set_env_value "${BACKEND_ENV}" "${env_key}" ""
  done
}

llm_wizard() {
  ensure_config_files
  local choice provider provider_id key_var model_var default_model key model endpoint
  cat <<'EOF'
LLM API 設定
  1) Google Gemini
  2) OpenAI
  3) Anthropic Claude
  4) OpenRouter
  5) xAI
  6) Groq
  7) 自訂 OpenAI-compatible API（本地或其他服務）
  8) 清除目前 API key
  0) 暫時跳過
EOF
  read -r -p "請選擇 [0-8]：" choice
  case "${choice}" in
    1) provider="Google Gemini"; provider_id="google"; key_var="GOOGLE_API_KEY"; model_var="GOOGLE_GENERATIVE_MODEL"; default_model="gemini-2.5-flash" ;;
    2) provider="OpenAI"; provider_id="openai"; key_var="OPENAI_API_KEY"; model_var="OPENAI_MODEL"; default_model="gpt-4.1-mini" ;;
    3) provider="Anthropic Claude"; provider_id="anthropic"; key_var="ANTHROPIC_API_KEY"; model_var="ANTHROPIC_MODEL"; default_model="claude-3-5-sonnet-latest" ;;
    4) provider="OpenRouter"; provider_id="openrouter"; key_var="OPENROUTER_API_KEY"; model_var="OPENROUTER_MODEL"; default_model="openai/gpt-4.1-mini" ;;
    5) provider="xAI"; provider_id="xai"; key_var="XAI_API_KEY"; model_var="XAI_MODEL"; default_model="grok-3-mini" ;;
    6) provider="Groq"; provider_id="groq"; key_var="GROQ_API_KEY"; model_var="GROQ_MODEL"; default_model="llama-3.3-70b-versatile" ;;
    7)
      provider="自訂 OpenAI-compatible API"
      provider_id="custom"
      read -r -p "完整 chat/completions URL（例如 http://127.0.0.1:8081/v1/chat/completions）：" endpoint
      [[ "${endpoint}" =~ ^https?://[^[:space:]]+$ ]] || { warn "Endpoint 必須是 http:// 或 https:// 的完整 URL。"; return 1; }
      read -r -p "模型名稱／alias：" model
      [[ -n "${model}" ]] || { warn "自訂 API 必須指定模型名稱。"; return 1; }
      read -r -s -p "API key（本地無驗證服務可直接 Enter）：" key
      printf '\n'
      say "Provider：${provider}"
      say "Endpoint：${endpoint}"
      say "Model：${model}"
      confirm "確認儲存至 backend/.env？" yes || { skip "LLM API 設定"; return 0; }
      clear_llm_api_keys
      set_env_value "${BACKEND_ENV}" LLM_PROVIDER "custom"
      set_env_value "${BACKEND_ENV}" CUSTOM_LLM_ENDPOINT "${endpoint}"
      set_env_value "${BACKEND_ENV}" CUSTOM_LLM_MODEL "${model}"
      set_env_value "${BACKEND_ENV}" CUSTOM_LLM_API_KEY "${key}"
      set_env_value "${BACKEND_ENV}" api_key "${key}"
      ok "LLM 已設定：custom / ${model}"
      return 0
      ;;
    8)
      confirm "確定清除目前儲存的所有 LLM API key？" no || { skip "清除 API key"; return 0; }
      clear_llm_api_keys
      set_env_value "${BACKEND_ENV}" LLM_PROVIDER ""
      ok "API key 已清除"
      return 0
      ;;
    0|"") skip "LLM API 設定"; return 0 ;;
    *) warn "無效選項。"; return 1 ;;
  esac

  read -r -s -p "請輸入 ${provider} API key（輸入不會顯示）：" key
  printf '\n'
  [[ -n "${key}" ]] || { warn "API key 為空，未修改設定。"; return 1; }
  read -r -p "模型名稱 [${default_model}]：" model
  model="${model:-${default_model}}"
  say "Provider：${provider}"
  say "Model：${model}"
  confirm "確認儲存至 backend/.env？" yes || { skip "LLM API 設定"; return 0; }

  # Store an explicit provider instead of relying only on vendor-specific key
  # prefixes. This also supports newly issued keys whose prefix may change.
  clear_llm_api_keys
  set_env_value "${BACKEND_ENV}" LLM_PROVIDER "${provider_id}"
  set_env_value "${BACKEND_ENV}" api_key "${key}"
  set_env_value "${BACKEND_ENV}" "${key_var}" "${key}"
  set_env_value "${BACKEND_ENV}" "${model_var}" "${model}"
  ok "LLM 已設定：${provider} / ${model}"
}

configure_deployment() {
  ensure_config_files
  local mode frontend backend
  say "目前部署模式：$(env_value "${MODEL_ENV}" SLIDEAI_DEPLOY_MODE 2>/dev/null || printf 'docker')"
  cat <<'EOF'
  1) Docker（建議用於新環境）
  2) Native（沿用既有本機 venv）
  3) 只修改預設 ports
  0) 返回
EOF
  read -r -p "請選擇 [0-3]：" mode
  case "${mode}" in
    1) set_env_value "${MODEL_ENV}" SLIDEAI_DEPLOY_MODE docker; ok "已設定 Docker 模式" ;;
    2) set_env_value "${MODEL_ENV}" SLIDEAI_DEPLOY_MODE native; ok "已設定 Native 模式" ;;
    3) ;;
    0|"") return 0 ;;
    *) warn "無效選項。"; return 1 ;;
  esac
  if confirm "是否修改預設 ports？" no; then
    read -r -p "前端 port [5174]：" frontend
    read -r -p "後端 port [8002]：" backend
    frontend="${frontend:-5174}"; backend="${backend:-8002}"
    [[ "${frontend}" =~ ^[0-9]{4,5}$ && "${backend}" =~ ^[0-9]{4,5}$ ]] || { warn "Port 格式錯誤。"; return 1; }
    [[ "${frontend}" != "${backend}" ]] || { warn "前後端不可使用相同 port。"; return 1; }
    set_env_value "${MODEL_ENV}" FRONTEND_PORT "${frontend}"
    set_env_value "${MODEL_ENV}" BACKEND_PORT "${backend}"
    ok "預設 ports：frontend ${frontend} / backend ${backend}"
  fi
}

configure_tts_engine() {
  ensure_config_files
  local current choice runtime_path deploy_mode
  current="$(env_value "${MODEL_ENV}" VOXTTS_ENGINE 2>/dev/null || printf 'original')"
  deploy_mode="$(env_value "${MODEL_ENV}" SLIDEAI_DEPLOY_MODE 2>/dev/null || printf 'docker')"
  line
  say "VoxCPM2 執行方式（目前：${current}）"
  cat <<'EOF'
  1) 官方 VoxCPM2（預設／建議）
     相容性較高、環境較單純、顯存配置較自然，但生成速度較慢。
  2) Nano-vLLM 加速（選用）
     RTX 5090 長文本約可到 0.11–0.13 RTF；會增加一套 Torch、
     FlashAttention 與 CUDA 相依環境，映像更大，舊 GPU／驅動相容性較嚴格，
     且 vLLM 會預留設定比例的顯存。
  0) 保留目前設定
EOF
  read -r -p "請選擇 [0-2]：" choice
  case "${choice}" in
    1)
      set_env_value "${MODEL_ENV}" SLIDEAI_TTS_PROVIDER voxcpm
      set_env_value "${MODEL_ENV}" VOXTTS_ENGINE original
      set_env_value "${MODEL_ENV}" VOXTTS_INSTALL_NANO 0
      set_env_value "${MODEL_ENV}" VOXTTS_RUNTIME_PYTHON_CONTAINER /opt/slideai/voxcpm/bin/python
      ok "已選擇官方 VoxCPM2；Docker 不會建置 Nano-vLLM 環境。"
      ;;
    2)
      confirm "確定額外建置 Nano-vLLM／FlashAttention 加速環境？" no \
        || { skip "Nano-vLLM 設定"; return 0; }
      set_env_value "${MODEL_ENV}" SLIDEAI_TTS_PROVIDER voxcpm_nano
      set_env_value "${MODEL_ENV}" VOXTTS_ENGINE nano_vllm
      set_env_value "${MODEL_ENV}" VOXTTS_INSTALL_NANO 1
      set_env_value "${MODEL_ENV}" VOXTTS_RUNTIME_PYTHON_CONTAINER /opt/slideai/voxcpm-nano/bin/python
      ok "已選擇 Nano-vLLM；下次 Docker build 會加入加速環境。"
      ;;
    0|"") return 0 ;;
    *) warn "無效選項。"; return 1 ;;
  esac

  if [[ "${deploy_mode}" == "native" ]]; then
    runtime_path="$(env_value "${MODEL_ENV}" VOXTTS_RUNTIME_PYTHON_HOST_PATH 2>/dev/null || true)"
    read -r -p "Native TTS Python 路徑 [${runtime_path}]：" runtime_path
    if [[ -n "${runtime_path}" ]]; then
      set_env_value "${MODEL_ENV}" VOXTTS_RUNTIME_PYTHON_HOST_PATH "${runtime_path}"
    else
      warn "Native 模式仍需在『5 設定』指定可匯入對應 TTS 套件的 Python。"
    fi
  fi
}

configure_optional_qwen_tts() {
  ensure_config_files
  local current choice
  current="$(env_value "${MODEL_ENV}" QWEN3_TTS_INSTALL 2>/dev/null || printf '0')"
  line
  say "Qwen3 TTS 品質備援（目前：$([[ "${current}" == "1" ]] && printf '安裝' || printf '不安裝')）"
  cat <<'EOF'
  1) 不安裝（預設；VoxCPM2 主流程不需要）
  2) 額外安裝 Qwen3 TTS
     因 Transformers 版本與 Qwen3 ASR 不同，會建立獨立環境並增加約 5–6 GB。
  0) 保留目前設定
EOF
  read -r -p "請選擇 [0-2]：" choice
  case "${choice}" in
    1) set_env_value "${MODEL_ENV}" QWEN3_TTS_INSTALL 0; ok "不建置 Qwen3 TTS 選用環境" ;;
    2) set_env_value "${MODEL_ENV}" QWEN3_TTS_INSTALL 1; ok "將額外建置獨立 Qwen3 TTS 環境" ;;
    0|"") return 0 ;;
    *) warn "無效選項。"; return 1 ;;
  esac
}

configure_runtime() {
  ensure_config_files
  local timesteps memory idle align_idle engine
  engine="$(env_value "${MODEL_ENV}" VOXTTS_ENGINE 2>/dev/null || printf 'original')"
  read -r -p "VoxCPM inference timesteps [12]：" timesteps
  if [[ "${engine}" == "nano_vllm" ]]; then
    read -r -p "Nano-vLLM GPU memory utilization [0.50]：" memory
    read -r -p "Nano-vLLM 閒置卸載秒數 [120]：" idle
  else
    memory="$(env_value "${MODEL_ENV}" VOXTTS_NANO_GPU_MEMORY_UTILIZATION 2>/dev/null || printf '0.50')"
    idle="$(env_value "${MODEL_ENV}" VOXTTS_NANO_IDLE_TIMEOUT_SEC 2>/dev/null || printf '120')"
  fi
  read -r -p "強制對齊閒置卸載秒數 [60]：" align_idle
  timesteps="${timesteps:-12}"; memory="${memory:-0.50}"
  idle="${idle:-120}"; align_idle="${align_idle:-60}"
  [[ "${timesteps}" =~ ^[0-9]+$ ]] || { warn "timesteps 必須是整數。"; return 1; }
  [[ "${memory}" =~ ^0(\.[0-9]+)?$|^1(\.0+)?$ ]] || { warn "GPU memory utilization 必須介於 0 與 1。"; return 1; }
  [[ "${idle}" =~ ^[0-9]+$ && "${align_idle}" =~ ^[0-9]+$ ]] || { warn "卸載時間必須是整數秒。"; return 1; }
  set_env_value "${MODEL_ENV}" VOXTTS_NANO_TIMESTEPS "${timesteps}"
  set_env_value "${MODEL_ENV}" VOXTTS_NANO_GPU_MEMORY_UTILIZATION "${memory}"
  set_env_value "${MODEL_ENV}" VOXTTS_NANO_IDLE_TIMEOUT_SEC "${idle}"
  set_env_value "${MODEL_ENV}" QWEN3_ALIGNMENT_IDLE_TIMEOUT_SEC "${align_idle}"
  ok "語音 runtime 參數已更新"
}

settings_menu() {
  ensure_config_files
  local choice
  while true; do
    line
    cat <<'EOF'
設定
  1) 部署模式與 ports
  2) TTS 模型
  3) ASR 模型
  4) 強制對齊模型
  5) LLM API
  6) 語音 runtime 參數
  7) Hugging Face 下載設定
  0) 返回
EOF
    read -r -p "請選擇 [0-7]：" choice
    case "${choice}" in
      1) configure_deployment || true ;;
      2)
        model_wizard "VoxCPM2 TTS" VOXTTS_MODEL_HOST_PATH VOXCPM2_SOURCE "./models/tts/VoxCPM2" 0 || true
        configure_tts_engine || true
        ;;
      3) model_wizard "Qwen3 ASR" QWEN3_ASR_MODEL_HOST_PATH QWEN3_ASR_SOURCE "./models/asr/Qwen3-ASR-1.7B" 0 || true ;;
      4) model_wizard "Qwen3 ForcedAligner" QWEN3_ALIGNER_MODEL_HOST_PATH QWEN3_ALIGNER_SOURCE "./models/alignment/Qwen3-ForcedAligner-0.6B" 0 || true ;;
      5) llm_wizard || true ;;
      6) configure_runtime || true ;;
      7) configure_hf_token 1 || true ;;
      0|"") return 0 ;;
      *) warn "無效選項。" ;;
    esac
  done
}

build_frontend_backend() {
  load_model_settings
  local frontend="${FRONTEND_PORT:-5174}" backend="${BACKEND_PORT:-8002}" gpu_runtime=0
  if curl -fsS "http://127.0.0.1:${frontend}/" >/dev/null 2>&1 \
    && curl -fsS "http://127.0.0.1:${backend}/api/health" >/dev/null 2>&1; then
    ok "前後端目前可正常開啟（frontend ${frontend} / backend ${backend}）"
    confirm "是否仍要重新建置 Docker 映像？" no || return 0
  fi
  if ! ensure_docker_for_build; then
    skip "Docker 前後端建制"
    return 0
  fi
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    ok "NVIDIA driver / GPU"
    if nvidia_runtime_ready; then
      gpu_runtime=1
      ok "NVIDIA Container Runtime"
    else
      warn "Docker 尚未註冊 NVIDIA Container Runtime。"
      install_nvidia_container_toolkit && gpu_runtime=1 || skip "NVIDIA Container Toolkit"
    fi
  else
    warn "未偵測到 NVIDIA GPU；前後端可建置，但語音模型無法使用 GPU。"
  fi

  if "${DOCKER[@]}" image inspect slideai-backend slideai-frontend >/dev/null 2>&1; then
    ok "SlideAI Docker 映像已存在"
    confirm "是否重新建置映像？" no || return 0
  else
    confirm "是否開始建置 SlideAI 前後端 Docker 映像？" yes || { skip "Docker 映像建置"; return 0; }
  fi
  if ! compose build; then
    warn "Docker 映像建置失敗；不會嘗試啟動不存在或過期的映像。"
    return 1
  fi
  ok "Docker 前後端映像建置完成"
  if confirm "是否立即啟動並驗證前後端？" yes; then
    if (( gpu_runtime == 0 )); then
      warn "目前 Compose backend 要求 NVIDIA runtime，無法啟動完整 Docker 工作流。"
      if confirm "是否改以基礎前後端模式啟動（不含 LLM/TTS/ASR）？" yes; then
        start_basic_services
      else
        skip "啟動服務；Docker 映像已保留"
      fi
      return 0
    fi
    # Compose creates absent bind-mount sources as root-owned directories.
    # Create the configured model placeholders as the current user first so a
    # later Hugging Face download can write into them normally.
    local model_key model_path
    for model_key in VOXTTS_MODEL_HOST_PATH QWEN3_ASR_MODEL_HOST_PATH QWEN3_ALIGNER_MODEL_HOST_PATH QWEN3_TTS_MODEL_HOST_PATH; do
      model_path="$(absolute_path "${!model_key:-}")"
      [[ -n "${!model_key:-}" ]] && mkdir -p "${model_path}"
    done
    compose up -d --no-build
    info "等待前後端健康檢查……"
    local attempt
    for attempt in $(seq 1 90); do
      if curl -fsS "http://127.0.0.1:${frontend}/" >/dev/null 2>&1 \
        && curl -fsS "http://127.0.0.1:${backend}/api/health" >/dev/null 2>&1; then
        mkdir -p "$(dirname "${PORT_STATE}")"
        {
          printf 'FRONTEND_PORT=%s\n' "${frontend}"
          printf 'BACKEND_PORT=%s\n' "${backend}"
          printf 'DEPLOY_MODE=docker\n'
        } > "${PORT_STATE}"
        ok "前後端實際啟動驗證通過"
        return 0
      fi
      sleep 2
    done
    compose ps || true
    warn "映像建置成功，但服務未在三分鐘內通過 health check。"
    return 1
  fi
}

build_wizard() {
  is_interactive || die "建制精靈需要互動式終端。"
  local frontend_backend_ready=1
  ensure_config_files
  line
  say "SlideAI 首次建制精靈"
  say "每個階段都可以跳過；進行下載或安裝前會再次確認。"
  line

  host_preflight || {
    warn "主機不符合目前完整 GPU 映像的自動建制條件。"
    confirm "是否仍繼續嘗試建置基本前後端？" no || return 1
  }

  say "步驟 1/6：選擇 TTS 執行方式"
  configure_tts_engine || warn "沿用目前 TTS 執行方式。"
  configure_optional_qwen_tts || warn "沿用目前 Qwen3 TTS 選用設定。"

  line; say "步驟 2/6：前後端與 Docker"
  if ! build_frontend_backend; then
    frontend_backend_ready=0
    warn "前後端環境尚未完成；仍可設定模型與 API，但本次不會嘗試啟動。"
  fi

  line; say "步驟 3/6：TTS 模型"
  configure_hf_token || warn "Hugging Face 下載設定尚未完成；將嘗試匿名下載。"
  model_wizard "VoxCPM2 TTS" VOXTTS_MODEL_HOST_PATH VOXCPM2_SOURCE "./models/tts/VoxCPM2" 0 || warn "TTS 尚未完成。"

  line; say "步驟 4/6：ASR"
  model_wizard "Qwen3 ASR" QWEN3_ASR_MODEL_HOST_PATH QWEN3_ASR_SOURCE "./models/asr/Qwen3-ASR-1.7B" 0 || warn "ASR 尚未完成。"

  line; say "步驟 5/6：強制對齊"
  model_wizard "Qwen3 ForcedAligner" QWEN3_ALIGNER_MODEL_HOST_PATH QWEN3_ALIGNER_SOURCE "./models/alignment/Qwen3-ForcedAligner-0.6B" 0 || warn "強制對齊尚未完成。"
  load_model_settings
  if [[ "${QWEN3_TTS_INSTALL:-0}" == "1" ]]; then
    model_wizard "Qwen3 TTS（選用）" QWEN3_TTS_MODEL_HOST_PATH QWEN3_TTS_SOURCE "./models/tts/Qwen3-TTS-12Hz-1.7B-Base" 1 || true
  fi

  line; say "步驟 6/6：LLM API"
  llm_wizard || warn "LLM API 尚未完成；選擇『無』模式時仍可進入基本流程。"

  line
  say "建制精靈完成。以下為目前狀態："
  status_report
  if (( frontend_backend_ready == 0 )); then
    warn "Docker 映像尚未建置成功；請修正錯誤後再次執行『6 建制』。"
  elif confirm "是否現在啟動 SlideAI？" yes; then
    start_services
  fi
}

runtime_status() {
  local label="$1" python_path="$2" module="$3"
  if [[ -x "${python_path}" ]] && "${python_path}" -c "import ${module}" >/dev/null 2>&1; then
    ok "${label} runtime：${python_path}"
  elif [[ -n "${python_path}" ]]; then
    warn "${label} runtime 無法使用：${python_path}"
  else
    info "${label} runtime：Docker 模式由映像提供"
  fi
}

status_report() {
  ensure_config_files
  load_model_settings
  local frontend="${FRONTEND_PORT:-5174}" backend="${BACKEND_PORT:-8002}" state_mode=""
  local deploy_mode docker_ready=0 frontend_owned=1 backend_owned=1
  if [[ -f "${PORT_STATE}" ]]; then
    # shellcheck disable=SC1090
    source "${PORT_STATE}"
    frontend="${FRONTEND_PORT:-${frontend}}"; backend="${BACKEND_PORT:-${backend}}"
    state_mode="${DEPLOY_MODE:-}"
  fi
  deploy_mode="${state_mode:-${SLIDEAI_DEPLOY_MODE:-docker}}"
  if [[ "${deploy_mode}" == "docker" ]]; then
    frontend_owned=0
    backend_owned=0
    if docker_access; then
      docker_ready=1
      compose_service_running_here frontend && frontend_owned=1
      compose_service_running_here backend && backend_owned=1
    fi
  fi
  line
  say "服務"
  say "  部署模式：${deploy_mode}"
  if curl -fsS "http://127.0.0.1:${frontend}/" >/dev/null 2>&1 \
    && (( frontend_owned == 1 )); then
    ok "Frontend ${frontend}"
  elif curl -fsS "http://127.0.0.1:${frontend}/" >/dev/null 2>&1; then
    warn "Frontend port ${frontend} 有回應，但不屬於目前專案目錄"
  else
    warn "Frontend ${frontend} 未運作"
  fi
  if curl -fsS "http://127.0.0.1:${backend}/api/health" >/dev/null 2>&1 \
    && (( backend_owned == 1 )); then
    ok "Backend ${backend}"
  elif curl -fsS "http://127.0.0.1:${backend}/api/health" >/dev/null 2>&1; then
    warn "Backend port ${backend} 有回應，但不屬於目前專案目錄"
  else
    warn "Backend ${backend} 未運作"
  fi

  line; say "容器與硬體"
  if (( docker_ready == 1 )) || docker_access; then
    ok "Docker / Compose"
    compose ps 2>/dev/null || true
    if [[ "${deploy_mode}" == "docker" ]]; then
      if "${DOCKER[@]}" image inspect slideai-backend slideai-frontend >/dev/null 2>&1; then
        if docker_images_current; then
          ok "Docker 映像與目前程式碼版本一致"
        else
          warn "Docker 映像落後於目前程式碼；下次啟動會自動重建"
        fi
      else
        info "尚未建立 SlideAI Docker 映像"
      fi
    fi
  else
    warn "Docker daemon 或 Compose 無法使用"
  fi
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader | sed 's/^/  GPU：/'
  else
    warn "NVIDIA GPU 無法使用"
  fi

  line; say "模型"
  local label key value path
  while IFS='|' read -r label key; do
    value="${!key:-}"
    path="$(absolute_path "${value}")"
    if model_ready "${path}"; then ok "${label}：${path}"; else warn "${label} 缺少或不完整：${path}"; fi
  done <<'EOF'
VoxCPM2 TTS|VOXTTS_MODEL_HOST_PATH
Qwen3 ASR|QWEN3_ASR_MODEL_HOST_PATH
Qwen3 ForcedAligner|QWEN3_ALIGNER_MODEL_HOST_PATH
Qwen3 TTS（選用）|QWEN3_TTS_MODEL_HOST_PATH
EOF

  line; say "執行環境"
  if [[ "${SLIDEAI_DEPLOY_MODE:-docker}" == "native" ]]; then
    if [[ "${VOXTTS_ENGINE:-original}" == "nano_vllm" ]]; then
      runtime_status "VoxCPM Nano-vLLM" "${VOXTTS_RUNTIME_PYTHON_HOST_PATH:-}" nanovllm_voxcpm
    else
      runtime_status "官方 VoxCPM" "${VOXTTS_RUNTIME_PYTHON_HOST_PATH:-}" voxcpm
    fi
    runtime_status "Qwen Speech" "${QWEN_SPEECH_RUNTIME_PYTHON_HOST_PATH:-}" qwen_asr
    [[ -x "${PROJECT_ROOT}/backend/.venv/bin/python" ]] && ok "Backend venv" || warn "Backend venv 缺少"
    [[ -d "${PROJECT_ROOT}/frontend/node_modules" ]] && ok "Frontend node_modules" || warn "Frontend node_modules 缺少"
  else
    if [[ "${VOXTTS_ENGINE:-original}" == "nano_vllm" ]]; then
      info "Docker 模式：官方 VoxCPM + Nano-vLLM（選用加速）+ Qwen Speech"
    else
      info "Docker 模式：官方 VoxCPM + Qwen Speech（未安裝 Nano-vLLM）"
    fi
  fi

  line; say "LLM"
  local key provider llm_model llm_endpoint key_state
  key="$(env_value "${BACKEND_ENV}" api_key 2>/dev/null || true)"
  [[ -n "${key}" ]] || key="$(env_value "${BACKEND_ENV}" GOOGLE_API_KEY 2>/dev/null || true)"
  [[ -n "${key}" ]] || key="$(env_value "${BACKEND_ENV}" OPENAI_API_KEY 2>/dev/null || true)"
  [[ -n "${key}" ]] || key="$(env_value "${BACKEND_ENV}" ANTHROPIC_API_KEY 2>/dev/null || true)"
  [[ -n "${key}" ]] || key="$(env_value "${BACKEND_ENV}" OPENROUTER_API_KEY 2>/dev/null || true)"
  [[ -n "${key}" ]] || key="$(env_value "${BACKEND_ENV}" XAI_API_KEY 2>/dev/null || true)"
  [[ -n "${key}" ]] || key="$(env_value "${BACKEND_ENV}" GROQ_API_KEY 2>/dev/null || true)"
  provider="$(env_value "${BACKEND_ENV}" LLM_PROVIDER 2>/dev/null || true)"
  [[ -n "${provider}" ]] || provider="$(infer_llm_provider "${key}")"
  if [[ "${provider}" == "custom" ]]; then
    llm_model="$(env_value "${BACKEND_ENV}" CUSTOM_LLM_MODEL 2>/dev/null || true)"
    llm_endpoint="$(env_value "${BACKEND_ENV}" CUSTOM_LLM_ENDPOINT 2>/dev/null || true)"
    if [[ -n "${llm_model}" && -n "${llm_endpoint}" ]]; then
      if [[ -n "${key}" ]]; then key_state="已設定"; else key_state="未設定（允許本地無驗證服務）"; fi
      ok "自訂 LLM：${llm_model} / ${llm_endpoint}（API key ${key_state}）"
    else
      warn "自訂 LLM 尚缺 model 或 endpoint"
    fi
  elif [[ -n "${key}" ]]; then
    ok "API key 已設定（${provider}；不顯示內容）"
  else
    warn "LLM API key 尚未設定"
  fi
  line
}

main_menu() {
  is_interactive || die "不帶參數時需要互動式終端；可使用 start/stop/restart/status/settings/build。"
  local choice
  while true; do
    clear 2>/dev/null || true
    cat <<'EOF'
SlideAI 管理工具

  1) 啟動
  2) 關閉
  3) 重新啟動
  4) 狀態（服務、模型與環境）
  5) 設定
  6) 建制（首次安裝精靈）
  0) 離開
EOF
    read -r -p "請選擇 [0-6]：" choice
    case "${choice}" in
      1) start_services; pause_screen ;;
      2) stop_services; pause_screen ;;
      3) restart_services; pause_screen ;;
      4) status_report; pause_screen ;;
      5) settings_menu ;;
      6) build_wizard; pause_screen ;;
      0) say "已離開。"; return 0 ;;
      *) warn "無效選項：${choice}"; sleep 1 ;;
    esac
  done
}

command_name="${1:-}"
if [[ -z "${command_name}" ]]; then
  main_menu
else
  case "${command_name}" in
    start) start_services ;;
    stop) stop_services ;;
    restart) restart_services ;;
    status) status_report ;;
    settings|config) settings_menu ;;
    build|setup) build_wizard ;;
    -h|--help|help)
      say "用法：./slideai.sh [start|stop|restart|status|settings|build]"
      ;;
    *) die "未知指令：${command_name}" ;;
  esac
fi
