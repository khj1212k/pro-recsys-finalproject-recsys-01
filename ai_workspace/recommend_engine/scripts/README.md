# Utility Scripts

이 디렉토리는 추천 시스템의 데이터 전처리, 통계 생성, 성능 평가를 위한 유틸리티 스크립트들을 포함하고 있습니다.

## 1. `generate_embeddings.py` (임베딩 생성)
뉴스 기사의 제목, 카테고리, 본문을 결합하고 **BGE-M3 모델**을 사용하여 임베딩 벡터를 생성합니다. 생성된 벡터는 DB의 `news_letter_embedding` 컬럼에 저장됩니다.

- **실행 명령:**
  ```bash
  python scripts/generate_embeddings.py
  ```
- **주요 기능:**
  - DB에서 뉴스 데이터 로드 (`news_letter` 테이블)
  - 텍스트 결합 (Title + Category + Content)
  - Batch 단위 임베딩 생성 및 DB 업데이트

## 2. `generate_daily_stats.py` (통계 집계)
개인화 추천이 불가능한 **신규 유저(Cold Start)**를 위해, 최근 클릭수 기반의 인기 뉴스 리스트를 집계합니다.

- **실행 명령:**
  ```bash
  python scripts/generate_daily_stats.py
  ```
- **결과물:** `results/daily_stats.json`
- **주요 내용:**
  - **Global Top-K**: 전체 유저 대상 가장 많이 클릭된 뉴스
  - **Age Group Top-K**: 연령대(20대, 30대 등)별 인기 뉴스

## 3. `evaluate_results.py` (모델 평가)
`main_lgbm.py` 실행 결과로 생성된 추천 리스트(CSV)와 실제 유저 로그(DB)를 비교하여 정량적 성능 지표를 계산합니다.

- **실행 명령:**
  ```bash
  python scripts/evaluate_results.py
  ```
- **평가 지표:**
  - **Precision@K**: 추천된 상위 K개 중 실제 클릭한 비율
  - **Recall@K**: 실제 클릭한 아이템 중 추천된 아이템 비율
  - **nDCG@K**: 순위 가중치를 고려한 성능 지표
  - **MRR**: 첫 번째 정답 아이템의 순위 역수
  - **Coverage**: 추천된 아이템들의 카테고리 다양성