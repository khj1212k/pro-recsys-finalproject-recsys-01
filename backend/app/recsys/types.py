from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from recsys_core.profile import HistState
from recsys_core.serving import ClickEvent, WindowCounts

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
    # 대표 카테고리(매핑된 카테고리 ID 중 가장 작은 것). 화면에 내보낼 수 있는 아이템은 항상 값이 있다.
    category_id: Optional[int] = None


@dataclass(frozen=True)
class ProfileState:
    """user_profile_state 한 행: 클릭마다 갱신되는 장기 프로필의 증분 상태(ADR 0033).

    last_event_at은 반영된 클릭 중 가장 늦은 것의 시각(마이크로초)이다. 요청 시각보다 늦으면 이 상태는
    그 요청의 피처 입력으로 쓸 수 없다(recsys_core.serving.check_inputs)."""

    hist: HistState = field(default_factory=HistState)
    last_event_at: Optional[datetime] = None


@dataclass
class UserState:
    user_id: int
    # 장기 벡터 = 증분 상태의 방향(반감기 7일 감쇠 합). 클릭 이력이 없으면 None이다.
    long_term: Optional[np.ndarray] = None
    # 단기 벡터 = 최근 24시간·최근 20클릭의 단위 벡터 합(ADR 0017)
    short_term: Optional[np.ndarray] = None
    category_ids: List[int] = field(default_factory=list)
    clicked_ids: Set[int] = field(default_factory=set)
    # long_term이 없을 때 콜드스타트 체인이 채우는 대체 프로필(온보딩 평균/카테고리 중심)
    profile: Optional[np.ndarray] = None
    profile_source: str = "none"
    # --- recsys_core 서빙 어댑터의 입력(ADR 0033). 휴리스틱 스코어러는 읽지 않는다.
    hist: Optional[HistState] = None
    hist_last_event_at: Optional[datetime] = None
    recent_clicks: List[ClickEvent] = field(default_factory=list)
    # 후보 아이템의 인기도 창 집계. None은 "아직 읽지 않음"이다(어댑터가 값을 내지 않는다).
    popularity: Optional[Dict[int, WindowCounts]] = None

    @property
    def has_personal_signal(self) -> bool:
        return self.profile is not None or self.short_term is not None


@dataclass
class ScoreResult:
    scores: np.ndarray
    model_version: str
    # shadow 모델 버전 -> 같은 아이템 순서의 점수. 응답 순서에는 쓰이지 않고 로그에만 남는다(ADR 0025).
    extra_scores: Dict[str, np.ndarray] = field(default_factory=dict)
    # 로그에 남길 피처 (아이템 수, 피처 수) float32와 그 해석 버전(scoring.FEATURE_SCHEMAS).
    features: Optional[np.ndarray] = None
    feature_schema_version: Optional[int] = None
    # 요청 경로 밖으로 넘긴 shadow·피처 작업의 손잡이(app.recsys.shadow.DeferredScores). 없으면 None.
    deferred: Optional[Any] = None


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
    # 이 목록(과 피처)을 계산한 요청 시각. 캐시에서 꺼내 쓴 요청도 로그에 이 값을 적는다(ADR 0033).
    computed_at: Optional[datetime] = None
    # 요청 경로 밖에서 계산 중이거나 끝난 shadow 점수·피처. 로그를 쓸 때 찾아간다.
    deferred: Optional[Any] = None

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
    # 이 칸의 아이템이 후보 배열(DeterministicList.eligible_ids)에서 놓인 자리. 요청 경로 밖에서 계산한
    # 값(Recommendation.deferred)에서 이 칸의 행을 찾는 데 쓴다.
    row: Optional[int] = None


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
    features_as_of: Optional[datetime] = None
    deferred: Optional[Any] = None

    def score_by_id(self) -> Dict[int, Optional[float]]:
        return dict(zip(self.news_letter_ids, self.scores))
