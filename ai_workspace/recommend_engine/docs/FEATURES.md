# 📊 LightGBM Recommendation Features

뉴스레터 추천 모델(LightGBM Ranker)에서 사용하는 Feature들의 상세 설명입니다.

## 1. 기본 정보 (ID)
| Feature Name | Description |
| :--- | :--- |
| `user_id` | 사용자 고유 ID |
| `news_id` | 뉴스레터 고유 ID |
| `score` | 모델이 예측한 **클릭 확률 (0~1)**. 높을수록 추천 순위가 높음. |

## 2. 시간/최신성 (Recency Features)
신규 뉴스에 가산점을 부여하고, 오래된 뉴스를 감점하기 위한 피처들입니다.

| Feature Name | Type | Description |
| :--- | :--- | :--- |
| `hours_since_published` | Float | 뉴스 발행 시점부터 현재까지 경과한 시간 (단위: 시간) |
| `is_fresh_24h` | Binary | 발행된 지 **24시간 이내**인가? (1: Yes, 0: No) |
| `is_fresh_7d` | Binary | 발행된 지 **7일 이내**인가? (1: Yes, 0: No) |

## 3. 사용자-뉴스 관련성 (Relevance/Similarity)
사용자의 과거 기록과 현재 후보 뉴스가 얼마나 유사한지 측정합니다.

| Feature Name | Type | Description |
| :--- | :--- | :--- |
| `history_cosine_similarity` | Float | **(User History Embedding) vs (News Embedding)** 간의 코사인 유사도.<br>사용자가 과거에 읽은 뉴스들의 평균 벡터와 현재 뉴스가 얼마나 비슷한지를 나타내는 **핵심 피처**. |
| `category_match_count` | Int | 사용자가 선호하는 카테고리 목록에 현재 뉴스의 카테고리가 몇 개나 포함되는지 개수. |
| `is_category_match` | Binary | 선호 카테고리와 하나라도 일치하면 1, 아니면 0. |

## 4. 콘텐츠 속성 (Content Features)
| Feature Name | Type | Description |
| :--- | :--- | :--- |
| `news_category_repr` | Int | 뉴스의 대표 카테고리 ID (Multi-hot 카테고리 중 첫 번째 값을 사용). |

## 5. 사용자 속성 (User Demographics)
사용자 그룹별 특성을 반영하기 위한 피처입니다.

| Feature Name | Type | Description |
| :--- | :--- | :--- |
| `user_age_band` | Int | 사용자 연령대 (0: ~18, 1: 19~24, ... 5: 65+). |
| `user_gender` | Int | 사용자 성별 (0: Unknown, 1: Male, 2: Female). |
| `user_onboarding_cnt` | Int | 사용자가 온보딩 시 선택한 선호 카테고리의 총 개수. (헤비 유저인지 판별 가능) |