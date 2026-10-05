"""요청 시점 추천 파이프라인: 사용자 상태 -> 후보 합집합 -> 스코어링 -> MMR (ADR 0015).

여기서 만드는 것은 결정론 목록(DeterministicList)까지다. 탐색 칸을 넣어 한 요청의 화면으로 만드는 일
(build_recommendation)은 결과 캐시 뒤에서 요청마다 한다(ADR 0025).
"""
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.recsys.cache import TTLCache
from app.recsys.config import RecsysConfig
from app.recsys.deadline import BudgetExceeded, Deadline  # noqa: F401 (기존 임포트 경로 유지)
from app.recsys.exploration import plan_slate
from app.recsys.metrics import RecsysCounters
from app.recsys.repository import RecsysRepository
from app.recsys.scoring import Scorer, ScorerStack
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


def build_user_state(
    repo: RecsysRepository, user_id: int, now: datetime, cfg: RecsysConfig
) -> UserState:
    long_term, category_ids = repo.long_term_and_categories(user_id)
    short_term = repo.short_term_vector(
        user_id, now - timedelta(hours=cfg.short_term_hours), cfg.short_term_max_clicks
    )
    state = UserState(
        user_id=user_id,
        long_term=long_term,
        short_term=short_term,
        category_ids=list(category_ids),
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


def popular_ids(
    repo: RecsysRepository, since: datetime, now: datetime, n: int
) -> List[int]:
    return [s["id"] for s in compute_scores(repo.window_meta(since), now)[:n]]


def _round_robin_union(sources: Dict[str, List[int]], cap: int) -> CandidateSet:
    seen, merged = set(), []
    contributed = {name: 0 for name in sources}
    iters = {name: iter(ids) for name, ids in sources.items()}
    while iters and len(merged) < cap:
        for name in list(iters):
            for nid in iters[name]:
                if nid not in seen:
                    seen.add(nid)
                    merged.append(nid)
                    contributed[name] += 1
                    break
            else:
                del iters[name]
                continue
            if len(merged) >= cap:
                break
    return CandidateSet(ids=merged, by_source=sources, contributed=contributed)


def generate_candidates(
    repo: RecsysRepository, state: UserState, cfg: RecsysConfig, now: datetime
) -> CandidateSet:
    since = now - timedelta(hours=cfg.freshness_hours)
    sources: Dict[str, List[int]] = {}
    if state.profile is not None:
        sources["knn_profile"] = repo.knn_ids(state.profile, since, cfg.knn_k)
    if state.short_term is not None:
        sources["knn_short"] = repo.knn_ids(state.short_term, since, cfg.knn_k)
    sources["recent"] = repo.recent_ids(cfg.recent_n)
    sources["popular"] = popular_ids(repo, since, now, cfg.popular_n)
    if state.category_ids:
        sources["category"] = repo.category_recent_ids(state.category_ids, since, cfg.category_n)
    # 캡을 넘으면 한 생성기(예: KNN 100개)가 자리를 독식하지 않도록 순서를 번갈아 합친다.
    return _round_robin_union(sources, cfg.candidate_cap)


def _finite_or_none(value) -> Optional[float]:
    value = float(value)
    return value if math.isfinite(value) else None


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
            # JSON에는 NaN/inf가 없다: 유한하지 않은 shadow 점수는 null로 남긴다.
            scores_shadow={v: _finite_or_none(arr[i]) for v, arr in det.extra_scores.items()} or None,
            features=None if det.features is None else det.features[i],
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
    )


class RealtimeRecommender:
    def __init__(
        self,
        cfg: RecsysConfig,
        scorer: Scorer,
        reranker: Optional[CategoryBasedMMRReranker] = None,
        item_cache: Optional[TTLCache] = None,
        counters: Optional[RecsysCounters] = None,
    ):
        self.cfg = cfg
        self.scorer = scorer
        self.counters = counters or RecsysCounters()
        # 스코어러 하나만 받으면 shadow 없는 묶음으로 감싼다. 묶음을 받으면 그대로 쓴다.
        self.stack = scorer if isinstance(scorer, ScorerStack) else ScorerStack(scorer, counters=self.counters)
        self.reranker = reranker or CategoryBasedMMRReranker()
        self.item_cache = item_cache or TTLCache(cfg.item_cache_ttl_s, cfg.item_cache_max_entries)

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
            logger.warning("fatigue lookup failed in log mode; continuing without it", exc_info=True)
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

        result = self.stack.score(state, items, now, deadline)
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
        )

    def recommend(
        self, repo: RecsysRepository, user_id: int, now: datetime, deadline: Deadline
    ) -> Recommendation:
        """탐색 없이 결정론 목록을 그대로 낸다(오프라인 점검과 테스트용). 서비스는 rank()를 캐시하고
        요청마다 build_recommendation으로 탐색 칸을 넣는다."""
        det = self.rank(repo, user_id, now, deadline)
        return build_recommendation(det, self.cfg, str(uuid.uuid4()), rng=None)
