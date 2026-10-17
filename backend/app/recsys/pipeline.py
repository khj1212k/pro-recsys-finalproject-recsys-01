"""요청 시점 추천 파이프라인: 사용자 상태 -> 후보 합집합 -> 스코어링 -> MMR (ADR 0015).

여기서 만드는 것은 결정론 목록(DeterministicList)까지다. 탐색 칸을 넣어 한 요청의 화면으로 만드는 일
(build_recommendation)은 결과 캐시 뒤에서 요청마다 한다(ADR 0025).
"""
import logging
import math
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.recsys.cache import TTLCache
from app.recsys.config import RecsysConfig
from app.recsys.deadline import BudgetExceeded, Deadline  # noqa: F401 (기존 임포트 경로 유지)
from app.recsys.exploration import plan_slate
from app.recsys.metrics import RecsysCounters
from app.recsys.repository import RecsysRepository
from app.recsys.scoring import PRIOR_SET, Scorer, ScorerStack, heuristic_of, make_heuristic, popularity_order
from app.recsys.throttle import ThrottledExceptionLog
from recsys_core import round_robin_union
from recsys_core import serving as core_serving
from recsys_core.profile import unit_rows

from app.recsys.types import (
    SOURCE_COLD_CATEGORY,
    SOURCE_COLD_ONBOARDING,
    SOURCE_COLD_POPULAR,
    SOURCE_REALTIME,
    DeterministicList,
    Item,
    Recommendation,
    SlotInfo,
    UserState,
    WindowCounts,
)

logger = logging.getLogger(__name__)
from app.recsys.popularity import compute_scores
# 성승우님이 recommend_engine(배치 LightGBM 경로)에 구현한 MMR을 그대로 쓴다 -
# 요청 시점 경로와 배치 경로가 같은 다양성 규칙(선호 카테고리 수별 λ)을 공유하도록.
# src.core는 numpy만 쓰는 패키지라 API 이미지에는 이 디렉터리만 복사한다
# (docker/api.Dockerfile). 저장소에서 직접 띄울 때는
# `pip install --no-deps -e ai_workspace/recommend_engine`이 필요하다.
from src.core.reranker import CategoryBasedMMRReranker

# 폴백 체인의 popular 단계(와 되돌리기 세트의 콜드 경로)가 쓰는 compute_scores 순서의 이름(ADR 0014 대안 8).
POPULARITY_MODEL_VERSION = "popularity-v1"
# 후보의 6시간 클릭 수는 어댑터의 첫 인기도 창(pop_clicks_6h)과 같은 창으로 센다(ADR 0014 사전 등록 2).
CLICK_WINDOW_H = 6
assert float(core_serving.SERVING_FEATURE_CONFIG.pop_windows_h[0]) == CLICK_WINDOW_H, "어댑터의 첫 인기도 창이 6시간이 아니다"


class EmptyRecommendation(Exception):
    pass


@dataclass
class CandidateSet:
    ids: List[int]
    by_source: Dict[str, List[int]]
    contributed: Dict[str, int] = field(default_factory=dict)


def _unit_sum(clicks) -> Optional[np.ndarray]:
    """클릭한 뉴스레터의 단위 벡터 합(방향만 쓴다). 어댑터의 단기 벡터와 같은 방향이다."""
    if not clicks:
        return None
    return unit_rows(np.stack([c.embedding for c in clicks])).sum(axis=0)


def build_user_state(
    repo: RecsysRepository, user_id: int, now: datetime, cfg: RecsysConfig
) -> UserState:
    """요청 시각 now의 사용자 상태. 클릭 이력에서 오는 것은 now보다 엄격히 이전의 클릭만이다.

    장기 벡터는 클릭마다 갱신해 둔 증분 상태의 방향이고(읽을 때 감쇠를 다시 계산하지 않는다), 단기 벡터는 피처
    어댑터가 읽는 것과 같은 최근 클릭(요청 초 기준 24시간 안의 최근 20개, (초, 뉴스레터 ID) 순)의 단위 벡터 합이다.
    휴리스틱·knn_short 후보·어댑터가 이 한 벌의 정의를 같이 쓴다(ADR 0014 사전 등록 2).

    콜드스타트 임계(cfg.min_personal_events, ADR 0031 결정 1): 장기 상태에 반영된 클릭 수가 임계보다 적으면 장기·단기
    벡터를 프로필로 쓰지 않고 콜드 체인으로 간다. 어댑터의 입력(hist, recent_clicks)은 그대로 둔다(피처는 계산·로그)."""
    profile_state, category_ids = repo.profile_state(user_id)
    clicks = repo.recent_clicks(
        user_id, core_serving.short_window_start(now), now, core_serving.SHORT_MAX_EVENTS
    )
    # "최근 N개"의 순서는 어댑터가 정한 것 하나다((초, 뉴스레터 ID) 순 - recsys_core.serving.latest_events).
    recent = core_serving.latest_events(clicks)

    hist = profile_state.hist
    personal = hist.hist_len >= cfg.min_personal_events
    long_term = hist.direction() if personal else None
    state = UserState(
        user_id=user_id,
        long_term=long_term,
        short_term=_unit_sum(recent) if personal else None,
        category_ids=list(category_ids),
        hist=hist,
        hist_last_event_at=profile_state.last_event_at,
        recent_clicks=recent,
        below_min_personal_events=not personal and (hist.hist_len > 0 or bool(recent)),
    )
    # 콜드스타트 체인: 장기 벡터가 없으면 온보딩 선호 뉴스레터 평균 -> 선호 카테고리
    # 최근 뉴스레터 중심 순으로 대체 프로필을 만든다. 끝까지 없으면 인기 목록(추천기).
    if long_term is not None:
        state.profile, state.profile_source = long_term, "long_term"
        return state
    onboarding = repo.onboarding_vector(user_id)
    if onboarding is not None:
        state.profile, state.profile_source = onboarding, "onboarding"
        return state
    if category_ids:
        centroid = repo.category_centroid(
            category_ids, now - timedelta(hours=cfg.freshness_hours)
        )
        if centroid is not None:
            state.profile, state.profile_source = centroid, "category"
    return state


def load_popularity(repo: RecsysRepository, ids: Sequence[int], now: datetime) -> Dict[int, WindowCounts]:
    """후보 ids의 인기도 창 집계를 어댑터의 시각 규칙대로 읽는다(창의 끝은 요청보다 앞이다)."""
    return repo.item_window_counts(
        ids,
        core_serving.item_window_starts(now),
        core_serving.inview_window_start(now),
        core_serving.item_window_end(now),
    )


class ClickCounts:
    """한 요청 안에서 아이템의 최근 6시간 클릭 수를 아이템마다 한 번만 읽는다(ADR 0014 사전 등록 2·3·4).

    창은 어댑터의 pop_clicks_6h와 같다: [T - 2초 - 6시간, T - 2초), event = 'click', 모든 사용자. 후보 출처 popular가
    신선도 창 전체를 먼저 읽으므로, 스코어링 단계에서는 창 밖에서 온 후보(recent 출처)만 더 읽는다.

    읽기가 실패하면 그 요청에서는 더 읽지 않고 None을 돌려준다(popular 출처는 생성 시각 순서가 되고, 휴리스틱의 인기 항은
    모든 후보에서 0이 된다). 실패한 문장 뒤의 조회가 거부되지 않게 되돌린다(피로 규칙 조회와 같은 이유)."""

    def __init__(self, repo: RecsysRepository, now: datetime, on_error: Optional[Callable[[], None]] = None):
        self.repo = repo
        self.start = core_serving.item_window_starts(now)[0]
        self.end = core_serving.item_window_end(now)
        self.on_error = on_error
        self.failed = False
        self.reads = 0
        self._known: Dict[int, int] = {}

    def get(self, ids: Sequence[int]) -> Optional[Dict[int, int]]:
        if self.failed:
            return None
        wanted = [int(i) for i in ids]
        missing = [i for i in dict.fromkeys(wanted) if i not in self._known]
        if missing:
            try:
                # 노출 창을 비워(시작 = 끝) 클릭만 센다. SQL 구현은 빈 창의 노출 조회를 보내지 않는다.
                got = self.repo.item_window_counts(missing, (self.start,), self.end, self.end)
            except Exception:
                self.failed = True
                if self.on_error is not None:
                    self.on_error()
                self.repo.rollback()
                return None
            self.reads += 1
            for i in missing:
                wc = got.get(i)
                self._known[i] = 0 if wc is None else int(wc.clicks[0])
        return {i: self._known[i] for i in wanted}


def popular_ids(
    repo: RecsysRepository, since: datetime, now: datetime, n: int, clicks: Optional[ClickCounts] = None
) -> List[int]:
    """후보 출처 popular(ADR 0014 사전 등록 4): 신선도 창 안의 표시 가능한 뉴스레터를 최근 6시간 클릭 수 내림차순 ->
    생성 시각 내림차순 -> ID 내림차순으로 세운 상위 n개. E8이 판정한 구성의 popular(pop_clicks_6h 상위)와 같은 뜻이다."""
    metas = repo.window_meta(since)
    counts = (clicks or ClickCounts(repo, now)).get([m.news_letter_id for m in metas])
    return popularity_order(metas, counts)[:n]


def legacy_popular_ids(repo: RecsysRepository, since: datetime, now: datetime, n: int) -> List[int]:
    """compute_scores(최신성 + 묶인 기사 수, popularity-v1) 순서의 상위 n개. 폴백 체인의 popular 단계만 쓴다 - 실시간
    경로가 실패하거나 시간 예산을 넘긴 뒤의 최후 경로라 클릭 집계를 더하지 않는다(ADR 0014 대안 8)."""
    return [s["id"] for s in compute_scores(repo.window_meta(since), now)[:n]]


def _round_robin_union(sources: Dict[str, List[int]], cap: int) -> CandidateSet:
    merged, contributed = round_robin_union(sources, cap)
    return CandidateSet(ids=merged, by_source=sources, contributed=contributed)


def generate_candidates(
    repo: RecsysRepository,
    state: UserState,
    cfg: RecsysConfig,
    now: datetime,
    clicks: Optional[ClickCounts] = None,
    popular_by_clicks: Optional[bool] = None,
) -> CandidateSet:
    """출처·순서·k·창·상한은 전부 cfg.candidate_spec()에서 온다(recsys_core.SERVING_CANDIDATE_SPEC, ADR 0033).
    신호가 없는 출처(프로필 없음, 최근 클릭 없음, 선호 카테고리 없음)는 목록 자체를 내지 않는다.
    clicks는 그 요청의 6시간 클릭 수 조회(popular 출처와 휴리스틱이 같이 쓴다). 없으면 여기서 만든다.

    popular_by_clicks는 출처 popular의 순서다. 참이면 6시간 클릭 수(ADR 0014 사전 등록 4), 거짓이면 M5 전의
    compute_scores 순서(되돌리기 세트 prior - 요청 경로에 클릭 수 조회가 남지 않는다, ADR 0014 사전 등록 이후 변경 기록 1).
    None이면 cfg.heuristic_set을 따른다. 출처의 이름·k·창·상한은 어느 쪽이든 같다."""
    spec = cfg.candidate_spec()
    if popular_by_clicks is None:
        popular_by_clicks = cfg.heuristic_set != PRIOR_SET
    clicks = clicks or ClickCounts(repo, now)
    since = now - timedelta(hours=spec.window_h)
    producers = {
        "knn_profile": lambda k: None if state.profile is None else repo.knn_ids(state.profile, since, k),
        "knn_short": lambda k: None if state.short_term is None else repo.knn_ids(state.short_term, since, k),
        "recent": lambda k: repo.recent_ids(k),
        "popular": lambda k: (
            popular_ids(repo, since, now, k, clicks) if popular_by_clicks else legacy_popular_ids(repo, since, now, k)
        ),
        "category": lambda k: (
            repo.category_recent_ids(state.category_ids, since, k) if state.category_ids else None
        ),
    }
    sources: Dict[str, List[int]] = {}
    for name, k in spec.sources:
        ids = producers[name](k)
        if ids is not None:
            sources[name] = ids
    # 캡을 넘으면 한 생성기(예: KNN 100개)가 자리를 독식하지 않도록 순서를 번갈아 합친다.
    return _round_robin_union(sources, spec.cap)


def _finite_or_none(value) -> Optional[float]:
    value = float(value)
    return value if math.isfinite(value) else None


def shadow_scores_at(extra_scores: Dict[str, np.ndarray], row: int) -> Optional[Dict[str, Optional[float]]]:
    """후보 배열의 row번째 아이템에 대한 {shadow 모델 버전: 점수}. shadow가 없으면 None(SQL NULL로 남는다).
    JSON에는 NaN/inf가 없으므로 유한하지 않은 점수는 null이다."""
    return {v: _finite_or_none(arr[row]) for v, arr in extra_scores.items()} or None


def build_recommendation(
    det: DeterministicList,
    cfg: RecsysConfig,
    request_id: str,
    rng: Optional[np.random.Generator],
    cache_hit: bool = False,
) -> Recommendation:
    """결정론 목록에 이 요청의 탐색 칸을 넣어 화면을 만든다. rng가 없으면 결정론 목록 그대로다.

    결과 캐시에는 DeterministicList만 들어가므로, 캐시가 적중한 요청도 탐색 칸은 독립적으로 뽑힌다."""
    eligible: List[int] = det.eligible_ids.tolist()
    plan = plan_slate(det.ranked_ids, eligible, cfg.top_k, cfg.explore_slots_for(det.cold), rng)
    index = {nid: i for i, nid in enumerate(eligible)}
    rows = [index[slot.news_letter_id] for slot in plan.slots]
    slots = [
        SlotInfo(
            explored=slot.explored,
            propensity=slot.propensity,
            det_rank=slot.det_rank,
            scores_shadow=shadow_scores_at(det.extra_scores, i),
            features=None if det.features is None else det.features[i],
            row=i,
        )
        for slot, i in zip(plan.slots, rows)
    ]
    return Recommendation(
        request_id=request_id,
        news_letter_ids=plan.ids,
        scores=[float(det.scores[i]) for i in rows],
        source=det.source,
        model_version=det.model_version,
        cache_hit=cache_hit,
        slots=slots,
        policy_version=plan.policy,
        explore_positions=plan.explore_positions,
        explore_pool_size=plan.pool_size,
        eligible_ids=eligible,
        candidate_count=det.candidate_count,
        profile_source=det.profile_source,
        shadow_versions=sorted(det.extra_scores),
        feature_schema_version=det.feature_schema_version,
        fatigue_mode=det.fatigue_mode,
        fatigued_count=det.fatigued_count,
        features_as_of=det.computed_at,
        deferred=det.deferred,
    )


RepoFactory = Callable[[], AbstractContextManager]


class RealtimeRecommender:
    def __init__(
        self,
        cfg: RecsysConfig,
        scorer: Scorer,
        reranker: Optional[CategoryBasedMMRReranker] = None,
        item_cache: Optional[TTLCache] = None,
        counters: Optional[RecsysCounters] = None,
        feature_repo_factory: Optional[RepoFactory] = None,
        heuristic: Optional[Scorer] = None,
    ):
        self.cfg = cfg
        self.scorer = scorer
        # 요청 경로 밖에서 도는 피처 작업이 인기도 창 집계를 읽을 때 쓰는 저장소(자기 커넥션). 없으면 그 작업은
        # 인기도 입력을 읽지 못하고, 어댑터 피처는 남지 않는다.
        self.feature_repo_factory = feature_repo_factory
        self.counters = counters or RecsysCounters()
        # 스코어러 하나만 받으면 shadow 없는 묶음으로 감싼다. 묶음을 받으면 그대로 쓴다.
        self.stack = scorer if isinstance(scorer, ScorerStack) else ScorerStack(scorer, counters=self.counters)
        # 개인 신호가 없는 사용자의 순서를 내는 휴리스틱(ADR 0014 사전 등록 3). 활성 스코어러(또는 그 폴백)가 휴리스틱이면
        # 그것과 같은 것을 쓴다 - 콜드 경로와 개인화 경로의 활성 식이 갈리지 않게.
        self.heuristic = heuristic or heuristic_of(self.stack.active) or make_heuristic(cfg.heuristic_set)
        # 그 휴리스틱이 6시간 클릭 수를 쓰는가. 쓰지 않으면(되돌리기 세트 prior) 요청 경로가 그 조회를 하지 않는다 -
        # 콜드 경로, 후보 출처 popular(M5 전의 compute_scores 순서), 스코어링의 인기 항 모두(ADR 0014 사전 등록 이후
        # 변경 기록 1). 지연 때문에 되돌릴 때 새 조회가 남지 않게 하려는 것이다.
        self.uses_clicks = bool(getattr(self.heuristic, "uses_clicks", True))
        self.reranker = reranker or CategoryBasedMMRReranker()
        self.item_cache = item_cache or TTLCache(cfg.item_cache_ttl_s, cfg.item_cache_max_entries)
        self._errors = ThrottledExceptionLog(logger=logger)

    def _items(self, repo: RecsysRepository, ids: Sequence[int]) -> List[Item]:
        # 뉴스레터 임베딩/생성시각/기사 수는 생성 후 바뀌지 않고, 신선도 창 안의 후보는
        # 모든 사용자에게 거의 같다 - 벡터 전송·파싱 비용을 프로세스당 한 번만 치른다.
        cached = self.item_cache.get_many(ids)
        missing = [i for i in ids if i not in cached]
        if missing:
            fetched = repo.items(missing)
            for nid, item in fetched.items():
                self.item_cache.put(nid, item)
            cached.update(fetched)
        return [cached[i] for i in ids if i in cached]

    def _apply_fatigue(
        self, repo: RecsysRepository, user_id: int, ids: List[int], now: datetime
    ) -> Tuple[List[int], Optional[int]]:
        """노출 피로 규칙(ADR 0025). 돌려주는 것은 (남은 후보, 규칙에 걸린 수). off면 조회하지 않는다.

        log 모드는 세기만 하는 모드라 조회가 실패해도 요청을 실패시키지 않는다(센 뒤 rollback하고 진행).
        enforce 모드에서는 규칙이 정책의 일부이므로 실패가 그대로 올라가 폴백으로 간다."""
        mode = self.cfg.fatigue_mode
        if mode == "off" or not ids:
            return ids, None
        since = now - timedelta(hours=self.cfg.fatigue_hours)
        try:
            fatigued = repo.fatigued_among(user_id, ids, since, self.cfg.fatigue_min_impressions)
        except Exception:
            if mode == "enforce":
                raise
            # 조회가 계속 실패하면(예: 마이그레이션 전의 DB) 요청마다 같은 예외가 난다: traceback은 분당 한 번만.
            self._errors.exception("fatigue", "fatigue lookup failed in log mode; continuing without it")
            self.counters.inc("fatigue.lookup_error")
            # PostgreSQL은 실패한 문장 뒤의 문장을 전부 거부하므로 되돌려야 다음 조회가 된다. 되돌리면
            # 트랜잭션 로컬로 건 statement_timeout도 풀린다: 이 요청의 남은 조회는 요청 쪽 시간 예산만 지킨다.
            repo.rollback()
            return ids, None
        if fatigued:
            self.counters.inc("fatigue.requests_with_fatigued")
            self.counters.inc("fatigue.items", len(fatigued))
        if mode == "enforce":
            return [i for i in ids if i not in fatigued], len(fatigued)
        return ids, len(fatigued)

    def _click_counts(self, repo: RecsysRepository, now: datetime) -> ClickCounts:
        def failed() -> None:
            self._errors.exception("clicks_6h", "6h click count lookup failed; the popularity term is 0 for this request")
            self.counters.inc("popularity.clicks_error")

        return ClickCounts(repo, now, on_error=failed)

    def _cold_popular(self, repo, user_id, now, clicks: ClickCounts) -> DeterministicList:
        since = now - timedelta(hours=self.cfg.freshness_hours)
        metas = repo.window_meta(since)
        # 활성 휴리스틱의 비개인 항으로 정렬한다(ADR 0014 사전 등록 3). 되돌리기 세트(prior)는 compute_scores 순서이고
        # 클릭 수를 읽지 않는다.
        counts = clicks.get([m.news_letter_id for m in metas]) if self.uses_clicks and metas else None
        ranked = self.heuristic.cold_order(metas, counts, now)
        if not ranked:
            raise EmptyRecommendation("no newsletters in the freshness window")
        clicked = repo.clicked_among(user_id, [nid for nid, _ in ranked])
        # 후보 수 상한은 개인화 경로와 같다. 순서대로 상위 candidate_cap개가 E다.
        eligible = [(nid, score) for nid, score in ranked if nid not in clicked][: self.cfg.candidate_cap]
        kept, fatigued_count = self._apply_fatigue(repo, user_id, [nid for nid, _ in eligible], now)
        if len(kept) != len(eligible):
            kept_set = set(kept)
            eligible = [(nid, score) for nid, score in eligible if nid in kept_set]
        if not eligible:
            raise EmptyRecommendation("every popular newsletter was already clicked")
        ids = [int(nid) for nid, _ in eligible]
        return DeterministicList(
            ranked_ids=ids[: self.cfg.top_k],
            eligible_ids=ids,
            scores=np.asarray([float(score) for _, score in eligible], dtype=np.float64),
            source=SOURCE_COLD_POPULAR,
            model_version=self.heuristic.cold_model_version,
            profile_source="none",
            candidate_count=len(ranked),
            cold=True,
            fatigue_mode=self.cfg.fatigue_mode,
            fatigued_count=fatigued_count,
        )

    def rank(
        self, repo: RecsysRepository, user_id: int, now: datetime, deadline: Deadline
    ) -> DeterministicList:
        state = build_user_state(repo, user_id, now, self.cfg)
        if state.below_min_personal_events:
            self.counters.inc("profile.below_min_events")
        deadline.check("candidates")
        clicks = self._click_counts(repo, now)
        if not state.has_personal_signal:
            return self._cold_popular(repo, user_id, now, clicks)

        cands = generate_candidates(repo, state, self.cfg, now, clicks, popular_by_clicks=self.uses_clicks)
        deadline.check("exclusion")
        clicked = repo.clicked_among(user_id, cands.ids)
        state.clicked_ids = clicked
        ids = [i for i in cands.ids if i not in clicked]
        ids, fatigued_count = self._apply_fatigue(repo, user_id, ids, now)
        deadline.check("items")
        items = self._items(repo, ids)
        if not items:
            raise EmptyRecommendation("no scorable candidates")
        deadline.check("scoring")

        item_ids = [int(it.news_letter_id) for it in items]
        # 활성 휴리스틱(또는 활성 모델의 폴백)의 인기 항. popular 출처가 창 안의 수를 이미 읽었다(ADR 0014).
        # 되돌리기 세트(prior)의 heuristic-v1은 이 값을 쓰지 않으므로 읽지 않는다.
        state.clicks_6h = clicks.get(item_ids) if self.uses_clicks else None
        if self.stack.active_needs_features():
            # 활성 모델이 어댑터 피처로 점수를 낸다: 인기도 입력을 요청 경로에서 읽어야 한다.
            state.popularity = self._popularity_in_path(repo, item_ids, now)
        result = self.stack.score(
            state, items, now, deadline, popularity_loader=self._popularity_loader(item_ids, now)
        )
        embeddings = np.stack([it.embedding for it in items])
        # MMR은 항상 top_k개를 고른다. 탐색 칸이 있으면 앞쪽 (top_k - 탐색 칸 수)개만 화면에 들어가고,
        # 탐욕 선택이라 그 앞부분은 탐색을 켜고 꺼도 같다.
        picked = self.reranker.rerank_for_user(
            result.scores, embeddings, self.cfg.top_k, len(state.category_ids)
        )

        if state.long_term is not None or state.short_term is not None:
            source = SOURCE_REALTIME
        elif state.profile_source == "onboarding":
            source = SOURCE_COLD_ONBOARDING
        else:
            source = SOURCE_COLD_CATEGORY
        return DeterministicList(
            ranked_ids=[int(items[idx].news_letter_id) for idx, _ in picked],
            eligible_ids=[int(it.news_letter_id) for it in items],
            scores=np.asarray(result.scores, dtype=np.float64),
            source=source,
            model_version=result.model_version,
            profile_source=state.profile_source,
            candidate_count=len(cands.ids),
            cold=False,
            extra_scores=result.extra_scores,
            features=result.features,
            feature_schema_version=result.feature_schema_version,
            fatigue_mode=self.cfg.fatigue_mode,
            fatigued_count=fatigued_count,
            computed_at=now,
            deferred=result.deferred,
        )

    def _popularity_in_path(
        self, repo: RecsysRepository, ids: Sequence[int], now: datetime
    ) -> Optional[Dict[int, WindowCounts]]:
        """활성 모델을 위한 인기도 조회. 실패하면 None을 돌려준다: 어댑터가 값을 내지 않고, 활성 LightGBM
        스코어러는 그 요청을 휴리스틱으로 채점한다(조회 하나가 요청을 폴백으로 보내지 않는다)."""
        try:
            return load_popularity(repo, ids, now)
        except Exception:
            self._errors.exception("popularity", "popularity lookup for the active model failed")
            self.counters.inc("features.popularity_error")
            repo.rollback()  # 실패한 문장 뒤의 조회가 거부되지 않게(피로 규칙 조회와 같은 이유)
            return None

    def _popularity_loader(self, ids: Sequence[int], now: datetime):
        """요청 경로 밖에서 인기도 입력을 읽는 함수. 자기 저장소(자기 커넥션)를 연다."""
        factory = self.feature_repo_factory
        if factory is None:
            return None

        def load() -> Dict[int, WindowCounts]:
            with factory() as feature_repo:
                return load_popularity(feature_repo, ids, now)

        return load

    def recommend(
        self, repo: RecsysRepository, user_id: int, now: datetime, deadline: Deadline
    ) -> Recommendation:
        """탐색 없이 결정론 목록을 그대로 낸다(오프라인 점검과 테스트용). 서비스는 rank()를 캐시하고
        요청마다 build_recommendation으로 탐색 칸을 넣는다."""
        det = self.rank(repo, user_id, now, deadline)
        return build_recommendation(det, self.cfg, str(uuid.uuid4()), rng=None)
