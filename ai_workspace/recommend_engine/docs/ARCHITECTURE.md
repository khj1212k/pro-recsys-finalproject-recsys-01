# 🏗️ System Architecture & Logic Detail

> **버전:** 0.6.1
> **작성일:** 2026-02-03
> **기술 스택:** Python, LightGBM, PostgreSQL (pgvector), NumPy, Pandas

이 문서는 뉴스레터 추천 시스템의 내부 동작 원리, 데이터 파이프라인, 그리고 모델링 전략을 상세히 기술합니다.

---

## 1. 아키텍처 오버뷰 (The Big Picture)

우리의 시스템은 **2-Stage Recommendation Pipeline**을 따릅니다.
LightGBM을 이용한 정교한 랭킹 후, MMR 알고리즘으로 다양성을 확보하여 재배열하는 구조입니다.

```mermaid
graph TD
    subgraph "Data Layer (PostgreSQL)"
        Logs[(User Activity Logs)] -->|Sliding Window (28d)| DataLoader
        News[(Newsletter Metadata)] -->|Full Load| DataLoader
        Emb[("pgvector Embeddings")] -->|BGE-M3 Vectors| DataLoader
    end

    subgraph "Preprocessing Layer"
        DataLoader -->|Raw Data| FeatureEng[Feature Engineer]
        FeatureEng -->|Time-Decayed User Vector| UserProfile
        FeatureEng -->|Feature Generation| LGBM_Input
    end

    subgraph "Stage 1: Ranking (LightGBM)"
        LGBM_Input --> Ranker[LightGBM Ranker]
        Ranker -->|Predict Score (CTR)| Scored_List
    end

    subgraph "Stage 2: Reranking (MMR)"
        Scored_List --> Reranker[MMR Reranker]
        Reranker -->|Diversity Filtering (Adaptive Lambda)| Final_Top20
    end
    
    subgraph "Output Layer"
        Final_Top20 -->|Insert| DB_Batch_Table[(news_letter_today_batch)]
    end
```

---

## 2. 핵심 전략: Why LightGBM with DB-Centric?

### 2.1 Modeling Strategy
**"Feature Engineering 기반의 GBDT(LightGBM)"** 방식을 채택했습니다.
- **Small Data 대응:** User 100명 / Daily News 200건 환경에서 딥러닝보다 과적합 위험이 적음.
- **Interpretability:** 피처 중요도 분석을 통해 추천 이유를 설명 가능.

### 2.2 Data Strategy
- **DB-First:** 모든 데이터(임베딩 포함)는 PostgreSQL에서 관리하며, `pgvector`를 활용합니다.
- **Strict Time Splitting:** 학습 시 `_timestamp`를 기준으로 Train/Valid를 칼같이 나누어 미래 정보가 학습에 새어 나가는(Data Leakage) 것을 원천 차단했습니다.

---

## 3. Feature Engineering 상세

모델의 성능을 좌우하는 핵심 피처들입니다. (`src/features/feature_engineer.py`)

| 카테고리 | 피처 이름 | 데이터 타입 | 설명 및 의도 |
| :--- | :--- | :--- | :--- |
| **최신성**<br>(Recency) | `hours_since_published` | Float | 기준 시간(가상/실제 현재) 대비 뉴스 발행 경과 시간. **최신 뉴스일수록 높은 점수**를 받도록 유도. |
| | `is_fresh_24h` | Binary | 24시간 이내 발행 여부. |
| **유사도**<br>(Similarity) | `history_cosine_similarity` | Float | **(Core)** 유저 히스토리 벡터와 뉴스 벡터 간의 **Cosine Similarity**. 유저 벡터는 시간 감쇠(Time Decay)가 적용된 평균 벡터임. |
| **관심사**<br>(Explicit) | `category_match_count` | Int | 유저가 가입 시 선택한 관심 카테고리와 뉴스의 카테고리 일치 개수. |
| **유저 속성** | `user_onboarding_cnt` | Int | 유저가 선택한 카테고리 총 개수 (헤비/라이트 유저 구분). |

---

## 4. Stage 1: Candidate Generation (Ranking)

LightGBM을 사용하여 (User, News) 쌍에 대해 **클릭 확률(CTR)**을 예측합니다.

- **Objective:** Binary Classification
- **Negative Sampling:**
    - Positive(클릭) : Negative(랜덤 추출 미클릭) = **1 : 5**
    - **Point-in-Time Correctness:** Negative 샘플 생성 시, Positive 샘플이 발생한 그 시점(`timestamp`)을 그대로 복사하여 할당. (당시의 맥락 유지)

---

## 5. Stage 2: Diversity Reranking (MMR)

**Filter Bubble** 방지를 위해 MMR(Maximal Marginal Relevance)을 적용합니다.

### 5.1 Adaptive Lambda (적응형 파라미터)
유저가 선택한 관심 카테고리 수(`num_preferred_categories`)에 따라 $\lambda$(다양성 조절 계수)를 동적으로 변경합니다.

- **관심사 좁음 (<=2개):** $\lambda = 0.8$ (관련성 위주 추천)
- **관심사 보통 (3~4개):** $\lambda = 0.7$
- **관심사 넓음 (>=5개):** $\lambda = 0.6$ (다양성 위주 추천)

---

## 6. Cold Start & Fallback

**User Cold Start (로그 없음):**
    - `DailyStatsAggregator` (`src/statistics`)를 이용해 통계 기반 추천 제공.
    - Global Top-K (전체 인기) 및 Age Group Top-K (연령별 인기) 뉴스 혼합.

**Item Cold Start (신규 뉴스):**
    - 로그가 없어도 `is_fresh` 및 `category_match` 피처를 통해 추천 가능.
    - 시스템적으로 최신 뉴스에 가산점이 부여됨.