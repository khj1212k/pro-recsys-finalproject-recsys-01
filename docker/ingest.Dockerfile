# syntax=docker/dockerfile:1
# Tier 0(OCI Always Free E2.1.Micro, 1 GB) 수집 전용 이미지. docker/worker.Dockerfile에서 torch·BGE-M3·
# hdbscan·lightgbm·LLM SDK를 뺀 것으로, 레이아웃(/app, PYTHONPATH, app 사용자, supercronic)은 같다.
# 돌 수 있는 잡: jobs.migrate, ingest --stages rss,extract, popularity, daily_report (ADR 0026).
# embed/cluster/generate/train은 이 이미지에서 ImportError가 난다 - docker/compose.micro.yaml이
# JOBS_DISABLED로 스케줄에서 끈다.
#
# 모든 의존성이 amd64/arm64 휠로 받아지므로(hdbscan 소스 빌드 없음) 컴파일러 단계가 필요 없다.
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
    TQDM_DISABLE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
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

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /usr/local/bin/uv
COPY docker/requirements-ingest.txt /tmp/requirements-ingest.txt
RUN uv venv --python /usr/local/bin/python3.11 /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/requirements-ingest.txt \
    && rm /usr/local/bin/uv

WORKDIR /app
COPY ai_workspace ./ai_workspace
COPY backend/app ./backend/app
COPY backend/alembic ./backend/alembic
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/scheduler ./backend/scheduler
COPY jobs ./jobs
COPY docker/crontab docker/crontab.micro docker/scheduler-entrypoint.sh ./docker/

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app/logs /app/ai_workspace/logs \
    && chown -R app:app /app/logs /app/ai_workspace/logs
USER app

ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

ENTRYPOINT ["python", "-m", "jobs.run"]
CMD ["--help"]
