"""retention: 보존 기한(기본 30일)이 지난 상업 언론사 본문을 비운다 - 해시·길이·제목·URL·임베딩은 남긴다 (ADR 0023).

만료: `raw_news_crawled_at + days <= 지금(UTC)`. raw_news_crawled_at은 시간대 없는 UTC 값이라 기준 시각도
`now() AT TIME ZONE 'UTC'`로 DB에서 한 번 만들고(세션 TimeZone과 무관), 그 실행 동안 고정한다.
예: 2026-09-26 02:36 UTC(11:36 KST)에 수집한 기사는 2026-10-26 02:36 UTC(11:36 KST)에 만료된다.

한 행을 비우는 것: 본문을 NULL로, 길이(char_length)·비운 시각을 쓰고, 본문 sha256이 비어 있으면 채운다.
건드리지 않는 행:
- 재배포 가능 출처(config/sources.py - 정책브리핑, 공공누리 제1유형). 유형이 1이 아니라고 적힌 행은 대상이다.
- 본문 추출기가 아직 고를 수 있는 빈 행(상태 NULL, 또는 재시도 여지가 남은 fetch_failed/error이고 본문 '').
  본문 추출기는 이 행들만 쓰고 이 잡은 나머지만 쓰므로 두 잡이 같은 행을 두고 기다리지 않는다. 그런 행에
  나중에 본문이 들어오면(만료 직전에 추출된 경우) 다음 실행이 비운다.

배치: 기한이 지난 대상 행을 id 순서로 --batch-size개씩 `FOR UPDATE SKIP LOCKED`로 골라 한 문장으로 비우고 배치마다
커밋한다. 배치마다 statement_timeout·lock_timeout이 걸린다. 다른 트랜잭션이 잡고 있는 행은 기다리지 않고 건너뛰며,
끝난 뒤 "기한이 지났는데 본문이 남은 행"을 다시 세어 0이 아니면 경고(알림)를 남긴다.
멱등이다: 두 번째 실행은 0행을 비운다. 같은 잡의 동시 실행은 jobs.run의 advisory lock이 막는다.

--dry-run은 세기만 한다(UPDATE 없음). job_runs.stats에 scanned(기한이 지났고 본문 칸이 비지 않은 행)·
purgeable·purged·skipped_*·remaining_expired_bodies·oldest_remaining_body_crawled_at 등을 남긴다.
"""
import argparse
import logging
import math
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Sequence

logger = logging.getLogger("jobs.retention")

BATCH_SIZE_DEFAULT = 1000
BATCH_SIZE_MAX = 5000
STATEMENT_TIMEOUT_S_DEFAULT = 60
STATEMENT_TIMEOUT_S_MAX = 600
LOCK_TIMEOUT_S = 5

# 기한이 지났고 본문 칸이 NULL이 아닌 행(면제·추출 대기 포함) = scanned
_EXPIRED = "n.raw_news_content IS NOT NULL AND n.raw_news_crawled_at <= %(cutoff)s"
# 기한 면제: 재배포 가능 출처이고 공공누리 유형이 1이거나 적혀 있지 않음
EXEMPT_SQL = "(p.press_name = ANY(%(exempt_presses)s) AND COALESCE(n.raw_news_kogl_type, 1) = 1)"
# 본문 추출기가 아직 고를 수 있는 행 - crawler/content_extractor/extractor.py의 대상 조회와 같은 조건
_AWAITING = (
    "(n.raw_news_extract_status IS NULL OR (n.raw_news_extract_status = ANY(%(retryable)s) "
    "AND n.raw_news_extract_attempts < %(max_attempts)s))"
)
# 비울 대상: 면제가 아니고, 본문이 있거나 추출이 끝난 행
_TARGET = f"(NOT {EXEMPT_SQL} AND (n.raw_news_content <> '' OR NOT {_AWAITING}))"

CUTOFF_SQL = (
    "SELECT now() AT TIME ZONE 'UTC', (now() AT TIME ZONE 'UTC') - make_interval(days => %(days)s)"
)

COUNT_SQL = f"""
SELECT count(*),
       count(*) FILTER (WHERE {_TARGET}),
       count(*) FILTER (WHERE {_TARGET} AND n.raw_news_content <> ''),
       COALESCE(sum(char_length(n.raw_news_content)) FILTER (WHERE {_TARGET}), 0),
       COALESCE(sum(octet_length(n.raw_news_content)) FILTER (WHERE {_TARGET}), 0),
       count(*) FILTER (WHERE {EXEMPT_SQL}),
       count(*) FILTER (WHERE NOT {EXEMPT_SQL} AND n.raw_news_content = '' AND {_AWAITING})
FROM news_raw n JOIN press p ON p.press_id = n.press_id
WHERE {_EXPIRED}
"""

# 해시: 이미 있으면 그대로(본문 추출기가 쓴 content_sha256(정제 본문)), 없으면 같은 식(UTF-8 sha256 hex)으로 만든다.
# 'ok' 행끼리는 해시가 유일해야 하므로(uq_news_raw_content_sha256_ok) 다른 'ok' 행이 이미 그 해시를 갖고 있으면
# 비워 둔다 - 그런 행은 d48994e9d26e 이후 생기지 않지만, 생겨도 본문은 비운다.
_NEW_HASH = "encode(sha256(convert_to(n.raw_news_content, 'UTF8')), 'hex')"
PURGE_BATCH_SQL = f"""
WITH batch AS (
    SELECT n.raw_news_id,
           octet_length(n.raw_news_content) AS bytes,
           n.raw_news_content <> '' AS had_body,
           (n.raw_news_content_sha256 IS NULL AND n.raw_news_content <> '') AS needs_hash
    FROM news_raw n JOIN press p ON p.press_id = n.press_id
    WHERE {_EXPIRED} AND {_TARGET}
    ORDER BY n.raw_news_id
    LIMIT %(batch_size)s
    FOR UPDATE OF n SKIP LOCKED
), purged AS (
    UPDATE news_raw n
    SET raw_news_content_sha256 = CASE
            WHEN n.raw_news_content_sha256 IS NOT NULL THEN n.raw_news_content_sha256
            WHEN n.raw_news_content = '' THEN NULL
            WHEN n.raw_news_extract_status = 'ok' AND EXISTS (
                SELECT 1 FROM news_raw k
                WHERE k.raw_news_extract_status = 'ok' AND k.raw_news_id <> n.raw_news_id
                  AND k.raw_news_content_sha256 = {_NEW_HASH}) THEN NULL
            ELSE {_NEW_HASH}
        END,
        raw_news_content_length = char_length(n.raw_news_content),
        raw_news_content = NULL,
        raw_news_content_purged_at = now()
    FROM batch
    WHERE n.raw_news_id = batch.raw_news_id
    RETURNING n.raw_news_content_length AS chars, n.raw_news_content_sha256 IS NOT NULL AS hashed,
              batch.bytes, batch.had_body, batch.needs_hash
)
SELECT count(*),
       count(*) FILTER (WHERE had_body),
       COALESCE(sum(chars) FILTER (WHERE had_body), 0),
       COALESCE(sum(bytes), 0),
       count(*) FILTER (WHERE needs_hash AND hashed),
       count(*) FILTER (WHERE needs_hash AND NOT hashed)
FROM purged
"""

# 실행 뒤: 면제가 아닌 행에 남은 본문 중 기한이 지난 것과 가장 오래된 것
REMAINING_SQL = f"""
SELECT count(*) FILTER (WHERE n.raw_news_crawled_at <= %(cutoff)s),
       min(n.raw_news_crawled_at)
FROM news_raw n JOIN press p ON p.press_id = n.press_id
WHERE n.raw_news_content IS NOT NULL AND n.raw_news_content <> '' AND NOT {EXEMPT_SQL}
"""


def _bounded_int(name: str, low: int, high: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name}는 정수여야 한다: {value!r}") from None
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"{name}는 {low}~{high} 사이여야 한다: {number}")
        return number

    return parse


def _days_arg(value: str) -> int:
    from config.retention import RetentionConfigError, validate_retention_days

    try:
        return validate_retention_days(value)
    except RetentionConfigError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def add_arguments(parser) -> None:
    parser.add_argument("--days", type=_days_arg, default=None,
                        help="보존 기한(일, 수집 시각 기준). 기본: Settings.BODY_RETENTION_DAYS(30). 허용 7~30")
    parser.add_argument("--dry-run", action="store_true", help="비울 행을 세기만 하고 아무것도 쓰지 않는다")
    parser.add_argument("--batch-size", type=_bounded_int("--batch-size", 1, BATCH_SIZE_MAX),
                        default=BATCH_SIZE_DEFAULT, help=f"한 트랜잭션에서 비울 최대 행 수 (기본 {BATCH_SIZE_DEFAULT})")
    parser.add_argument("--statement-timeout-s", type=_bounded_int("--statement-timeout-s", 1, STATEMENT_TIMEOUT_S_MAX),
                        default=STATEMENT_TIMEOUT_S_DEFAULT,
                        help=f"문장 하나의 시간 제한(초, 기본 {STATEMENT_TIMEOUT_S_DEFAULT}). 락 대기는 {LOCK_TIMEOUT_S}초")


def _begin(cur, statement_timeout_s: int) -> None:
    """문장 시간 제한과 락 대기 한도를 이 트랜잭션에만 건다(psycopg2는 첫 문장에서 트랜잭션을 연다)."""
    cur.execute(f"SET LOCAL statement_timeout = '{int(statement_timeout_s)}s'")
    cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_S}s'")


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def purge_expired_bodies(
    conn,
    *,
    days: int,
    exempt_presses: Sequence[str],
    retryable_statuses: Sequence[str],
    max_extract_attempts: int,
    dry_run: bool = False,
    batch_size: int = BATCH_SIZE_DEFAULT,
    statement_timeout_s: int = STATEMENT_TIMEOUT_S_DEFAULT,
    stats: Optional[Dict[str, Any]] = None,
    on_batch: Optional[Callable[[], None]] = None,
) -> Dict[str, Any]:
    """기한이 지난 본문을 비우고 통계를 돌려준다. conn은 autocommit이 아닌 psycopg2 연결(전용)이다.

    stats를 넘기면 그 dict를 배치마다 갱신한다(잡이 중간에 끊겨도 진행 상황이 job_runs에 남는다)."""
    stats = stats if stats is not None else {}
    stats.update({
        "days": days, "dry_run": dry_run, "batch_size": batch_size,
        "purged": 0, "purged_bodies": 0, "purged_chars": 0, "purged_bytes": 0,
        "hash_filled": 0, "hash_left_null": 0, "batches": 0,
    })
    params: Dict[str, Any] = {
        "days": int(days),
        "exempt_presses": list(exempt_presses),
        "retryable": list(retryable_statuses),
        "max_attempts": int(max_extract_attempts),
        "batch_size": int(batch_size),
    }

    with conn.cursor() as cur:
        _begin(cur, statement_timeout_s)
        cur.execute(CUTOFF_SQL, params)
        now_utc, cutoff = cur.fetchone()
        params["cutoff"] = cutoff
        stats["now_utc"], stats["cutoff_utc"] = _iso(now_utc), _iso(cutoff)
        cur.execute(COUNT_SQL, params)
        (stats["scanned"], stats["purgeable"], stats["purgeable_bodies"], stats["purgeable_chars"],
         stats["purgeable_bytes"], stats["skipped_exempt"], stats["skipped_awaiting_extraction"]) = cur.fetchone()
    conn.commit()
    logger.info(
        f"기준 {stats['cutoff_utc']} UTC 이전 수집분: 본문 칸이 있는 행 {stats['scanned']}건, "
        f"비울 대상 {stats['purgeable']}건(본문 {stats['purgeable_bodies']}건), 면제 {stats['skipped_exempt']}건, "
        f"추출 대기 {stats['skipped_awaiting_extraction']}건{' - dry run' if dry_run else ''}"
    )
    if on_batch:
        on_batch()

    if not dry_run and stats["purgeable"]:
        # 행이 매 배치 대상에서 빠지므로 루프는 끝난다. 그래도 계획보다 크게 돌면 멈춘다(동시에 대상이 계속 생기는 등).
        max_batches = math.ceil(stats["purgeable"] / batch_size) + 2
        started = time.monotonic()
        while stats["batches"] < max_batches:
            with conn.cursor() as cur:
                _begin(cur, statement_timeout_s)
                cur.execute(PURGE_BATCH_SQL, params)
                rows, bodies, chars, nbytes, hash_filled, hash_null = cur.fetchone()
            conn.commit()
            if not rows:
                break
            stats["batches"] += 1
            stats["purged"] += rows
            stats["purged_bodies"] += bodies
            stats["purged_chars"] += int(chars)
            stats["purged_bytes"] += int(nbytes)
            stats["hash_filled"] += hash_filled
            stats["hash_left_null"] += hash_null
            if on_batch:
                on_batch()
        stats["purge_s"] = round(time.monotonic() - started, 3)

    with conn.cursor() as cur:
        _begin(cur, statement_timeout_s)
        cur.execute(REMAINING_SQL, params)
        remaining, oldest = cur.fetchone()
    conn.commit()
    stats["remaining_expired_bodies"] = remaining
    stats["oldest_remaining_body_crawled_at"] = _iso(oldest)
    stats["oldest_remaining_body_age_days"] = (
        round((now_utc - oldest).total_seconds() / 86400, 3) if oldest is not None else None
    )
    return stats


def run(ctx) -> Dict[str, Any]:
    from config.retention import exempt_press_names, validate_retention_days
    from config.settings import Settings
    from crawler.content_extractor.extractor import RETRYABLE_STATUSES
    from db.connection import connect_unpooled

    args = ctx.args
    # --days를 주지 않으면 설정값. 설정값이 범위 밖이면 RetentionConfigError로 실패(알림)한다.
    days = args.days if args.days is not None else validate_retention_days(Settings.BODY_RETENTION_DAYS)
    # 락·실행 기록용 연결(jobs/store.py)과 따로 쓰는 작업 연결. 중간에 끊기면(예외, SIGTERM) 닫는 것으로
    # 진행 중이던 배치 하나만 되돌려지고, 앞 배치들은 이미 커밋돼 있다.
    conn = connect_unpooled(application_name="jobs.run:retention:work")
    try:
        stats = purge_expired_bodies(
            conn,
            days=days,
            exempt_presses=exempt_press_names(),
            retryable_statuses=RETRYABLE_STATUSES,
            max_extract_attempts=Settings.MAX_EXTRACT_ATTEMPTS,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            statement_timeout_s=args.statement_timeout_s,
            stats=ctx.stats,
            on_batch=ctx.checkpoint,
        )
    finally:
        conn.close()

    if not args.dry_run and stats["remaining_expired_bodies"]:
        ctx.warn(
            f"기한({days}일)이 지났는데 본문이 남은 행 {stats['remaining_expired_bodies']}건 - 다른 트랜잭션이 잡고 있던 "
            "행이거나 실행 중에 새로 추출된 행이다. 다음 실행이 비운다. 계속 남으면 runbook-hosting 4.1을 본다"
        )
    if stats.get("hash_left_null"):
        ctx.warn(f"본문 해시를 남기지 못한 'ok' 행 {stats['hash_left_null']}건(같은 해시의 다른 'ok' 행이 있음)")
    logger.info(
        f"{'dry run - ' if args.dry_run else ''}비움 {stats['purged']}건(본문 {stats['purged_bodies']}건, "
        f"{stats['purged_bytes']} bytes), 남은 기한 지난 본문 {stats['remaining_expired_bodies']}건, "
        f"가장 오래된 본문 {stats['oldest_remaining_body_crawled_at']} UTC"
    )
    return {}
