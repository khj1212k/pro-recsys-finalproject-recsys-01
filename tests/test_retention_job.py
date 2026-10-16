"""`python -m jobs.run retention`(본문 보존, ADR 0023)의 DB 없이 검증할 수 있는 동작.

가짜 연결이 SQL마다 정해 둔 결과를 돌려준다. 실제 행이 비워지는지·경계·면제·동시 실행은
tests/integration/test_retention_job_db.py가 CI의 Postgres에서 본다.
"""
import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jobs.run import build_parser, main
from jobs.runtime import JobContext
from jobs.tasks import retention

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 26, 17, 50, 0)
CUTOFF = NOW - timedelta(days=30)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = None

    def execute(self, sql, params=None):
        self.conn.log.append((sql, params))
        if sql.startswith("SET LOCAL"):
            self.conn.settings_in_tx.append(sql)
            self._result = None
        elif sql is retention.CUTOFF_SQL:
            self._result = (self.conn.now, self.conn.now - timedelta(days=params["days"]))
        elif sql is retention.COUNT_SQL:
            self._result = self.conn.counts
        elif sql is retention.PURGE_BATCH_SQL:
            self._result = self.conn.batches.pop(0) if self.conn.batches else (0, 0, 0, 0, 0, 0)
        elif sql is retention.REMAINING_SQL:
            self._result = self.conn.remaining
        else:
            raise AssertionError(f"예상하지 못한 SQL: {sql[:80]}")

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """psycopg2 연결 대역. 트랜잭션(커밋 단위)마다 SET LOCAL 두 개가 먼저 오는지 기록한다."""

    def __init__(self, *, counts, batches=(), remaining=(0, None), now=NOW):
        self.counts = counts
        self.batches = list(batches)
        self.remaining = remaining
        self.now = now
        self.log = []
        self.settings_in_tx = []
        self.transactions = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.transactions.append(list(self.settings_in_tx))
        self.settings_in_tx = []

    def rollback(self):
        self.settings_in_tx = []

    def close(self):
        self.closed = True

    def executed(self, sql):
        return [params for s, params in self.log if s is sql]


# scanned, purgeable, purgeable_bodies, purgeable_chars, purgeable_bytes, skipped_exempt, skipped_awaiting
COUNTS = (2600, 2500, 2300, 4_000_000, 9_000_000, 70, 30)


def _purge(conn, **kw):
    defaults = dict(days=30, exempt_presses=["정책브리핑"], retryable_statuses=("fetch_failed", "error"),
                    max_extract_attempts=3)
    defaults.update(kw)
    return retention.purge_expired_bodies(conn, **defaults)


# --------------------------------------------------------------------------- 핵심 루프


def test_dry_run_counts_and_writes_nothing():
    conn = FakeConn(counts=COUNTS, remaining=(2300, CUTOFF - timedelta(days=3)))

    stats = _purge(conn, dry_run=True)

    assert conn.executed(retention.PURGE_BATCH_SQL) == []
    assert (stats["scanned"], stats["purgeable"], stats["purgeable_bodies"]) == (2600, 2500, 2300)
    assert (stats["skipped_exempt"], stats["skipped_awaiting_extraction"]) == (70, 30)
    assert stats["purged"] == 0 and stats["batches"] == 0 and stats["dry_run"] is True
    # dry run이라 기한 지난 본문이 그대로 남아 있다고 보고한다
    assert stats["remaining_expired_bodies"] == 2300
    assert stats["oldest_remaining_body_age_days"] == 33.0


def test_purges_in_bounded_batches_each_committed_with_timeouts():
    batches = [(1000, 950, 1_600_000, 3_800_000, 2, 0), (1000, 900, 1_500_000, 3_600_000, 0, 0),
               (500, 450, 900_000, 1_600_000, 0, 0)]
    conn = FakeConn(counts=COUNTS, batches=batches, remaining=(0, CUTOFF + timedelta(minutes=3)))
    progress = []

    stats = _purge(conn, batch_size=1000, statement_timeout_s=45, on_batch=lambda: progress.append(1))

    assert stats["batches"] == 3
    assert (stats["purged"], stats["purged_bodies"]) == (2500, 2300)
    assert (stats["purged_chars"], stats["purged_bytes"]) == (4_000_000, 9_000_000)
    assert stats["hash_filled"] == 2 and stats["hash_left_null"] == 0
    assert stats["remaining_expired_bodies"] == 0
    assert stats["oldest_remaining_body_crawled_at"] == (CUTOFF + timedelta(minutes=3)).isoformat()
    # 배치 문장은 4번(마지막은 0행으로 끝을 확인). 배치마다 따로 커밋한다.
    purge_calls = conn.executed(retention.PURGE_BATCH_SQL)
    assert len(purge_calls) == 4 and all(p["batch_size"] == 1000 for p in purge_calls)
    # 트랜잭션 = 세기 1 + 배치 4 + 남은 것 확인 1. 모든 트랜잭션이 시간 제한과 락 대기 한도로 시작한다.
    assert len(conn.transactions) == 6
    for tx in conn.transactions:
        assert tx == ["SET LOCAL statement_timeout = '45s'", f"SET LOCAL lock_timeout = '{retention.LOCK_TIMEOUT_S}s'"]
    # 진행 상황을 세기 직후와 배치마다 남긴다(job_runs 중간 기록)
    assert len(progress) == 1 + 3


def test_cutoff_comes_from_the_database_once_and_is_reused_for_every_statement():
    conn = FakeConn(counts=COUNTS, batches=[(10, 10, 100, 300, 0, 0)], now=datetime(2026, 11, 1, 2, 0))

    stats = _purge(conn, days=30)

    expected_cutoff = datetime(2026, 11, 1, 2, 0) - timedelta(days=30)
    assert stats["cutoff_utc"] == expected_cutoff.isoformat()
    assert len(conn.executed(retention.CUTOFF_SQL)) == 1
    for sql in (retention.COUNT_SQL, retention.PURGE_BATCH_SQL, retention.REMAINING_SQL):
        for params in conn.executed(sql):
            assert params["cutoff"] == expected_cutoff
            assert params["exempt_presses"] == ["정책브리핑"]
            assert params["retryable"] == ["fetch_failed", "error"] and params["max_attempts"] == 3


def test_nothing_to_purge_skips_the_batch_loop():
    conn = FakeConn(counts=(5, 0, 0, 0, 0, 5, 0))

    stats = _purge(conn)

    assert conn.executed(retention.PURGE_BATCH_SQL) == []
    assert stats["purged"] == 0 and stats["skipped_exempt"] == 5


def test_loop_is_bounded_even_if_rows_keep_appearing():
    """대상이 계속 생겨도(예: 실행 중 새로 추출된 만료 행) 계획한 배치 수 + 2에서 멈춘다."""
    conn = FakeConn(counts=(10, 10, 10, 1000, 3000, 0, 0), batches=[(5, 5, 50, 150, 0, 0)] * 50)

    stats = _purge(conn, batch_size=5)

    assert stats["batches"] == 2 + 2


# --------------------------------------------------------------------------- 잡(run)과 인자


def _ctx(argv):
    args = build_parser().parse_args(["retention", *argv])
    return JobContext(job="retention", args=args)


def _patch_connection(monkeypatch, conn):
    import db.connection as connection

    opened = []

    def connect_unpooled(application_name=None):
        opened.append(application_name)
        return conn

    monkeypatch.setattr(connection, "connect_unpooled", connect_unpooled)
    return opened


def test_run_uses_settings_default_license_table_and_extractor_retry_rule(monkeypatch):
    from config.settings import Settings

    monkeypatch.setattr(Settings, "BODY_RETENTION_DAYS", 30)
    monkeypatch.setattr(Settings, "MAX_EXTRACT_ATTEMPTS", 4)
    conn = FakeConn(counts=COUNTS, batches=[(2500, 2300, 4_000_000, 9_000_000, 0, 0)])
    opened = _patch_connection(monkeypatch, conn)
    ctx = _ctx([])

    assert retention.run(ctx) == {}

    assert opened == ["jobs.run:retention:work"] and conn.closed
    params = conn.executed(retention.COUNT_SQL)[0]
    assert params["days"] == 30 and params["max_attempts"] == 4
    assert params["exempt_presses"] == ["정책브리핑"]  # config/sources.py의 재배포 가능 출처
    assert params["retryable"] == ["fetch_failed", "error"]  # 본문 추출기의 RETRYABLE_STATUSES
    assert ctx.stats["purged"] == 2500 and ctx.warnings == []


def test_run_warns_when_expired_bodies_remain(monkeypatch):
    conn = FakeConn(counts=COUNTS, batches=[(2400, 2200, 1, 1, 0, 0)], remaining=(100, CUTOFF - timedelta(hours=2)))
    _patch_connection(monkeypatch, conn)
    ctx = _ctx([])

    retention.run(ctx)

    assert len(ctx.warnings) == 1 and "100건" in ctx.warnings[0]


def test_dry_run_does_not_warn_about_remaining_bodies(monkeypatch):
    conn = FakeConn(counts=COUNTS, remaining=(2300, CUTOFF - timedelta(days=1)))
    _patch_connection(monkeypatch, conn)
    ctx = _ctx(["--dry-run"])

    retention.run(ctx)

    assert ctx.warnings == [] and ctx.stats["dry_run"] is True
    assert conn.executed(retention.PURGE_BATCH_SQL) == []


def test_run_closes_the_work_connection_when_a_statement_fails(monkeypatch):
    conn = FakeConn(counts=COUNTS)
    conn.counts = None  # COUNT_SQL 결과를 풀 때 TypeError

    _patch_connection(monkeypatch, conn)
    with pytest.raises(TypeError):
        retention.run(_ctx([]))
    assert conn.closed


def test_settings_value_outside_the_allowed_range_fails_the_run(monkeypatch):
    from config.retention import RetentionConfigError
    from config.settings import Settings

    monkeypatch.setattr(Settings, "BODY_RETENTION_DAYS", 45)
    _patch_connection(monkeypatch, FakeConn(counts=COUNTS))
    with pytest.raises(RetentionConfigError, match="45일"):
        retention.run(_ctx([]))


@pytest.mark.parametrize("argv", [["--days", "6"], ["--days", "31"], ["--days", "x"], ["--batch-size", "0"],
                                  ["--batch-size", "5001"], ["--statement-timeout-s", "601"]])
def test_out_of_range_arguments_are_usage_errors(argv):
    with pytest.raises(SystemExit) as exc:
        main(["retention", *argv])
    assert exc.value.code == 2


def test_argument_defaults():
    args = build_parser().parse_args(["retention"])
    assert args.days is None and args.dry_run is False
    assert args.batch_size == retention.BATCH_SIZE_DEFAULT == 1000
    assert args.statement_timeout_s == 60
    assert build_parser().parse_args(["retention", "--days", "7"]).days == 7


# --------------------------------------------------------------------------- 설정과 출처


def test_retention_days_bounds():
    from config.retention import (BODY_RETENTION_MAX_DAYS, BODY_RETENTION_MIN_DAYS, RetentionConfigError,
                                  validate_retention_days)

    assert (BODY_RETENTION_MIN_DAYS, BODY_RETENTION_MAX_DAYS) == (7, 30)
    assert validate_retention_days("30") == 30 and validate_retention_days(7) == 7
    for bad in (6, 31, None, "삼십"):
        with pytest.raises(RetentionConfigError):
            validate_retention_days(bad)


def test_only_redistributable_sources_are_exempt():
    from config.retention import exempt_press_names
    from config.sources import POLICY_BRIEFING_PRESS, SOURCE_LICENSES, ALL_RIGHTS_RESERVED

    assert exempt_press_names() == [POLICY_BRIEFING_PRESS]
    # 표에 없는 출처는 면제 목록에 들어오지 않는다(fail closed) - 새 출처를 더해도 재배포 가능으로 판정하기 전까지는 기한이 걸린다
    assert exempt_press_names({**SOURCE_LICENSES, "새 언론사": ALL_RIGHTS_RESERVED}) == [POLICY_BRIEFING_PRESS]


def test_settings_default_is_thirty_days():
    from config.settings import Settings

    assert Settings.BODY_RETENTION_DAYS == 30


# --------------------------------------------------------------------------- SQL 계약 (의미는 integration이 확인)


def _assigned_columns(sql):
    """UPDATE ... SET 절에서 값을 쓰는 열 이름들."""
    set_clause = sql.split("UPDATE news_raw n", 1)[1].split("FROM batch", 1)[0]
    return set_clause, set(re.findall(r"(?m)^\s*(?:SET\s+)?([a-z_0-9]+) = ", set_clause))


def test_purge_statement_blanks_only_the_body_and_keeps_identity_columns():
    sql = retention.PURGE_BATCH_SQL
    set_clause, assigned = _assigned_columns(sql)
    # 쓰는 열은 이 넷뿐이다 - 제목·URL·언론사·임베딩·추출 상태·뉴스레터 연결·수집 시각은 그대로 남는다
    assert assigned == {"raw_news_content_sha256", "raw_news_content_length", "raw_news_content",
                        "raw_news_content_purged_at"}
    assert "raw_news_content = NULL" in set_clause
    assert "raw_news_content_length = char_length(n.raw_news_content)" in set_clause
    assert "raw_news_content_purged_at = now()" in set_clause
    assert "WHEN n.raw_news_content_sha256 IS NOT NULL THEN n.raw_news_content_sha256" in set_clause
    assert "DELETE" not in sql.upper()
    # 배치: id 순서, 상한, 다른 트랜잭션이 잡은 행은 기다리지 않고 건너뜀
    assert "ORDER BY n.raw_news_id" in sql and "LIMIT %(batch_size)s" in sql
    assert "FOR UPDATE OF n SKIP LOCKED" in sql


def test_expiry_is_computed_in_utc_independent_of_the_session_time_zone():
    assert "now() AT TIME ZONE 'UTC'" in retention.CUTOFF_SQL
    assert "make_interval(days => %(days)s)" in retention.CUTOFF_SQL
    # 만료와 같은 순간부터 만료(<=) - 반출 도구의 `expires_at <= now`와 같은 경계
    assert "n.raw_news_crawled_at <= %(cutoff)s" in retention.COUNT_SQL
    assert "n.raw_news_crawled_at <= %(cutoff)s" in retention.PURGE_BATCH_SQL


def test_awaiting_extraction_rule_matches_the_extractor_selection():
    """보존 잡이 건너뛰는 "추출 대기" 조건은 본문 추출기가 대상을 고르는 조건과 같아야 한다 - 다르면 두 잡이 같은
    행을 두고 다툰다(실제 행으로는 integration 테스트가 확인한다)."""
    import inspect

    from crawler.content_extractor import extractor

    source = inspect.getsource(extractor.ContentExtractor.extract_parallel_with_stats)
    assert "N.raw_news_extract_status IS NULL" in source
    assert "N.raw_news_extract_status IN ('fetch_failed', 'error')" in source
    assert "N.raw_news_extract_attempts < %s" in source
    assert extractor.RETRYABLE_STATUSES == ("fetch_failed", "error")
    assert "n.raw_news_extract_status IS NULL" in retention.COUNT_SQL
    assert "n.raw_news_extract_attempts < %(max_attempts)s" in retention.COUNT_SQL


# --------------------------------------------------------------------------- 스케줄


def _cron_jobs(path):
    lines = [l for l in (REPO_ROOT / path).read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    out = []
    for line in lines:
        fields = line.split()
        assert fields[5:8] == ["python", "-m", "jobs.run"], line
        out.append((fields[:5], fields[8:]))
    return out


@pytest.mark.parametrize("path", ["docker/crontab", "docker/crontab.micro"])
def test_crontabs_run_retention_daily_off_the_ingest_minute(path):
    jobs = _cron_jobs(path)
    parser = build_parser()
    for _schedule, argv in jobs:
        parser.parse_args(argv)  # 모든 줄이 등록된 잡과 유효한 인자다
    retention_lines = [schedule for schedule, argv in jobs if argv[0] == "retention"]
    assert retention_lines == [["50", "17", "*", "*", "*"]]  # 매일 17:50 UTC = 02:50 KST
    # 다른 잡과 같은 시각에 시작하지 않는다(매시 잡은 분, 하루 한 번 잡은 시·분)
    for schedule, argv in jobs:
        if argv[0] == "retention":
            continue
        minute, hour = schedule[:2]
        assert not (minute == "50" and hour in ("*", "17")), (schedule, argv)


def test_micro_overlay_lets_the_operator_disable_a_scheduled_job_without_a_rebuild():
    text = (REPO_ROOT / "docker" / "compose.micro.yaml").read_text(encoding="utf-8")
    assert re.search(r"JOBS_DISABLED: \$\{SCHEDULER_JOBS_DISABLED:-\}", text)


# --------------------------------------------------------------------------- 일일 리포트의 보존 상태


def _report(retention_status):
    return {
        "job_runs": {"retention": {"succeeded": 1}},
        "news_raw_24h": {"total": 0, "ok": 0, "dropped": 0, "duplicate": 0, "empty": 0, "fetch_failed": 0,
                         "error": 0, "embedded": 0, "per_press": {}},
        "totals": {"news_raw": 0, "embedded": 0, "newsletters_24h": 0},
        "retention": retention_status,
    }


def test_daily_report_shows_retention_state_and_flags_an_overdue_purge():
    from jobs.tasks.daily_report import format_report, retention_warning

    ok = {"days": 30, "expired_bodies": 412, "overdue_bodies": 0, "purged_24h": 1300, "purged_total": 9100,
          "oldest_body_crawled_at": "2026-10-01 18:02"}
    text = format_report(_report(ok))
    assert "본문 보존(30일): 기한 지난 본문 412건(하루 넘게 밀린 것 0건)" in text
    assert "누적 9100건" in text and "2026-10-01 18:02 UTC" in text
    # 만료된 지 하루가 안 된 본문은 다음 실행이 비운다 - 경고하지 않는다
    assert retention_warning(ok) is None

    stuck = dict(ok, overdue_bodies=37)
    warning = retention_warning(stuck)
    assert warning and "37건" in warning and "retention" in warning
    assert retention_warning(None) is None


def test_daily_report_without_retention_state_keeps_the_old_layout():
    from jobs.tasks.daily_report import format_report

    report = _report(None)
    del report["retention"]
    assert "본문 보존" not in format_report(report)
