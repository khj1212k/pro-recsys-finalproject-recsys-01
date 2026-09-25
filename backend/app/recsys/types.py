from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set

import numpy as np

# X-Rec-Source 값. realtime/cold_start_*는 요청 시점 파이프라인이 만든 결과이고,
# batch/popular/recent는 폴백 체인(또는 RECSYS_MODE=batch)이 만든 결과다.
SOURCE_REALTIME = "realtime"
SOURCE_COLD_ONBOARDING = "cold_start_onboarding"
SOURCE_COLD_CATEGORY = "cold_start_category"
SOURCE_COLD_POPULAR = "cold_start_popular"
SOURCE_BATCH = "batch"
SOURCE_POPULAR = "popular"
SOURCE_RECENT = "recent"
SOURCE_EMPTY = "empty"


@dataclass(frozen=True)
class NewsletterMeta:
    """compute_scores(backend/scheduler/calculate_ranking.py)가 요구하는 속성 이름 그대로."""

    news_letter_id: int
    news_letter_created_at: datetime
    raw_news_count: int
    news_letter_title: str = ""


@dataclass(frozen=True)
class Item:
    """스코어링에 필요한 뉴스레터 속성. 생성 후 바뀌지 않으므로 프로세스 내 캐시 대상."""

    news_letter_id: int
    embedding: np.ndarray
    created_at: datetime
    raw_news_count: int


@dataclass
class UserState:
    user_id: int
    long_term: Optional[np.ndarray] = None
    short_term: Optional[np.ndarray] = None
    category_ids: List[int] = field(default_factory=list)
    clicked_ids: Set[int] = field(default_factory=set)
    # long_term이 없을 때 콜드스타트 체인이 채우는 대체 프로필(온보딩 평균/카테고리 중심)
    profile: Optional[np.ndarray] = None
    profile_source: str = "none"

    @property
    def has_personal_signal(self) -> bool:
        return self.profile is not None or self.short_term is not None


@dataclass
class ScoreResult:
    scores: np.ndarray
    model_version: str


@dataclass
class Recommendation:
    request_id: str
    news_letter_ids: List[int]
    scores: List[Optional[float]]
    source: str
    model_version: str
    cache_hit: bool = False
    fallback_reason: Optional[str] = None

    def score_by_id(self) -> Dict[int, Optional[float]]:
        return dict(zip(self.news_letter_ids, self.scores))
