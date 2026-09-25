# syntax=docker/dockerfile:1
# FastAPI 서버 전용 슬림 이미지 - torch/Airflow/ai_workspace 없음 (docs/adr/0006).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /usr/local/bin/uv

COPY backend/requirements.txt /tmp/requirements-api.txt
RUN uv pip install --system --no-cache -r /tmp/requirements-api.txt

WORKDIR /app/backend
COPY backend/app ./app

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
