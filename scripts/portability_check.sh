#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_ROOT}"

say() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "missing command: git"
command -v docker >/dev/null 2>&1 || fail "missing command: docker"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable"

bash -n slideai.sh
docker compose --env-file deploy/models.env.example config --quiet

for path in \
  backend/.env \
  deploy/models.env \
  runtime/hf.env \
  data/video_runs/portability-test.mp4 \
  models/tts/portability-test.safetensors; do
  git check-ignore -q "${path}" || fail "sensitive/generated path is not ignored: ${path}"
done

if [[ -x backend/.venv/bin/python ]]; then
  backend/.venv/bin/python -m pytest -q backend/tests
else
  say "SKIP: backend tests (backend/.venv is not installed)"
fi

if [[ -d frontend/node_modules ]]; then
  npm --prefix frontend run build
  npm audit --omit=dev --prefix frontend
else
  say "SKIP: frontend build/audit (frontend/node_modules is not installed)"
fi

say "PASS: launcher syntax, Compose, Git exclusions and available local tests"
