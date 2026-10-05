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
from app.recsys.scoring import Scorer, ScorerStack
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

POPULARITY_MODEL_VERSION = "popularity-v1"


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

    장기 벡터는 클릭마다 갱신해 둔 증분 상태의 방향이고(읽을 때 감쇠를 다시 계산하지 않는다),
    단기 벡터는 최근 클릭을 한 번 읽어 여기서 더한다. 같은 클릭 행들이 피처 어댑터의 입력이 된다."""
    profile_state, category_ids = repo.profile_state(user_id)
    # 어댑터가 정의한 창(24시간·20건)과 설정의 창 중 넓은 쪽으로 한 번 읽고, 각자 자기 몫을 쓴다.
    adapter_since = core_serving.short_window_start(now)
    heuristic_since = now - timedelta(hours=cfg.short_term_hours)
    clicks = repo.recent_clicks(
        user_id,
        min(adapter_since, heuristic_since),
        now,
        max(core_serving.SHORT_MAX_EVENTS, cfg.short_term_max_clicks),
    )
    # "최근 N개"의 순서는 어댑터가 정한 것 하나다((초, 뉴스레터 ID) 순 - recsys_core.serving.latest_events).
    for_adapter = core_serving.latest_events([c for c in clicks if c.at >= adapter_since])
    for_heuristic = core_serving.latest_events(
        [c for c in clicks if c.at >= heuristic_since], cfg.short_term_max_clicks
    )

    long_term = profile_state.hist.direction()
    state = UserState(
        user_id=user_id,
        long_term=long_term,
        short_term=_unit_sum(for_heuristic),
        category_ids=list(category_ids),
        hist=profile_state.hist,
        hist_last_event_at=profile_state.last_event_at,
        recent_clicks=for_adapter,
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


def popular_ids(
    repo: RecsysRepository, since: datetime, now: datetime, n: int
) -> List[int]:
    return [s["id"] for s in compute_scores(repo.window_meta(since), now)[:n]]


def _round_robin_union(sources: Dict[str, List[int]], cap: int) -> CandidateSet:
    merged, contributed = round_robin_union(sources, cap)
    return CandidateSet(ids=merged, by_source=sources, contributed=contributed)


def generate_candidates(
    repo: RecsysRepository, state: UserState, cfg: RecsysConfig, now: datetime
) -> CandidateSet:
    """출처·순서·k·창·상한은 전부 cfg.candidate_spec()에서 온다(recsys_core.SERVING_CANDIDATE_SPEC, ADR 0033).
    신호가 없는 출처(프로필 없음, 최근 클릭 없음, 선호 카테고리 없음)는 목록 자체를 내지 않는다."""
    spec = cfg.candidate_spec()
    since = now - timedelta(hours=spec.window_h)
    producers = {
        "knn_profile": lambda k: None if state.profile is None else repo.knn_ids(state.profile, since, k),
        "knn_short": lambda k: None if state.short_term is None else repo.knn_ids(state.short_term, since, k),
        "recent": lambda k: repo.recent_ids(k),
        "popular": lambda k: popular_ids(repo, since, now, k),
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
    ):
        self.cfg = cfg
        self.scorer = scorer
        # 요청 경로 밖에서 도는 피처 작업이 인기도 창 집계를 읽을 때 쓰는 저장소(자기 커넥션). 없으면 그 작업은
        # 인기도 입력을 읽지 못하고, 어댑터 피처는 남지 않는다.
        self.feature_repo_factory = feature_repo_factory
        self.counters = counters or RecsysCounters()
        # 스코어러 하나만 받으면 shadow 없는 묶음으로 감싼다. 묶음을 받으면 그대로 쓴다.
        self.stack = scorer if isinstance(scorer, ScorerStack) else ScorerStack(scorer, counters=self.counters)
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

    def _cold_popular(self, repo, user_id, now) -> DeterministicList:
        since = now - timedelta(hours=self.cfg.freshness_hours)
        ranked = compute_scores(repo.window_meta(since), now)
        if not ranked:
            raise EmptyRecommendation("no newsletters in the freshness window")
        clicked = repo.clicked_among(user_id, [s["id"] for s in ranked])
        # 후보 수 상한은 개인화 경로와 같다. 인기순 상위 candidate_cap개가 E다.
        eligible = [s for s in ranked if s["id"] not in clicked][: self.cfg.candidate_cap]
        kept, fatigued_count = self._apply_fatigue(repo, user_id, [s["id"] for s in eligible], now)
        if len(kept) != len(eligible):
            kept_set = set(kept)
            eligible = [s for s in eligible if s["id"] in kept_set]
        if not eligible:
            raise EmptyRecommendation("every popular newsletter was already clicked")
        ids = [int(s["id"]) for s in eligible]
        return DeterministicList(
            ranked_ids=ids[: self.cfg.top_k],
            eligible_ids=ids,
            scores=np.asarray([float(s["score"]) for s in eligible], dtype=np.float64),
            source=SOURCE_COLD_POPULAR,
            model_version=POPULARITY_MODEL_VERSION,
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
        deadline.check("candidates")
        if not state.has_personal_signal:
            return self._cold_popular(repo, user_id, now)

        cands = generate_candidates(repo, state, self.cfg, now)
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
