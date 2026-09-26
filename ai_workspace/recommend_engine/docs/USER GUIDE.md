# 📘 Recommendation Engine User Guide

> **Project:** Recommend Engine (LightGBM + MMR)
> **Version:** 0.6.1
> **Last Updated:** 2026-02-03

이 문서는 추천 엔진의 실행 순서, 설정 방법, 그리고 성능 평가 지표에 대한 가이드를 제공합니다.

---

## 1. 🚀 Quick Start (실행 순서)

본 프로젝트는 `uv` 패키지 매니저를 사용하여 의존성을 관리합니다.

### Step 1. 임베딩 데이터 생성 및 DB 적재
뉴스 텍스트(제목+내용+카테고리)를 BGE-M3 모델로 임베딩하여 DB에 저장합니다.
*(최초 1회 실행, 혹은 뉴스 데이터가 업데이트되었을 때 실행)*

```bash
uv run python scripts/generate_embeddings.py
```

### Step 2. 모델 학습 (Training)
DB에서 최근 28일치 로그를 가져와 학습을 수행합니다. 시간순으로 데이터를 정렬하여 과거 데이터로 학습하고 미래 데이터로 검증합니다.

```bash
uv run python main_lgbm.py --train
```
- **Output:** `checkpoints/lgbm_model.pkl` 생성

### Step 3. 추론 (Inference)
학습된 모델을 사용하여 유저에게 뉴스를 추천합니다.
- **Debug 모드:** DB의 마지막 로그 시점을 '현재'로 가정하고 CSV 파일로 저장.
- **Production 모드:** 실제 현재 시간을 기준으로 추천하고 DB에 적재.

```bash
uv run python main_lgbm.py --inference
```

### Step 4. 성능 평가 (Evaluation)
생성된 추천 결과(CSV)와 DB의 검증 데이터(Validation Set)를 비교하여 성능을 측정합니다.

```bash
uv run python scripts/evaluate_results.py
```

---

## 2. ⚙️ Configuration (`config.yaml`)

주요 설정 항목에 대한 설명입니다.

| 섹션 | 항목 | 설명 |
| :--- | :--- | :--- |
| **system** | `execution_env` | `debug` (과거 시점 재연, CSV 저장) 또는 `production` (현재 시간, DB 저장) |
| **data** | `window_days` | 학습 시 DB에서 로드할 로그의 기간 (기본 28일, Sliding Window 적용) |
| | `validation_ratio` | 학습/검증 데이터 분할 비율 (기본 0.2). 시간순으로 분할함. |
| **lightgbm** | `negative_sample_ratio` | Positive 샘플 1개당 생성할 Negative 샘플 개수 (기본 5) |
| **recommendation** | `top_k` | 최종 추천할 아이템 개수 (기본 20) |
| | `mmr_lambda` | 다양성 조절 파라미터 (1에 가까울수록 관련성 우선, 0에 가까울수록 다양성 우선) |

---

## 3. 📊 Performance Metrics (성능 지표)

`scripts/evaluate_results.py`를 통해 계산된 지표의 의미입니다.

### 출력 형식 (값은 예시가 아니라 자리표시자)
```json
{
  "mrr": "<float>",
  "precision@5": "<float>",
  "ndcg@5": "<float>",
  "coverage@5": "<float>"
}
```

> 팀 시절(2026-02) 이 자리에 적혀 있던 벤치마크 수치는 철회했다. 두 가지 이유다.
> (1) 평가 로그가 LLM 페르소나로 만든 합성 클릭이라 절대 성능을 말할 수 없다.
> (2) 정답 기간을 `NOW()-6일`로 잡았는데 로그 전체가 몇 시간 분량이라 학습 구간이 정답에 섞였다(추론 시점 누출).
> 재현·분석은 평가 브랜치 병합 후 `reports/recsys/`에 싣는다. 이 스크립트의 출력은 같은 데이터 위에서 코드 변경의 상대 효과를 비교하는 용도로만 쓴다.

### 지표 상세 설명
1.  **MRR (Mean Reciprocal Rank):**
    * 추천 리스트 중 **"가장 먼저 나온 정답 아이템"**의 순위를 역수로 계산한 평균.
    * 예: 첫 정답이 평균적으로 1~2위에 있으면 0.5~1.0 사이 값이 나온다.
2.  **Precision@K:**
    * 상위 K개 추천 중 실제 유저가 클릭한(관심 있는) 아이템의 비율.
    * 예: Precision@5 = 0.4는 상위 5개 중 평균 2개가 클릭한 아이템이라는 뜻이다.
3.  **Recall@K:**
    * 유저가 클릭한 **전체** 뉴스 중 추천 시스템이 찾아낸 비율.
    * *Note:* 현재 검증셋 기간(약 6일)이 길기 때문에, 유저가 클릭한 뉴스가 매우 많아 Recall 수치는 상대적으로 낮게 나옵니다 (정상).
4.  **nDCG (Normalized Discounted Cumulative Gain):**
    * 순위의 가중치를 고려한 점수. 상위권에 정답을 맞출수록 점수가 높습니다.
5.  **Coverage:**
    * 추천된 뉴스들이 전체 카테고리 중 몇 %를 커버하는지 나타내는 다양성 지표.

---

## 4. 🛠️ Implementation Considerations (구현 고려사항)

### 1) Time Handling (시간 처리)
* **학습(Train):** `FeatureEngineer`가 `_timestamp` 임시 컬럼을 생성하여 반환하면, `main_lgbm.py`에서 이를 기준으로 엄격하게 정렬 후 데이터를 자릅니다. 학습 직전에는 해당 컬럼을 삭제하여 Data Leakage를 방지합니다.
* **추론(Inference):**
    * **Debug:** DB의 `MAX(created_at)`을 조회하여 '가상 현재 시간'으로 설정합니다. (과거 데이터 재연)
    * **Production:** 서버의 `datetime.now()`를 사용합니다.

### 2) Data Loading Strategy
* **참조 무결성 (News):** 뉴스 메타데이터와 임베딩은 과거 로그와의 매칭을 위해 **전체 데이터**를 로드합니다.
* **속도 최적화 (Logs):** 클릭 로그는 데이터량이 많으므로 `config['data']['window_days']` (28일) 만큼만 **Sliding Window** 방식으로 로드합니다.

### 3) User Embedding
* 별도의 모델 학습 없이, 유저가 과거에 읽은 뉴스들의 임베딩을 **Time-Decayed Weighted Average(시간 감쇠 가중 평균)**하여 유저 벡터를 생성합니다.
* 최신 클릭일수록 가중치가 높아 유저의 현재 관심사를 반영합니다.