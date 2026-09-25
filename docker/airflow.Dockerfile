# syntax=docker/dockerfile:1
# 선택 경로(compose --profile airflow): Airflow standalone + 잡 전용 venv.
# Airflow 자체 의존성과 잡 의존성(torch, sqlalchemy 2.x 등)을 한 환경에 섞으면 충돌하므로
# 잡은 /opt/jobs-venv의 파이썬으로 `python -m jobs.run <job>`을 실행한다(backend/airflow/dags/_jobs.py).
FROM apache/airflow:3.1.6-python3.11

USER root
COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /usr/local/bin/uv
COPY docker/requirements-worker.txt /tmp/requirements-worker.txt
# hdbscan은 linux/aarch64 휠이 없어 gcc가 필요하다 - 같은 레이어에서 설치 후 지워 이미지에 남기지 않는다.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 build-essential \
    && uv venv --python /usr/local/bin/python3.11 /opt/jobs-venv \
    && uv pip install --python /opt/jobs-venv/bin/python --no-cache --torch-backend cpu \
       -r /tmp/requirements-worker.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/newsletter
COPY ai_workspace ./ai_workspace
COPY backend/app ./backend/app
COPY backend/alembic ./backend/alembic
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/scheduler ./backend/scheduler
COPY backend/airflow/dags ./backend/airflow/dags
COPY evaluation ./evaluation
COPY jobs ./jobs
COPY docker/airflow-entrypoint.sh /opt/airflow-entrypoint.sh
RUN chmod +x /opt/airflow-entrypoint.sh \
    && mkdir -p /opt/newsletter/logs && chown -R airflow:0 /opt/newsletter/logs

ENV JOBS_PYTHON=/opt/jobs-venv/bin/python \
    HF_HOME=/hf \
    TOKENIZERS_PARALLELISM=false \
    TQDM_DISABLE=1
USER airflow

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/opt/airflow-entrypoint.sh"]
