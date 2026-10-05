"""GET /newsletters/today의 요청 시점 추천 오케스트레이션 (ADR 0015, 탐색·로그는 ADR 0025).

- RECSYS_MODE=realtime: 시간 예산 안에서 캐시(결정론 목록) -> 실시간 파이프라인 -> 요청마다 탐색 칸.
  실패/초과/빈 결과면 폴백 체인(인기 -> 최신)으로 떨어진다. 배치 행은 이 모드에서 읽지 않는다.
- RECSYS_MODE=batch: 폴백 체인만 쓴다(36h 내 배치 행 -> 인기 -> 최신. 기존 배치 동작 + 빈 목록 방지).
"""
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.recsys.cache import TTLCache
from app.recsys.config import RecsysConfig
from app.recsys.exploration import POLICY_EPS_UNIFORM
from app.recsys.metrics import RecsysCounters
from app.recsys.pipeline import (
    POPULARITY_MODEL_VERSION,
    BudgetExceeded,
    Deadline,
    EmptyRecommendation,
    RealtimeRecommender,
    build_recommendation,
    popular_ids,
)
from app.recsys.repository import RecsysRepository
from app.recsys.scoring import HeuristicScorer, Scorer, encode_features
from app.recsys.throttle import ThrottledExceptionLog  # noqa: F401 (기존 임포트 경로 유지)
from app.recsys.types import (
    SOURCE_BATCH,
    SOURCE_EMPTY,
    SOURCE_POPULAR,
    SOURCE_RECENT,
    DeterministicList,
    Recommendation,
    SlotInfo,
)

logger = logging.getLogger(__name__)

RepoFactory = Callable[[], AbstractContextManager]
# (칸 로그 행들, 요청 로그 행) - 한 트랜잭션으로 쓴다(sql_repository.SqlImpressionWriter).
ImpressionWriter = Callable[[List[dict], Optional[dict]], None]
RngFactory = Callable[[str], Optional[np.random.Generator]]

_NO_SLOT = SlotInfo()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rng_for_request(request_id: str) -> np.random.Generator:
    """탐색 뽑기의 난수원. request_id(uuid4, OS 난수 122비트)를 시드로 쓴다 - 요청마다 독립이고,
    같은 numpy 버전에서는 로그의 request_id로 그 요청의 뽑기를 다시 만들어 볼 수 있다(디버깅용.
    분석의 기준은 로그에 남은 위치와 propensity다)."""
    return np.random.default_rng(uuid.UUID(request_id).int)


class RecommendationService:
    def __init__(
        self,
        cfg: RecsysConfig,
        repo_factory: RepoFactory,
        recommender: RealtimeRecommender,
        counters: Optional[RecsysCounters] = None,
        impression_writer: Optional[ImpressionWriter] = None,
        now_fn: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.monotonic,
        rng_factory: RngFactory = rng_for_request,
    ):
        self.cfg = cfg
        self.repo_factory = repo_factory
        self.recommender = recommender
        self.counters = counters or RecsysCounters()
        self.impression_writer = impression_writer
        self.now_fn = now_fn
        self.rng_factory = rng_factory
        # 캐시에 들어가는 것은 결정론 목록(DeterministicList)이다. 탐색 칸은 꺼낸 뒤 요청마다 뽑는다.
        self.cache: TTLCache = TTLCache(cfg.cache_ttl_s, cfg.cache_max_entries, clock=clock)
        self._errors = ThrottledExceptionLog(clock=clock, logger=logger)
        self._executor = ThreadPoolExecutor(
            max_workers=cfg.workers, thread_name_prefix="recsys"
        )
        self._shutdown_hooks: List[Callable[[], None]] = []

    # ------------------------------------------------------------------ public
    def recommend(self, user_id: int, fallback_repo: RecsysRepository) -> Recommendation:
        """fallback_repo는 요청 스레드가 이미 쥔 세션 위의 저장소다. 실시간 경로가 커넥션
        풀 고갈로 막혀 시간 예산을 넘긴 경우에도 폴백은 새 커넥션 없이 돌 수 있다."""
        started = time.perf_counter()
        rec = self._recommend(user_id, fallback_repo)
        rec.latency_ms = int((time.perf_counter() - started) * 1000)
        return rec

    def _recommend(self, user_id: int, fallback_repo: RecsysRepository) -> Recommendation:
        self.counters.inc("requests")
        now = self.now_fn()
        if self.cfg.mode == "batch":
            return self._count(self._fallback(fallback_repo, user_id, now, reason=None))

        deadline = Deadline(self.cfg.time_budget_ms / 1000.0)
        future = self._executor.submit(self._realtime, user_id, now, deadline)
        reason = None
        try:
            rec = future.result(timeout=deadline.remaining())
        except (FutureTimeout, BudgetExceeded):
            future.cancel()
            reason = "timeout"
        except EmptyRecommendation:
            reason = "empty"
        except Exception:
            self._errors.exception("realtime", "realtime recommendation failed for user %s", user_id)
            reason = "error"
        else:
            return self._count(rec)

        self.counters.inc(f"fallback.{reason}")
        return self._count(self._fallback(fallback_repo, user_id, now, reason=reason))

    def log_impressions(
        self, user_id: int, rec: Recommendation, shown_ids: Sequence[int]
    ) -> None:
        """BackgroundTasks에서 호출된다(응답 전송 후). 실패해도 사용자 응답에는 영향이 없다.

        요청 로그 한 행과 화면에 나간 칸마다 한 행을 한 번에 쓴다. 빈 응답도 요청 행은 남긴다.
        화면에 나간 목록이 계획한 목록과 다르면(표시 단계가 항목을 뺐거나 잘랐으면) 위치가 밀려
        propensity가 더는 정확하지 않으므로 그 요청의 propensity는 NULL로 남긴다."""
        shown = list(shown_ids)
        if self.impression_writer is None:
            self.counters.inc("impressions.skipped", len(shown))
            return
        planned = bool(rec.slots)
        intact = planned and shown == list(rec.news_letter_ids)
        if planned and not intact:
            self.counters.inc("impressions.slate_mismatch")
        slot_by_id = dict(zip(rec.news_letter_ids, rec.slots)) if planned else {}
        score_by_id = rec.score_by_id()
        rows = []
        for pos, nid in enumerate(shown):
            slot = slot_by_id.get(nid, _NO_SLOT)
            rows.append(
                {
                    "request_id": rec.request_id,
                    "user_id": user_id,
                    "news_letter_id": nid,
                    "position": pos,
                    "score": score_by_id.get(nid),
                    "source": rec.source,
                    "model_version": rec.model_version,
                    "explored": slot.explored,
                    "propensity": slot.propensity if intact else None,
                    "det_rank": slot.det_rank,
                    "scores_shadow": slot.scores_shadow,
                    "features": None if slot.features is None else encode_features(slot.features),
                }
            )
        request_row = {
            "request_id": rec.request_id,
            "user_id": user_id,
            "source": rec.source,
            "model_version": rec.model_version,
            "policy_version": rec.policy_version,
            "profile_source": rec.profile_source,
            "cache_hit": rec.cache_hit,
            "fallback_reason": rec.fallback_reason,
            "candidate_count": rec.candidate_count,
            "eligible_count": None if rec.eligible_ids is None else len(rec.eligible_ids),
            "explore_pool_size": rec.explore_pool_size,
            # 폴백 응답에는 계획한 화면이 없다(표시 단계가 앞에서부터 자른다): 나간 수를 그대로 적는다.
            "slate_size": len(rec.news_letter_ids) if planned else len(shown),
            "shown_count": len(shown),
            "explore_positions": None if rec.explore_positions is None else list(rec.explore_positions),
            "candidate_ids": None if rec.eligible_ids is None else list(rec.eligible_ids),
            "feature_schema_version": rec.feature_schema_version,
            "shadow_versions": list(rec.shadow_versions) or None,
            "fatigue_mode": rec.fatigue_mode,
            "fatigued_count": rec.fatigued_count,
            "latency_ms": rec.latency_ms,
        }
        try:
            self.impression_writer(rows, request_row)
        except Exception:
            self._errors.exception("impressions", "failed to write %d impression rows", len(rows))
            self.counters.inc("impressions.failed")
            self.counters.inc("requests.log_failed")
            return
        self.counters.inc("impressions.logged", len(rows))
        self.counters.inc("requests.logged")

    def add_shutdown_hook(self, hook: Callable[[], None]) -> None:
        self._shutdown_hooks.append(hook)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        for hook in self._shutdown_hooks:
            hook()

    # ----------------------------------------------------------------- private
    def _count(self, rec: Recommendation) -> Recommendation:
        self.counters.inc(f"source.{rec.source}")
        return rec

    def _realtime(self, user_id: int, now: datetime, deadline: Deadline) -> Recommendation:
        with self.repo_factory() as repo:
            last_click = repo.last_click_id(user_id)
            key = (user_id, last_click)
            det: Optional[DeterministicList] = self.cache.get(key)
            cache_hit = det is not None
            if cache_hit:
                self.counters.inc("cache.hit")
            else:
                self.counters.inc("cache.miss")
                det = self.recommender.rank(repo, user_id, now, deadline)
                # 예산을 넘겨 요청은 이미 폴백으로 응답했더라도, 끝까지 계산된 결과는
                # 다음 요청(같은 클릭 상태)이 쓰도록 캐시에 남긴다.
                self.cache.put(key, det)
        # 탐색은 캐시 뒤에서 요청마다 한다: 60초 안의 재요청도 탐색 칸은 독립이다(ADR 0025).
        request_id = str(uuid.uuid4())
        rec = build_recommendation(det, self.cfg, request_id, self.rng_factory(request_id), cache_hit)
        if rec.policy_version == POLICY_EPS_UNIFORM:
            self.counters.inc("explore.requests")
            self.counters.inc("explore.slots", len(rec.explore_positions))
        return rec

    def _fallback(
        self,
        repo: RecsysRepository,
        user_id: int,
        now: datetime,
        reason: Optional[str],
    ) -> Recommendation:
        # 저장소가 화면에 내보낼 수 있는 뉴스레터만 돌려주므로(repository.py) 여기서 ID가
        # 비어 있지 않으면 응답 본문도 비지 않는다. 이 조회와 표시 단계 사이에 행이 지워지는
        # 경우를 대비해 top_k보다 넉넉히 넘기고 표시 단계가 앞에서부터 자른다.
        n = self.cfg.top_k * 2
        steps = [
            (SOURCE_POPULAR, lambda: self._popular(repo, now, n)),
            (SOURCE_RECENT, lambda: (repo.recent_ids(n), "recency")),
        ]
        if self.cfg.mode == "batch":
            # 배치 행은 batch 모드에서만 읽는다. realtime 모드의 폴백에서 뺀 이유(ADR 0015 "폴백 체인"):
            # 팀 배치 추천 잡이 꺼져 있어 그 행을 쓰는 것은 인기 목록을 다시 적는 잡뿐이고, 그러면 이 단계는
            # 다음 단계(인기)와 같은 목록을 더 오래된 시점 기준으로 돌려줄 뿐이다.
            steps.insert(0, (SOURCE_BATCH, lambda: self._batch_ids(repo, user_id, now)))
        for source, step in steps:
            try:
                got = step()
            except Exception:
                self._errors.exception(
                    f"fallback.{source}", "fallback step %s failed for user %s", source, user_id
                )
                self.counters.inc(f"fallback_step_error.{source}")
                self._rollback(repo)
                continue
            if got is None:
                continue
            ids, model_version = got
            if ids:
                return Recommendation(
                    request_id=str(uuid.uuid4()),
                    news_letter_ids=list(ids),
                    scores=[None] * len(ids),
                    source=source,
                    model_version=model_version,
                    fallback_reason=reason,
                )
        self.counters.inc("fallback.exhausted")
        return Recommendation(
            request_id=str(uuid.uuid4()),
            news_letter_ids=[],
            scores=[],
            source=SOURCE_EMPTY,
            model_version="none",
            fallback_reason=reason,
        )

    @staticmethod
    def _rollback(repo: RecsysRepository) -> None:
        # 폴백 단계들은 같은 요청 세션을 쓴다. PostgreSQL은 문장 하나가 실패하면 트랜잭션을
        # 중단 상태로 두므로, 되돌리지 않으면 다음 단계도 "current transaction is aborted"로
        # 실패해 체인이 인기/최신에 닿지 못하고 empty로 끝난다. 이 시점의 요청 세션은 읽기만
        # 했으므로 되돌려도 잃는 것이 없다.
        try:
            repo.rollback()
        except Exception:
            logger.exception("rollback after a failed fallback step failed")

    def _batch_ids(self, repo: RecsysRepository, user_id: int, now: datetime):
        row = repo.latest_batch(user_id)
        if not row:
            return None
        created_at, ids = row
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if now - created_at > timedelta(hours=self.cfg.batch_max_age_hours):
            self.counters.inc("fallback.batch_stale")
            return None
        # 배치 행은 만들어진 시점의 목록이라 화면에 못 내보내는 뉴스레터가 섞여 있을 수 있다.
        # 하나도 보여 줄 수 없으면 빈 화면 대신 다음 단계(인기)로 넘어간다.
        shown = repo.displayable_among(ids)
        ids = [i for i in ids if i in shown]
        if not ids:
            self.counters.inc("fallback.batch_undisplayable")
            return None
        return ids, "batch"

    def _popular(self, repo: RecsysRepository, now: datetime, n: int):
        since = now - timedelta(hours=self.cfg.freshness_hours)
        return popular_ids(repo, since, now, n), POPULARITY_MODEL_VERSION


def build_service(
    cfg: RecsysConfig,
    repo_factory: RepoFactory,
    scorer: Optional[Scorer] = None,
    impression_writer: Optional[ImpressionWriter] = None,
    now_fn: Callable[[], datetime] = utcnow,
    clock: Callable[[], float] = time.monotonic,
    counters: Optional[RecsysCounters] = None,
    rng_factory: RngFactory = rng_for_request,
) -> RecommendationService:
    """scorer는 스코어러 하나(shadow 없음)이거나 ScorerStack(활성 + shadow)이다."""
    counters = counters or RecsysCounters()
    recommender = RealtimeRecommender(cfg, scorer=scorer or HeuristicScorer(), counters=counters)
    return RecommendationService(
        cfg,
        repo_factory=repo_factory,
        recommender=recommender,
        counters=counters,
        impression_writer=impression_writer,
        now_fn=now_fn,
        clock=clock,
        rng_factory=rng_factory,
    )
