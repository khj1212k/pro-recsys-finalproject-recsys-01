"""rss_retry: 직전 ingest가 읽지 못한 RSS 피드만 다시 읽는다(수집만 - 본문 추출·임베딩 없음).

동아일보 RSS는 Tier 0에서 정시 회차의 약 75%가 RemoteDisconnected로 실패하고(2026-10-16 ~ 10-25, ADR 0026 2026-10-25 갱신 2),
피드가 50건뿐이라 읽기에 성공한 회차 사이가 6시간을 넘으면 그 사이 기사를 잃는다. 이 잡은 매시 :40(docker/crontab.micro,
popularity :35 뒤)에 직전 ingest 실행의 `stats.rss.per_feed`에서 `error`가 있던 피드만 다시 읽는다. 시간당 피드 읽기가 최대 1회
-> 2회가 될 뿐이고, 실패하지 않은 피드는 건드리지 않는다.

- 본문 추출은 하지 않는다. 다시 읽어 새로 들어온 'direct' 피드의 기사는 다음 정시 ingest(:05)가 추출하고, 'rss_summary' 피드(동아일보)는
  수집 때 요약이 본문 칸에 채워진다.
- 실패한 피드가 없으면 요청 없이 바로 끝난다(성공으로 기록, `retry_feeds`가 빈 목록).
- ingest와 겹치지 않는다: ingest의 advisory lock을 잡고 읽는다. ingest가 돌고 있으면 건너뛰고(skipped, `ingest_running`), 이 잡이
  도는 동안은 정시 ingest가 시작하지 못한다(건너뛴 ingest는 job_runs에 skipped로 남는다). 이 잡은 RSS 단계의 일부라 길지 않다 -
  2026-10-16 ~ 10-25 ingest 211회의 RSS 단계 전체가 중앙값 22초·최대 136초였다. 겹치면 요약 피드의 중복 방지(수집기가 맡는다,
  ADR 0026 한계 3)가 깨진다.
- 같은 ingest 실행의 실패 피드는 한 번만 다시 읽는다(`source_run_id`) - ingest가 건너뛰어져 새 실행이 없을 때 같은 목록을 시간마다
  되풀이하지 않는다.
- 같은 호스트 건너뛰기 규칙은 수집기(`collect_rss`)의 것을 그대로 쓴다: 호스트의 첫 피드를 못 읽으면 같은 호스트의 나머지는 재시도 없이 건너뛴다.
- 재시도가 또 실패해도 잡은 성공이고 경고도 더하지 않는다(그 회차에 ingest가 이미 피드별 경고를 냈다). 결과는 `stats.recovered`·`stats.still_failed`
  와 ingest와 같은 모양의 `stats.rss.per_feed`에 남는다.
"""
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional

from jobs.runtime import JobSkipped
from jobs.tasks.ingest import warn_empty_summaries

logger = logging.getLogger("jobs")

INGEST_JOB = "ingest"
# 다시 읽어도 달라지지 않는 오류(요청을 보내기 전의 설정 문제): press 테이블에 언론사가 없다.
NON_RETRYABLE_ERRORS = ("press_id not found",)


def add_arguments(parser) -> None:
    parser.add_argument("--rss-hours", type=int, default=100,
                        help="RSS 항목 발행 시각 cutoff(시간, ingest와 같은 기본값)")


def select_retry_feeds(per_feed: Optional[Dict[str, Any]], configured: Iterable[str]) -> List[str]:
    """직전 ingest의 per_feed에서 `error`가 있던 피드 이름 - 설정(Settings.RSS_FEEDS)에 아직 있는 것만, 설정 순서대로.

    같은 호스트라서 건너뛴 피드('skipped: <host> unreadable earlier in this run')도 오류가 있던 피드라 들어간다 - 다시 읽을 때 수집기가
    호스트의 첫 피드만 시도하고 나머지는 같은 규칙으로 건너뛴다.
    """
    failed = set()
    for name, feed_stats in (per_feed or {}).items():
        error = (feed_stats or {}).get("error")
        if error and not str(error).startswith(NON_RETRYABLE_ERRORS):
            failed.add(name)
    return [name for name in configured if name in failed]


def load_source(cur) -> Optional[Dict[str, Any]]:
    """RSS 단계의 통계가 남은 가장 최근 ingest 실행. 없으면 None.

    돌고 있는 ingest는 호출하는 쪽이 advisory lock으로 막으므로 여기서 'running' 행은 프로세스가 죽어 남은 행이다(그 행의 stats는
    RSS 단계가 끝난 뒤의 체크포인트까지 남아 있다). `already_retried`는 이 실행을 근거로 한 rss_retry 행이 이미 있는지다.
    """
    cur.execute(
        """
        SELECT j.id, j.status, j.started_at, now(), j.stats #> '{rss,per_feed}',
               EXISTS (SELECT 1 FROM job_runs r
                       WHERE r.job = 'rss_retry' AND r.started_at >= j.started_at
                         AND r.stats ->> 'source_run_id' = j.id::text)
        FROM job_runs j
        WHERE j.job = 'ingest' AND jsonb_typeof(j.stats #> '{rss,per_feed}') = 'object'
        ORDER BY j.started_at DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        return None
    run_id, status, started_at, now, per_feed, already_retried = row
    return {
        "run_id": run_id, "status": status, "started_at": started_at,
        "age_min": (now - started_at).total_seconds() / 60,
        "per_feed": per_feed or {}, "already_retried": bool(already_retried),
    }


def read_source() -> Optional[Dict[str, Any]]:
    from db.connection import get_db_connection

    with get_db_connection() as conn, conn.cursor() as cur:
        return load_source(cur)


@contextmanager
def ingest_lock() -> Iterator[None]:
    """ingest의 advisory lock을 잡고 있는 동안만 연다. ingest가 돌고 있으면 JobSkipped("ingest_running").

    잡 런타임(jobs/runtime.py)은 잡 이름으로 락을 잡으므로(이 잡은 'rss_retry') ingest의 락은 따로 잡는다 - 전용 연결 하나에 세션
    락으로 건다(풀 연결에 걸면 반납 뒤 엉뚱한 곳에 딸려 간다). 프로세스가 죽으면 연결이 끊기며 락도 풀린다.
    """
    from jobs.store import PostgresJobRunStore

    store = PostgresJobRunStore.connect("rss_retry")
    try:
        if not store.try_lock(INGEST_JOB):
            raise JobSkipped("ingest_running")
        try:
            yield
        finally:
            try:
                store.unlock(INGEST_JOB)
            except Exception:
                logger.exception("rss_retry: ingest advisory lock 해제 실패 (연결을 닫으면 풀린다)")
    finally:
        store.close()


def run(ctx) -> Dict[str, Any]:
    from config.settings import Settings
    from crawler.rss_collector import collect_rss

    with ingest_lock():
        source = read_source()
        if source is None:
            raise JobSkipped("no_ingest_rss_stats")
        if source["already_retried"]:
            raise JobSkipped("already_retried", {"skipped_source_run_id": source["run_id"]})

        retry = select_retry_feeds(source["per_feed"], Settings.RSS_FEEDS)
        ctx.stats.update({
            "source_run_id": source["run_id"],
            "source_status": source["status"],
            "source_started_at": source["started_at"].isoformat(),
            "source_age_min": round(source["age_min"], 1),
            "retry_feeds": retry,
        })
        if not retry:
            return {}

        ctx.checkpoint()  # 읽는 도중 죽어도 같은 ingest 실행의 피드를 다시 읽지 않도록 source_run_id를 먼저 남긴다
        started = time.monotonic()
        rss = dict(collect_rss(hours=ctx.args.rss_hours, feeds=retry))
        rss["duration_s"] = round(time.monotonic() - started, 3)

    ctx.stats["rss"] = rss
    per_feed = rss.get("per_feed") or {}
    ctx.stats["recovered"] = [name for name in retry if name in per_feed and not (per_feed[name] or {}).get("error")]
    ctx.stats["still_failed"] = [name for name in retry if name not in ctx.stats["recovered"]]
    warn_empty_summaries(ctx, rss)
    return {}
