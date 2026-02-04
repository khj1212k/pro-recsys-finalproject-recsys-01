# 📰 Personalize Newsletter RecSys

사용자별 맞춤형 뉴스레터를 제공하기 위한 **LightGBM + MMR 기반의 하이브리드 추천 시스템**입니다.
적은 데이터(Small Data) 환경에서도 높은 성능을 내기 위해 피처 엔지니어링 기반의 Ranking 모델과 다양성 확보를 위한 Reranking 기술을 결합했습니다.

## 🚀 Quick Start

### 1. 환경 설정
Python 3.10 이상의 환경에서 의존성을 설치합니다.
```bash
# 가상환경 생성 및 활성화
uv venv .venv
source .venv/bin/activate

# 패키지 설치
uv pip install -r requirements.txt
```

### 2. 모델 학습 (Train)
DB 로그를 기반으로 클릭 확률 예측 모델(LightGBM)을 학습합니다.
```bash
uv run python main_lgbm.py --train
```
- **Output:** `checkpoints/lgbm_model.pkl`

### 3. 추천 생성 (Inference)
전체 유저에 대해 개인화된 Top-20 뉴스레터 목록을 생성합니다.
- **Production Mode:** 생성된 결과를 DB (`news_letter_today_batch`)에 자동 적재합니다.
- **Debug Mode:** 결과를 `results/` 디렉토리에 CSV로 저장합니다. `results/rec_YYYYMMDD_HHmmss.csv`
```bash
uv run python main_lgbm.py --inference
```

### 4. 유틸리티 실행
임베딩 생성, 통계 집계, 모델 평가 등 다양한 보조 작업은 `scripts/` 내의 스크립트를 사용합니다.
자세한 내용은 [scripts/README.md](scripts/README.md)를 참고하세요.

---

## 📂 디렉토리 구조

```bash
recommend_engine/
├── config/              # 설정 파일 (DB 연결, 모델 파라미터)
├── src/
│   ├── data/            # 데이터 로드 및 전처리
│   ├── features/        # 피처 엔지니어링 (핵심 로직)
│   ├── models/          # LightGBM 모델 래퍼
│   ├── core/            # 추천(Inference) 및 평가(Evaluation) 로직
│   └── utils/           # 로깅 및 공통 유틸리티
├── scripts/             # 데이터 생성, 통계, 평가 등 유틸리티
├── main_lgbm.py         # 메인 실행 파일
└── README.md
```

## 📊 주요 기능 요약
1. **정교한 랭킹 (Ranking):** 최신성, 관심사, 임베딩 유사도를 반영한 클릭 확률 예측.
2. **다양성 확보 (Reranking):** MMR 알고리즘을 통해 특정 카테고리 편중 현상 방지.
3. **Cold Start 대응:** 신규 유저를 위한 나이대별 인기 뉴스 통계 제공.
4. **자동 평가:** 검증 데이터 존재 시 MRR, nDCG 지표 자동 산출.

---
**Developer:** Boostcamp AI Tech 8 - RecSys 01 Team