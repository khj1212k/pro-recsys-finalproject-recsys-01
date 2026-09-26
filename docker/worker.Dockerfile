# syntax=docker/dockerfile:1
# worker/scheduler/migrate 공용 이미지: ai_workspace 파이프라인 + CPU 전용 torch + BGE-M3 +
# supercronic. linux/arm64(Oracle A1, Apple Silicon의 colima)와 linux/amd64 모두 빌드된다.

# --- 의존성 빌드 단계: hdbscan은 linux/aarch64 휠이 없어 gcc로 소스 빌드해야 한다.
#     컴파일러는 이 단계에만 두고 최종 이미지에는 완성된 venv만 복사한다.
FROM python:3.11-slim AS deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /usr/local/bin/uv
# --torch-backend cpu: torch/torchvision을 PyTorch CPU 인덱스(+cpu 휠)에서 받아 CUDA 의존성(수 GB)을 피한다.
COPY docker/requirements-worker.txt /tmp/requirements-worker.txt
RUN uv venv --python /usr/local/bin/python3.11 /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache --torch-backend cpu \
       -r /tmp/requirements-worker.txt

# --- 런타임 이미지
FROM python:3.11-slim

ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.49
ARG SUPERCRONIC_SHA256_AMD64=a53ae236602c7338aba3fbaff40bda6300eae3b9fedb8261eb06cfe3724430c1
ARG SUPERCRONIC_SHA256_ARM64=02aa0cb229ba09050cba6638059dadb9eedc2276632ea43d6a57a2f8c1629dd5

ENV PATH=/opt/venv/bin:$PATH \
    VIRTUAL_ENV=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/ai_workspace:/app/backend \
    HF_HOME=/hf \
    TOKENIZERS_PARALLELISM=false \
    TQDM_DISABLE=1

# libgomp1: lightgbm 런타임 의존성. curl/ca-certificates: supercronic 다운로드용.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) sha="${SUPERCRONIC_SHA256_AMD64}" ;; \
      arm64) sha="${SUPERCRONIC_SHA256_ARM64}" ;; \
      *) echo "unsupported arch: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}"; \
    echo "${sha}  /usr/local/bin/supercronic" | sha256sum -c -; \
    chmod +x /usr/local/bin/supercronic

COPY --from=deps /opt/venv /opt/venv

WORKDIR /app
COPY ai_workspace ./ai_workspace
COPY backend/app ./backend/app
COPY backend/alembic ./backend/alembic
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/scheduler ./backend/scheduler
COPY evaluation ./evaluation
COPY jobs ./jobs
COPY docker/crontab docker/scheduler-entrypoint.sh ./docker/

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /hf /app/logs /app/ai_workspace/logs \
       /app/ai_workspace/recommend_engine/checkpoints \
       /app/ai_workspace/recommend_engine/results \
       /app/ai_workspace/recommend_engine/logs \
    && chown -R app:app /hf /app/logs /app/ai_workspace/logs /app/ai_workspace/recommend_engine
USER app

# job_runs.git_sha에 남는 값. 커밋마다 바뀌므로 의존성 레이어 캐시를 깨지 않게 맨 끝에 둔다.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

ENTRYPOINT ["python", "-m", "jobs.run"]
CMD ["--help"]
