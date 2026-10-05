# syntax=docker/dockerfile:1
# FastAPI 서버 전용 슬림 이미지 - torch/Airflow/pandas/lightgbm 없음 (docs/adr/0006).
# ai_workspace에서는 요청 시점 추천(docs/adr/0015)이 재사용하는 MMR 패키지(numpy만 임포트) 하나만 가져온다.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /usr/local/bin/uv

COPY backend/requirements.txt /tmp/requirements-api.txt
RUN uv pip install --system --no-cache -r /tmp/requirements-api.txt

WORKDIR /app/backend
COPY backend/app ./app
# app.recsys.pipeline이 `from src.core.reranker import ...`로 쓰는 MMR(recommend_engine, 성승우 구현).
# src/core는 numpy만 임포트한다. recommend_engine의 나머지(pandas/scikit-learn/lightgbm 의존)는 넣지 않는다.
# 이 목록만으로 app.main이 뜨는지는 tests/recsys/test_api_image_import_closure.py와 CI 스모크가 확인한다.
COPY ai_workspace/recommend_engine/src/__init__.py ./src/__init__.py
COPY ai_workspace/recommend_engine/src/core ./src/core
# 오프라인 하네스와 같이 쓰는 피처 코어(ADR 0033): 후보 구성, 장기 프로필 상태, 서빙 피처 어댑터.
# 서빙이 쓰는 경로는 numpy만 임포트한다(pandas는 DataFrame을 돌려주는 함수 안에서만 읽는다).
COPY recsys_core ./recsys_core

RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app
USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
