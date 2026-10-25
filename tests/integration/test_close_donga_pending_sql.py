"""scripts/oci/close_donga_pending.sql(과 되돌리기)을 실제 PostgreSQL에서 돌린다 (ADR 0026 2026-10-25 C-31, 런북 6.9).

fix_aitimes_created_at_kst_sql 테스트와 같은 방식: 테스트 DB 안에 스키마를 만들고 press / news_raw를 복제한 뒤
PGOPTIONS의 search_path로 그 스키마를 고른다. public에는 쓰지 않는다. 지어낸 제목만 쓴다.
"""
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLOSE_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "close_donga_pending.sql")
REVERT_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "close_donga_pending_revert.sql")
PSQL_SCRIPT_ERROR = 3
NOTICE_RE = re.compile(r"close_donga closed=(\d+)")
REVERT_NOTICE_RE = re.compile(r"close_donga_revert restore=(\d+) purged=(\d+)")
NOW = datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def psql_bin():
    path = shutil.which("psql")
    if not path:
        pytest.skip("psql 클라이언트가 없어 SQL 테스트를 건너뜁니다")
    return path


@pytest.fixture
def schema(pg_conn):
    name = f"close_donga_{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'CREATE TABLE "{name}".press (LIKE public.press INCLUDING ALL)')
        cur.execute(f'CREATE TABLE "{name}".news_raw (LIKE public.news_raw INCLUDING ALL)')
        cur.execute(f'CREATE TABLE "{name}".job_runs (LIKE public.job_runs INCLUDING ALL)')
    try:
        yield name
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def _run(psql_bin, database_url, schema, sql_file, *variables):
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema}", "PGCLIENTENCODING": "UTF8"}
    args = [psql_bin, "-X", "-v", "ON_ERROR_STOP=1"]
    for v in variables:
        args += ["-v", v]
    return subprocess.run([*args, "-f", sql_file, database_url], capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=120)


def _press(pg_conn, schema, name):
    with pg_conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{schema}".press (press_name) VALUES (%s) RETURNING press_id', (name,))
        return cur.fetchone()[0]


def _news(pg_conn, schema, press_id, key, content, status, attempts):
    with pg_conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{schema}".news_raw (press_id, raw_news_title, raw_news_url, raw_news_content, '
            "raw_news_created_at, raw_news_crawled_at, raw_news_extract_status, raw_news_extract_attempts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (press_id, key, f"https://example.com/{schema}/{key}", content, NOW - timedelta(hours=2),
             (NOW - timedelta(hours=1)).replace(tzinfo=None), status, attempts),
        )


def _state(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT raw_news_title, raw_news_extract_status, raw_news_extract_attempts FROM "{schema}".news_raw')
        return {t: (s, a) for t, s, a in cur.fetchall()}


def _has_audit(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f'"{schema}".news_raw_donga_close_pending',))
        return cur.fetchone()[0] is not None


def _job_run(pg_conn, schema, job, status):
    with pg_conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{schema}".job_runs (job, status) VALUES (%s, %s)', (job, status))


def _seed(pg_conn, schema):
    donga, other = _press(pg_conn, schema, "동아일보"), _press(pg_conn, schema, "경향신문")
    _news(pg_conn, schema, donga, "pending_null", "", None, 0)          # 닫는다
    _news(pg_conn, schema, donga, "pending_retry", "", "fetch_failed", 1)  # 닫는다
    _news(pg_conn, schema, donga, "final_failed", "", "fetch_failed", 3)   # 이미 끝남 - 그대로
    _news(pg_conn, schema, donga, "ok", "지어낸 본문", "ok", 1)             # 본문 있음 - 그대로
    _news(pg_conn, schema, donga, "summary", "지어낸 요약", "rss_summary", 0)  # 요약으로 닫힘 - 그대로
    _news(pg_conn, schema, other, "other_null", "", None, 0)            # 다른 언론사 - 그대로


def test_default_run_is_a_rehearsal(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _state(pg_conn, schema)
    done = _run(psql_bin, database_url, schema, CLOSE_SQL)
    assert done.returncode == 0, done.stderr
    assert NOTICE_RE.search(done.stderr).group(1) == "2"
    assert "ROLLBACK" in done.stdout
    assert _state(pg_conn, schema) == before and not _has_audit(pg_conn, schema)


def test_apply_closes_only_pending_donga_rows_once_and_revert_restores(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _state(pg_conn, schema)
    done = _run(psql_bin, database_url, schema, CLOSE_SQL, "apply=1")
    assert done.returncode == 0, done.stderr
    after = _state(pg_conn, schema)
    assert after["pending_null"] == ("blocked_origin", 0)
    assert after["pending_retry"] == ("blocked_origin", 1)
    for key in ("final_failed", "ok", "summary", "other_null"):
        assert after[key] == before[key]

    again = _run(psql_bin, database_url, schema, CLOSE_SQL, "apply=1")
    assert again.returncode == PSQL_SCRIPT_ERROR and "already exists" in again.stderr
    assert _state(pg_conn, schema) == after

    rehearsal = _run(psql_bin, database_url, schema, REVERT_SQL)
    assert rehearsal.returncode == 0, rehearsal.stderr
    assert _state(pg_conn, schema) == after and _has_audit(pg_conn, schema)
    reverted = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert reverted.returncode == 0, reverted.stderr
    assert _state(pg_conn, schema) == before and not _has_audit(pg_conn, schema)


def test_revert_refuses_when_a_closed_row_changed_since(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, CLOSE_SQL, "apply=1").returncode == 0
    with pg_conn.cursor() as cur:
        cur.execute(f"""UPDATE "{schema}".news_raw SET raw_news_extract_status = 'ok' WHERE raw_news_title = 'pending_null'""")
    refused = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert refused.returncode == PSQL_SCRIPT_ERROR
    assert _has_audit(pg_conn, schema)


def test_close_stops_while_an_ingest_is_running_and_proceeds_when_only_other_jobs_run(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _state(pg_conn, schema)
    _job_run(pg_conn, schema, "ingest", "running")
    refused = _run(psql_bin, database_url, schema, CLOSE_SQL, "apply=1")
    assert refused.returncode == PSQL_SCRIPT_ERROR and "ingest가 돌고 있다" in refused.stderr
    assert _state(pg_conn, schema) == before and not _has_audit(pg_conn, schema)

    with pg_conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{schema}".job_runs')
    _job_run(pg_conn, schema, "ingest", "succeeded")   # 끝난 ingest
    _job_run(pg_conn, schema, "ingest", "abandoned")   # 죽은 ingest
    _job_run(pg_conn, schema, "popularity", "running")  # 다른 잡
    ok = _run(psql_bin, database_url, schema, CLOSE_SQL, "apply=1")
    assert ok.returncode == 0, ok.stderr
    assert NOTICE_RE.search(ok.stderr).group(1) == "2"


def test_revert_stops_while_an_ingest_is_running(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, CLOSE_SQL, "apply=1").returncode == 0
    closed = _state(pg_conn, schema)
    _job_run(pg_conn, schema, "ingest", "running")
    refused = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert refused.returncode == PSQL_SCRIPT_ERROR and "ingest가 돌고 있다" in refused.stderr
    assert _state(pg_conn, schema) == closed and _has_audit(pg_conn, schema)


def test_revert_after_retention_restores_only_the_status_and_reports_the_purged_rows(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, CLOSE_SQL, "apply=1").returncode == 0
    with pg_conn.cursor() as cur:  # 보존 잡이 닫은 행 하나의 본문 칸을 비웠다(ADR 0023)
        cur.execute(
            f"""UPDATE "{schema}".news_raw SET raw_news_content = NULL, raw_news_content_length = 0,
                raw_news_content_purged_at = now() WHERE raw_news_title = 'pending_retry'"""
        )
    rehearsal = _run(psql_bin, database_url, schema, REVERT_SQL)
    assert rehearsal.returncode == 0, rehearsal.stderr
    assert REVERT_NOTICE_RE.search(rehearsal.stderr).groups() == ("2", "1")

    reverted = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert reverted.returncode == 0, reverted.stderr
    assert _state(pg_conn, schema)["pending_retry"] == ("fetch_failed", 1)  # 상태·시도 횟수는 돌아온다
    with pg_conn.cursor() as cur:  # 비운 칸은 돌아오지 않는다
        cur.execute(
            f"""SELECT raw_news_content, raw_news_content_purged_at IS NOT NULL FROM "{schema}".news_raw
                WHERE raw_news_title = 'pending_retry'"""
        )
        assert cur.fetchone() == (None, True)
