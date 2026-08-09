# syntax=docker/dockerfile:1.7
# SlideAI Ubuntu/NVIDIA image. PyTorch cu130 wheels provide the CUDA userspace
# libraries; NVIDIA Container Toolkit injects the host driver at runtime.
# VoxCPM and Qwen ASR stay in separate Python environments. Nano-vLLM and
# Qwen TTS are optional environments because their dependency constraints
# differ from the default runtimes.
FROM ubuntu:24.04

ARG INSTALL_VOXCPM_NANO=0
ARG INSTALL_QWEN_TTS=0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates ffmpeg fontconfig fonts-noto-cjk git libsndfile1 \
       poppler-utils python3.12 python3.12-venv \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /tmp/backend-requirements.txt
COPY deploy/requirements.voxcpm.txt /tmp/voxcpm-original-requirements.txt
COPY deploy/requirements.voxcpm-nano.txt /tmp/voxcpm-requirements.txt
COPY deploy/requirements.qwen-speech.txt /tmp/qwen-requirements.txt
COPY deploy/requirements.qwen-tts.txt /tmp/qwen-tts-requirements.txt

RUN python3.12 -m venv /opt/slideai/backend \
    && /opt/slideai/backend/bin/pip install -r /tmp/backend-requirements.txt \
    && python3.12 -m venv /opt/slideai/voxcpm \
    && /opt/slideai/voxcpm/bin/pip install \
       torch==2.11.0 torchaudio==2.11.0 \
       --index-url https://download.pytorch.org/whl/cu130 \
    && /opt/slideai/voxcpm/bin/pip install -r /tmp/voxcpm-original-requirements.txt \
    && if [ "${INSTALL_VOXCPM_NANO}" = "1" ]; then \
         python3.12 -m venv /opt/slideai/voxcpm-nano \
         && /opt/slideai/voxcpm-nano/bin/pip install \
            torch==2.9.0 torchaudio==2.9.0 \
            --index-url https://download.pytorch.org/whl/cu130 \
         && /opt/slideai/voxcpm-nano/bin/pip install \
            "https://github.com/Dao-AILab/flash-attention/releases/download/v2.8.3.post1/flash_attn-2.8.3%2Bcu13torch2.9cxx11abiTRUE-cp312-cp312-linux_x86_64.whl" \
         && /opt/slideai/voxcpm-nano/bin/pip install -r /tmp/voxcpm-requirements.txt; \
       fi \
    && python3.12 -m venv /opt/slideai/qwen-speech \
    && /opt/slideai/qwen-speech/bin/pip install \
       torch==2.11.0 torchaudio==2.11.0 \
       --index-url https://download.pytorch.org/whl/cu130 \
    && /opt/slideai/qwen-speech/bin/pip install -r /tmp/qwen-requirements.txt \
    && if [ "${INSTALL_QWEN_TTS}" = "1" ]; then \
         python3.12 -m venv /opt/slideai/qwen-tts \
         && /opt/slideai/qwen-tts/bin/pip install \
            torch==2.11.0 torchaudio==2.11.0 \
            --index-url https://download.pytorch.org/whl/cu130 \
         && /opt/slideai/qwen-tts/bin/pip install -r /tmp/qwen-tts-requirements.txt; \
       fi

COPY backend /app/backend
RUN mkdir -p /app/data/database /app/data/video_runs /models

# Keep the revision marker after the expensive dependency layer so a source
# update reuses the CUDA/Python environments and only rebuilds the small app
# layer.
ARG SLIDEAI_SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision="${SLIDEAI_SOURCE_REVISION}"

ENV PATH=/opt/slideai/backend/bin:$PATH \
    VOXTTS_NANO_WORKER_PATH=/app/backend/app/workers/nano_voxcpm_worker.py \
    VOXTTS_RUNTIME_PYTHON=/opt/slideai/voxcpm/bin/python \
    QWEN3_TTS_RUNTIME_PYTHON=/opt/slideai/qwen-tts/bin/python \
    QWEN3_ASR_RUNTIME_PYTHON=/opt/slideai/qwen-speech/bin/python

EXPOSE 8002
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
