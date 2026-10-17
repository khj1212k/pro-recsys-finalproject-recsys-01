"""scripts/oci/fix_aitimes_created_at_kst.sql(과 되돌리기)을 실제 PostgreSQL에서 돌린다 (ADR 0008, 런북 6.7).

런북과 같은 경로: `psql -X -v ON_ERROR_STOP=1 -v boundary=... [-v apply=1] -f <파일>`. 테스트 DB 안에 스키마 하나를 만들고
public.press / public.news_raw를 `LIKE ... INCLUDING ALL`로 복제한다. 스크립트는 테이블 이름에 스키마를 붙이지 않으므로
PGOPTIONS의 search_path로 그 스키마를 고른다(보관 표도 그 스키마에 생긴다). public에는 아무것도 쓰지 않는다.
"""
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "fix_aitimes_created_at_kst.sql")
REVERT_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "fix_aitimes_created_at_kst_revert.sql")
BOUNDARY = "2026-10-16 20:05:01.826735"
PSQL_SCRIPT_ERROR = 3
NOTICE_RE = re.compile(r"fix_kst fixed=(\d+)")
UTC = timezone.utc


@pytest.fixture(scope="module")
def psql_bin():
    path = shutil.which("psql")
    if not path:
        pytest.skip("psql 클라이언트가 없어 보정 SQL 테스트를 건너뜁니다 (CI integration-test 잡은 설치돼 있는지 확인한다)")
    return path


@pytest.fixture
def schema(pg_conn):
    name = f"fix_kst_{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'CREATE TABLE "{name}".press (LIKE public.press INCLUDING ALL)')
        cur.execute(f'CREATE TABLE "{name}".news_raw (LIKE public.news_raw INCLUDING ALL)')
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


def _news(pg_conn, schema, press_id, key, created_at, crawled_at):
    with pg_conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{schema}".news_raw (press_id, raw_news_title, raw_news_url, raw_news_content, '
            "raw_news_created_at, raw_news_crawled_at) VALUES (%s, %s, %s, '', %s, %s)",
            (press_id, key, f"https://example.com/{schema}/{key}", created_at, crawled_at),
        )


def _created(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT raw_news_title, raw_news_created_at FROM "{schema}".news_raw')
        return dict(cur.fetchall())


def _has_audit(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f'"{schema}".news_raw_created_at_fix_kst',))
        return cur.fetchone()[0] is not None


def _seed(pg_conn, schema):
    """경계 앞 AI타임스 3행(실제 발행 = 수집 1~3시간 전, 저장값은 9시간 뒤로 밀림), 경계 뒤 1행(맞게 저장), 다른 언론사 1행."""
    ai, other = _press(pg_conn, schema, "AI타임스"), _press(pg_conn, schema, "매일경제")
    boundary = datetime.fromisoformat(BOUNDARY)
    truth = {}
    for i, lag_h in enumerate((1, 2, 3)):
        crawled = boundary - timedelta(days=1, hours=i)
        published = (crawled - timedelta(hours=lag_h)).replace(tzinfo=UTC)
        truth[f"old{i}"] = published
        _news(pg_conn, schema, ai, f"old{i}", published + timedelta(hours=9), crawled)
    crawled = boundary + timedelta(hours=2)
    truth["new"] = (crawled - timedelta(minutes=30)).replace(tzinfo=UTC)
    _news(pg_conn, schema, ai, "new", truth["new"], crawled)
    truth["other"] = (boundary - timedelta(hours=5)).replace(tzinfo=UTC)
    _news(pg_conn, schema, other, "other", truth["other"], boundary - timedelta(hours=4))
    return ai, truth


def test_default_run_is_a_rehearsal_that_changes_nothing(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _created(pg_conn, schema)
    done = _run(psql_bin, database_url, schema, FIX_SQL, f"boundary={BOUNDARY}")
    assert done.returncode == 0, done.stderr
    assert NOTICE_RE.search(done.stderr).group(1) == "3"
    assert "ROLLBACK" in done.stdout
    assert _created(pg_conn, schema) == before and not _has_audit(pg_conn, schema)


def test_apply_shifts_only_old_ai_times_rows_once_and_revert_restores_them(psql_bin, database_url, pg_conn, schema):
    _, truth = _seed(pg_conn, schema)
    before = _created(pg_conn, schema)
    done = _run(psql_bin, database_url, schema, FIX_SQL, f"boundary={BOUNDARY}", "apply=1")
    assert done.returncode == 0, done.stderr
    after = _created(pg_conn, schema)
    assert after == truth  # 경계 앞 AI타임스만 9시간 당겨져 실제 발행 시각이 됐다. 경계 뒤·다른 언론사는 그대로
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT count(*), count(DISTINCT boundary_utc) FROM "{schema}".news_raw_created_at_fix_kst')
        assert cur.fetchone() == (3, 1)

    # 다시 돌리면 보관 표가 있어 멈춘다 - 두 번 빼지 않는다
    again = _run(psql_bin, database_url, schema, FIX_SQL, f"boundary={BOUNDARY}", "apply=1")
    assert again.returncode == PSQL_SCRIPT_ERROR and "already exists" in again.stderr
    assert _created(pg_conn, schema) == truth

    rehearsal = _run(psql_bin, database_url, schema, REVERT_SQL)
    assert rehearsal.returncode == 0, rehearsal.stderr
    assert _created(pg_conn, schema) == truth and _has_audit(pg_conn, schema)
    reverted = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert reverted.returncode == 0, reverted.stderr
    assert _created(pg_conn, schema) == before and not _has_audit(pg_conn, schema)


def test_apply_refuses_when_a_row_would_still_be_published_after_it_was_crawled(psql_bin, database_url, pg_conn,
                                                                                schema):
    ai, _ = _seed(pg_conn, schema)
    boundary = datetime.fromisoformat(BOUNDARY)
    # 경계 뒤인데 발행 시각이 수집보다 1시간 뒤 - 이 보정으로 설명되지 않는 행이다
    _news(pg_conn, schema, ai, "odd", (boundary + timedelta(hours=4)).replace(tzinfo=UTC), boundary + timedelta(hours=3))
    before = _created(pg_conn, schema)
    done = _run(psql_bin, database_url, schema, FIX_SQL, f"boundary={BOUNDARY}", "apply=1")
    assert done.returncode == PSQL_SCRIPT_ERROR and "보정 뒤에도 AI타임스 1행" in done.stderr
    assert _created(pg_conn, schema) == before and not _has_audit(pg_conn, schema)


def test_apply_refuses_a_second_shift_when_the_audit_table_was_lost(psql_bin, database_url, pg_conn, schema):
    _, truth = _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, FIX_SQL, f"boundary={BOUNDARY}", "apply=1").returncode == 0
    with pg_conn.cursor() as cur:
        cur.execute(f'DROP TABLE "{schema}".news_raw_created_at_fix_kst')
    done = _run(psql_bin, database_url, schema, FIX_SQL, f"boundary={BOUNDARY}", "apply=1")
    assert done.returncode == PSQL_SCRIPT_ERROR and "한 번 더 뺀 것" in done.stderr
    assert _created(pg_conn, schema) == truth


def test_wrong_boundary_or_missing_boundary_stops(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    early = _run(psql_bin, database_url, schema, FIX_SQL, "boundary=2026-01-01 00:00:00", "apply=1")
    assert early.returncode == PSQL_SCRIPT_ERROR and "0행" in early.stderr
    missing = _run(psql_bin, database_url, schema, FIX_SQL, "apply=1")
    assert missing.returncode == PSQL_SCRIPT_ERROR and "boundary" in missing.stderr
    assert not _has_audit(pg_conn, schema)
    nothing = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert nothing.returncode == PSQL_SCRIPT_ERROR and "보관 표" in nothing.stderr
