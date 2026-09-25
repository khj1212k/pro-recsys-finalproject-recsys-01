"""요청 시점 추천 파이프라인: 사용자 상태 -> 후보 합집합 -> 스코어링 -> MMR (ADR 0015)."""
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np

from app.recsys.cache import TTLCache
from app.recsys.config import RecsysConfig
from app.recsys.repository import RecsysRepository
from app.recsys.scoring import Scorer
from app.recsys.types import (
    SOURCE_COLD_CATEGORY,
    SOURCE_COLD_ONBOARDING,
    SOURCE_COLD_POPULAR,
    SOURCE_REALTIME,
    Item,
    Recommendation,
    UserState,
)
from scheduler.calculate_ranking import compute_scores
# 성승우님이 recommend_engine(배치 LightGBM 경로)에 구현한 MMR을 그대로 쓴다 -
# 요청 시점 경로와 배치 경로가 같은 다양성 규칙(선호 카테고리 수별 λ)을 공유하도록.
from src.core.reranker import CategoryBasedMMRReranker

POPULARITY_MODEL_VERSION = "popularity-v1"


class BudgetExceeded(Exception):
    def __init__(self, stage: str):
        super().__init__(f"time budget exhausted before {stage}")
        self.stage = stage


class EmptyRecommendation(Exception):
    pass


class Deadline:
    def __init__(self, budget_s: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._end = clock() + budget_s

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def check(self, stage: str) -> None:
        if self._clock() >= self._end:
            raise BudgetExceeded(stage)


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


class RealtimeRecommender:
    def __init__(
        self,
        cfg: RecsysConfig,
        scorer: Scorer,
        reranker: Optional[CategoryBasedMMRReranker] = None,
        item_cache: Optional[TTLCache] = None,
    ):
        self.cfg = cfg
        self.scorer = scorer
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

    def _cold_popular(self, repo, user_id, now) -> Recommendation:
        since = now - timedelta(hours=self.cfg.freshness_hours)
        ranked = compute_scores(repo.window_meta(since), now)
        if not ranked:
            raise EmptyRecommendation("no newsletters in the freshness window")
        clicked = repo.clicked_among(user_id, [s["id"] for s in ranked])
        top = [s for s in ranked if s["id"] not in clicked][: self.cfg.top_k]
        if not top:
            raise EmptyRecommendation("every popular newsletter was already clicked")
        return Recommendation(
            request_id=str(uuid.uuid4()),
            news_letter_ids=[s["id"] for s in top],
            scores=[float(s["score"]) for s in top],
            source=SOURCE_COLD_POPULAR,
            model_version=POPULARITY_MODEL_VERSION,
        )

    def recommend(
        self, repo: RecsysRepository, user_id: int, now: datetime, deadline: Deadline
    ) -> Recommendation:
        state = build_user_state(repo, user_id, now, self.cfg)
        deadline.check("candidates")
        if not state.has_personal_signal:
            return self._cold_popular(repo, user_id, now)

        cands = generate_candidates(repo, state, self.cfg, now)
        deadline.check("exclusion")
        clicked = repo.clicked_among(user_id, cands.ids)
        state.clicked_ids = clicked
        ids = [i for i in cands.ids if i not in clicked]
        deadline.check("items")
        items = self._items(repo, ids)
        if not items:
            raise EmptyRecommendation("no scorable candidates")
        deadline.check("scoring")

        result = self.scorer.score(state, items, now)
        embeddings = np.stack([it.embedding for it in items])
        picked = self.reranker.rerank_for_user(
            result.scores, embeddings, self.cfg.top_k, len(state.category_ids)
        )

        if state.long_term is not None or state.short_term is not None:
            source = SOURCE_REALTIME
        elif state.profile_source == "onboarding":
            source = SOURCE_COLD_ONBOARDING
        else:
            source = SOURCE_COLD_CATEGORY
        return Recommendation(
            request_id=str(uuid.uuid4()),
            news_letter_ids=[items[idx].news_letter_id for idx, _ in picked],
            scores=[float(score) for _, score in picked],
            source=source,
            model_version=result.model_version,
        )
