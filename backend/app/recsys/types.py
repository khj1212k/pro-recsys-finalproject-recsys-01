from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

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
    """compute_scores(app/recsys/popularity.py)가 요구하는 속성 이름 그대로."""

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
    # shadow 모델 버전 -> 같은 아이템 순서의 점수. 응답 순서에는 쓰이지 않고 로그에만 남는다(ADR 0025).
    extra_scores: Dict[str, np.ndarray] = field(default_factory=dict)
    # 활성 스코어러가 쓴 피처 (아이템 수, 피처 수) float32와 그 해석 버전(scoring.FEATURE_SCHEMAS).
    features: Optional[np.ndarray] = None
    feature_schema_version: Optional[int] = None


@dataclass
class DeterministicList:
    """한 사용자 상태에서 요청과 무관하게 정해지는 부분. 결과 캐시에 들어가는 것은 이것뿐이고,
    탐색 칸은 캐시에서 꺼낸 뒤 요청마다 새로 뽑는다(ADR 0025).

    scores / extra_scores / features는 eligible_ids와 같은 순서다 - 탐색으로 뽑힌 아이템의 점수와
    피처도 로그에 남기려면 화면에 들지 못한 후보의 값까지 들고 있어야 한다. 캐시 항목이 후보 수에
    비례해 커지므로 전부 numpy 배열로 든다(파이썬 int 리스트·dict로 들면 항목이 약 3배 크다)."""

    ranked_ids: List[int]  # 결정론 순위(최대 top_k개)
    eligible_ids: np.ndarray  # E: 제외와 표시 가능 필터를 거친 후보 전체 (int32)
    scores: np.ndarray
    source: str
    model_version: str
    profile_source: str
    candidate_count: int  # 제외 전 후보 합집합의 크기
    cold: bool = False  # 개인 신호가 전혀 없는 사용자(탐색 칸 수가 다르다)
    extra_scores: Dict[str, np.ndarray] = field(default_factory=dict)
    features: Optional[np.ndarray] = None
    feature_schema_version: Optional[int] = None
    fatigue_mode: str = "off"
    fatigued_count: Optional[int] = None

    def __post_init__(self):
        self.eligible_ids = np.asarray(self.eligible_ids, dtype=np.int32)


@dataclass(frozen=True)
class SlotInfo:
    """화면 한 칸의 로그 정보. Recommendation.news_letter_ids와 같은 순서로 놓인다."""

    explored: bool = False
    propensity: Optional[float] = None
    det_rank: Optional[int] = None
    scores_shadow: Optional[Dict[str, Optional[float]]] = None
    features: Optional[np.ndarray] = None


POLICY_NONE = "none"  # 폴백 응답: 탐색 정책이 만든 화면이 아니다


@dataclass
class Recommendation:
    request_id: str
    news_letter_ids: List[int]
    scores: List[Optional[float]]
    source: str
    model_version: str
    cache_hit: bool = False
    fallback_reason: Optional[str] = None
    # --- 로그 v2(ADR 0025). 폴백 응답은 아래가 전부 기본값이다.
    slots: List[SlotInfo] = field(default_factory=list)
    policy_version: str = POLICY_NONE
    explore_positions: Optional[Tuple[int, ...]] = None
    explore_pool_size: Optional[int] = None
    eligible_ids: Optional[List[int]] = None
    candidate_count: Optional[int] = None
    profile_source: Optional[str] = None
    shadow_versions: List[str] = field(default_factory=list)
    feature_schema_version: Optional[int] = None
    fatigue_mode: Optional[str] = None
    fatigued_count: Optional[int] = None
    latency_ms: Optional[int] = None

    def score_by_id(self) -> Dict[int, Optional[float]]:
        return dict(zip(self.news_letter_ids, self.scores))
