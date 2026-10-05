"""인기도-최신성 점수 (순수 함수, DB·ORM 비의존).

배치 인기 랭킹(backend/scheduler/calculate_ranking.py)과 요청 시점 추천의 인기 후보·
폴백이 같은 식을 쓰도록 한 곳에 둔다. 원래 scheduler/calculate_ranking.py 안에 있었는데,
그 모듈은 임포트 시점에 DB 엔진과 ORM 모델을 끌어오고 API 이미지에는 들어가지 않아
(docker/api.Dockerfile은 backend/app만 복사) app 패키지 안으로 옮겼다.

여기서 "인기"는 클릭 수가 아니라 뉴스레터에 묶인 기사 수(raw_news_count, 클러스터 크기)다.
"""
import math
from datetime import datetime, timezone
from typing import Dict, List


def compute_scores(newsletters, cutoff_utc: datetime) -> List[Dict]:
    """뉴스레터 리스트에 인기도-최신성 점수를 매겨 내림차순 정렬해 반환한다.

    Score = exp(-age_hours/48) + log1p(raw_news_count)/5

    newsletters의 각 원소는 news_letter_id / news_letter_created_at / raw_news_count /
    news_letter_title 속성을 가진 객체다(ORM 행 또는 app.recsys.types.NewsletterMeta).
    """
    scored_newsletters = []

    for nl in newsletters:
        created_at = nl.news_letter_created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        age_delta = cutoff_utc - created_at
        age_hours = age_delta.total_seconds() / 3600.0

        if age_hours < 0:
            age_hours = 0

        popularity = nl.raw_news_count

        score = math.exp(-age_hours / 48.0) + math.log1p(popularity) / 5.0

        scored_newsletters.append({
            "id": nl.news_letter_id,
            "score": score,
            "title": nl.news_letter_title,
            "age": age_hours,
            "pop": popularity
        })

    scored_newsletters.sort(key=lambda x: x["score"], reverse=True)
    return scored_newsletters
