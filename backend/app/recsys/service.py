"""GET /newsletters/today의 요청 시점 추천 오케스트레이션 (ADR 0015).

- RECSYS_MODE=realtime: 시간 예산 안에서 캐시 -> 실시간 파이프라인, 실패/초과/빈 결과면
  폴백 체인(24~36h 내 배치 행 -> 인기 -> 최신)으로 떨어진다.
- RECSYS_MODE=batch: 폴백 체인만 쓴다(기존 배치 동작 + 빈 목록 방지).
"""
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence

from app.recsys.cache import TTLCache
from app.recsys.config import RecsysConfig
from app.recsys.metrics import RecsysCounters
from app.recsys.pipeline import (
    POPULARITY_MODEL_VERSION,
    BudgetExceeded,
    Deadline,
    EmptyRecommendation,
    RealtimeRecommender,
    popular_ids,
)
from app.recsys.repository import RecsysRepository
from app.recsys.scoring import HeuristicScorer, Scorer
from app.recsys.types import (
    SOURCE_BATCH,
    SOURCE_EMPTY,
    SOURCE_POPULAR,
    SOURCE_RECENT,
    Recommendation,
)

logger = logging.getLogger(__name__)

RepoFactory = Callable[[], AbstractContextManager]
ImpressionWriter = Callable[[List[dict]], None]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    ):
        self.cfg = cfg
        self.repo_factory = repo_factory
        self.recommender = recommender
        self.counters = counters or RecsysCounters()
        self.impression_writer = impression_writer
        self.now_fn = now_fn
        self.cache: TTLCache = TTLCache(cfg.cache_ttl_s, cfg.cache_max_entries, clock=clock)
        self._executor = ThreadPoolExecutor(
            max_workers=cfg.workers, thread_name_prefix="recsys"
        )

    # ------------------------------------------------------------------ public
    def recommend(self, user_id: int, fallback_repo: RecsysRepository) -> Recommendation:
        """fallback_repo는 요청 스레드가 이미 쥔 세션 위의 저장소다. 실시간 경로가 커넥션
        풀 고갈로 막혀 시간 예산을 넘긴 경우에도 폴백은 새 커넥션 없이 돌 수 있다."""
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
            logger.exception("realtime recommendation failed for user %s", user_id)
            reason = "error"
        else:
            return self._count(rec)

        self.counters.inc(f"fallback.{reason}")
        return self._count(self._fallback(fallback_repo, user_id, now, reason=reason))

    def log_impressions(
        self, user_id: int, rec: Recommendation, shown_ids: Sequence[int]
    ) -> None:
        """BackgroundTasks에서 호출된다(응답 전송 후). 실패해도 사용자 응답에는 영향이 없다."""
        if not shown_ids:
            return
        if self.impression_writer is None:
            self.counters.inc("impressions.skipped", len(shown_ids))
            return
        score_by_id = rec.score_by_id()
        rows = [
            {
                "request_id": rec.request_id,
                "user_id": user_id,
                "news_letter_id": nid,
                "position": pos,
                "score": score_by_id.get(nid),
                "source": rec.source,
                "model_version": rec.model_version,
            }
            for pos, nid in enumerate(shown_ids)
        ]
        try:
            self.impression_writer(rows)
        except Exception:
            logger.exception("failed to write %d impression rows", len(rows))
            self.counters.inc("impressions.failed")
            return
        self.counters.inc("impressions.logged", len(rows))

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ----------------------------------------------------------------- private
    def _count(self, rec: Recommendation) -> Recommendation:
        self.counters.inc(f"source.{rec.source}")
        return rec

    def _realtime(self, user_id: int, now: datetime, deadline: Deadline) -> Recommendation:
        with self.repo_factory() as repo:
            last_click = repo.last_click_id(user_id)
            key = (user_id, last_click)
            cached = self.cache.get(key)
            if cached is not None:
                self.counters.inc("cache.hit")
                return replace(cached, request_id=str(uuid.uuid4()), cache_hit=True)
            self.counters.inc("cache.miss")
            rec = self.recommender.recommend(repo, user_id, now, deadline)
            # 예산을 넘겨 요청은 이미 폴백으로 응답했더라도, 끝까지 계산된 결과는
            # 다음 요청(같은 클릭 상태)이 쓰도록 캐시에 남긴다.
            self.cache.put(key, rec)
            return rec

    def _fallback(
        self,
        repo: RecsysRepository,
        user_id: int,
        now: datetime,
        reason: Optional[str],
    ) -> Recommendation:
        # 표시 단계에서 카테고리 없는 뉴스레터가 빠질 수 있어 top_k보다 넉넉히 가져온다
        # (배치 행은 기존 동작대로 통째로 넘기고 표시 단계가 앞에서부터 자른다).
        n = self.cfg.top_k * 2
        steps = (
            (SOURCE_BATCH, lambda: self._batch_ids(repo, user_id, now)),
            (SOURCE_POPULAR, lambda: self._popular(repo, now, n)),
            (SOURCE_RECENT, lambda: (repo.recent_ids(n), "recency")),
        )
        for source, step in steps:
            try:
                got = step()
            except Exception:
                logger.exception("fallback step %s failed for user %s", source, user_id)
                self.counters.inc(f"fallback_step_error.{source}")
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
) -> RecommendationService:
    recommender = RealtimeRecommender(cfg, scorer=scorer or HeuristicScorer())
    return RecommendationService(
        cfg,
        repo_factory=repo_factory,
        recommender=recommender,
        impression_writer=impression_writer,
        now_fn=now_fn,
        clock=clock,
    )
