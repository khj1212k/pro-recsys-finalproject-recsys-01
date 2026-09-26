# 📊 LightGBM Recommendation Features (v0.6.1)

뉴스레터 추천 모델(LightGBM Ranker)에서 사용하는 Feature들의 상세 명세입니다.

## 1. 기본 정보 & 타겟
| Feature Name | Description |
| :--- | :--- |
| `user_id` | 사용자 고유 ID |
| `news_id` | 뉴스레터 고유 ID |
| `label` | (Train Only) 1: 클릭함, 0: 클릭 안 함 (Negative Sample) |
| `_timestamp` | (Internal) 시간순 분할(Split)을 위해 임시 생성되는 컬럼. **학습/추론 직전 삭제됨.** |

## 2. 시간/최신성 (Recency Features)
시스템의 현재 시간(Real or Virtual)을 기준으로 계산됩니다.

| Feature Name | Type | Description |
| :--- | :--- | :--- |
| `hours_since_published` | Float | `기준 시간 - 뉴스 발행 시간`. 값이 작을수록 최신 뉴스. |
| `is_fresh_24h` | Binary | 발행된 지 **24시간 이내**인가? (1: Yes, 0: No) |
| `is_fresh_7d` | Binary | 발행된 지 **7일 이내**인가? (1: Yes, 0: No) |

## 3. 사용자-뉴스 관련성 (Relevance/Similarity)
**Implicit Feedback**과 **Explicit Feedback**을 모두 반영합니다.

| Feature Name | Type | Description |
| :--- | :--- | :--- |
| `history_cosine_similarity` | Float | **(Implicit)** 유저의 과거 열람 기록 벡터(Time-Decayed Average)와 뉴스 벡터 간의 유사도. <br>유저의 최신 취향 반영도가 높음. |
| `category_match_count` | Int | **(Explicit)** 유저가 직접 선택한 관심 카테고리와 뉴스의 카테고리 교집합 개수. |
| `is_category_match` | Binary | 관심 카테고리와 하나라도 일치하면 1. |

## 4. 콘텐츠 속성 (Content Features)
| Feature Name | Type | Description |
| :--- | :--- | :--- |
| `news_category_repr` | Int | 뉴스의 대표 카테고리 ID. (모델이 카테고리별 선호도를 학습하도록 유도) |

## 5. 사용자 속성 (User Attributes)
| Feature Name | Type | Description |
| :--- | :--- | :--- |
| `user_onboarding_cnt` | Int | 유저가 온보딩 시 선택한 카테고리 개수. 유저의 관심사 폭(Width)을 나타냄. |

> 팀 시절 문서에는 `user_age_band`/`user_gender`가 있었으나, 로더가 DB에서 읽지 않고 항상 0으로 채우던 상수 열이라 2026-09-26에 제거했다(ADR 0003 한계 절). 모델 입력 열 목록은 `tests/recommend_engine/test_feature_columns_contract.py`가 고정한다.
