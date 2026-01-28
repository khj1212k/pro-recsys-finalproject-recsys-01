# Recommend Engine (Serving Module)

이 모듈은 사전 계산된 벡터(Vector)를 기반으로 뉴스 추천을 수행합니다.

## 디렉토리 구조 및 역할

- **src/core/**: 추천 알고리즘 (Scorer, Reranker) 핵심 로직 [영구 보존]
- **src/data/loader.py**: 벡터 및 메타데이터 로더 (추후 DB Connector로 대체) [영구 보존]
- **scripts/bge_m3.py**: 임베딩 모델 (BGE-M3). 데이터 생성용으로만 사용 [통합 후 삭제 대상]
- **scripts/**: 데이터 생성 및 임베딩 스크립트 (ETL) [통합 후 삭제 대상]
- **data/**: 생성된 .pkl 파일 저장소 (벡터 DB 대용) [통합 후 삭제 대상]
- **main.py**: 추천 실행 엔트리포인트 (Serving)

## 실행 방법

1. 데이터 생성 (최초 1회)
   `python scripts/generate_data.py`

2. 추천 실행
   `python main.py`
