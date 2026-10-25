"""scripts/oci/fix_kmib_body_extraction.sql(과 되돌리기)을 실제 PostgreSQL에서 돌린다 (ADR 0026 결정 9, 런북 6.8).

런북과 같은 경로: `psql -X -v ON_ERROR_STOP=1 -v boundary=... -v until=... [-v apply=1] -f <파일>`. 테스트 DB 안에 스키마 하나를
만들고 public.press / public.news_raw를 `LIKE ... INCLUDING ALL`로 복제한다(CHECK 제약과 'ok' 부분 unique 인덱스도 따라온다).
스크립트는 테이블 이름에 스키마를 붙이지 않으므로 PGOPTIONS의 search_path로 그 스키마를 고른다(보관 표도 그 스키마에 생긴다).
public에는 아무것도 쓰지 않는다. 본문은 모두 지어낸 짧은 글이다(ADR 0023).
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "fix_kmib_body_extraction.sql")
REVERT_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "fix_kmib_body_extraction_revert.sql")
PSQL_SCRIPT_ERROR = 3
FIX_NOTICE = re.compile(r"fix_kmib reset=(\d+) ok=(\d+) duplicate=(\d+) dropped=(\d+) empty=(\d+)")
REVERT_NOTICE = re.compile(
    r"fix_kmib_revert restored=(\d+) left_unattempted_ok=(\d+) already_reextracted=(\d+) missing_from_news_raw=(\d+)")
UTC = timezone.utc
KMIB, OTHER = "국민일보", "세계일보"


@pytest.fixture(scope="module")
def psql_bin():
    path = shutil.which("psql")
    if not path:
        pytest.skip("psql 클라이언트가 없어 복구 SQL 테스트를 건너뜁니다 (CI integration-test 잡은 설치돼 있는지 확인한다)")
    return path


@pytest.fixture
def schema(pg_conn):
    name = f"fix_kmib_{uuid.uuid4().hex[:8]}"
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


def _command(psql_bin, database_url, schema, sql_file, variables, timezone_name="UTC", extra_env=None):
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema} -c timezone={timezone_name}", "PGCLIENTENCODING": "UTF8",
           **(extra_env or {})}
    args = [psql_bin, "-X", "-v", "ON_ERROR_STOP=1"]
    for v in variables:
        args += ["-v", v]
    return [*args, "-f", sql_file, database_url], env


def _run(psql_bin, database_url, schema, sql_file, *variables, timezone_name="UTC"):
    args, env = _command(psql_bin, database_url, schema, sql_file, variables, timezone_name)
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)


def _utc(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S+00")


NOW = datetime.now(UTC).replace(microsecond=0)
BOUNDARY = NOW - timedelta(days=5)  # 붕괴 시작 직전
UNTIL = NOW - timedelta(days=1)  # 고친 추출기의 첫 ingest
BOUNDARY_ARG = f"boundary={_utc(BOUNDARY)}"
UNTIL_ARG = f"until={_utc(UNTIL)}"


def _press(pg_conn, schema, name):
    with pg_conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{schema}".press (press_name) VALUES (%s) RETURNING press_id', (name,))
        return cur.fetchone()[0]


def _news(pg_conn, schema, press_id, key, status, extracted_at, sha=None, length=None, content="", attempts=1,
          crawled_days_ago=4, purged=False, letter_id=None, embedded=False):
    crawled = (NOW - timedelta(days=crawled_days_ago)).replace(tzinfo=None)  # 시간대 없는 UTC
    with pg_conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{schema}".news_raw (press_id, raw_news_title, raw_news_url, raw_news_content, '
            "raw_news_crawled_at, raw_news_extract_status, raw_news_extracted_at, raw_news_content_sha256, "
            "raw_news_content_length, raw_news_extract_attempts, raw_news_content_purged_at, news_letter_id, "
            "embedding_result) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "CASE WHEN %s THEN ('[' || repeat('0.1,', 1023) || '0.1]')::vector ELSE NULL END)",
            (press_id, key, f"https://example.com/{schema}/{key}", content, crawled, status, extracted_at, sha, length,
             attempts, NOW if purged else None, letter_id, embedded),
        )


def _job_run(pg_conn, schema, job, started_at, stats):
    with pg_conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{schema}".job_runs (job, started_at, finished_at, status, stats) '
            "VALUES (%s, %s, %s, 'succeeded', %s::jsonb)",
            (job, started_at, started_at + timedelta(minutes=3), json.dumps(stats)))


FIXED_STATS = {"extract": {"targets": 3, "body_container": {}}}  # 고친 추출기만 body_container 키를 남긴다(비어 있어도 있다)
OLD_STATS = {"extract": {"targets": 3}}


def _seed_job_runs(pg_conn, schema):
    """옛 추출기로 돈 ingest(붕괴 기간) -> 고친 추출기의 첫 ingest(= UNTIL)와 그 뒤 ingest. 가드가 세지 않는 실행도 둔다."""
    _job_run(pg_conn, schema, "ingest", BOUNDARY + timedelta(minutes=30), OLD_STATS)
    _job_run(pg_conn, schema, "ingest", UNTIL, FIXED_STATS)
    _job_run(pg_conn, schema, "ingest", NOW - timedelta(hours=1), FIXED_STATS)
    _job_run(pg_conn, schema, "ingest", UNTIL + timedelta(minutes=20), {"rss": {"new": 1}})  # extract 단계가 없다 - 세지 않는다
    _job_run(pg_conn, schema, "embed", UNTIL + timedelta(minutes=25), OLD_STATS)  # ingest가 아니다 - 세지 않는다


def _seed(pg_conn, schema):
    """붕괴 기간(BOUNDARY ~ UNTIL)의 국민일보 행 9개(되돌릴 행)와 건드리면 안 되는 행들."""
    _seed_job_runs(pg_conn, schema)
    kmib, other = _press(pg_conn, schema, KMIB), _press(pg_conn, schema, OTHER)
    inside = lambda hours: BOUNDARY + timedelta(hours=hours)  # noqa: E731
    junk = "특집 글을 뽑은 지어낸 본문"
    # --- 되돌릴 행 9개: ok 3 / duplicate 4 / dropped 1 / empty 1
    _news(pg_conn, schema, kmib, "ok1", "ok", inside(1), "h1", len(junk), junk)
    _news(pg_conn, schema, kmib, "ok2", "ok", inside(2), "h2", len(junk), junk)
    _news(pg_conn, schema, kmib, "ok3", "ok", inside(3), "h3", len(junk), junk)
    for key, sha, hours in (("dup1", "h1", 2), ("dup2", "h1", 2), ("dup3", "h2", 2), ("dup4", "h3", 3)):
        _news(pg_conn, schema, kmib, key, "duplicate", inside(hours), sha, 0, "")
    _news(pg_conn, schema, kmib, "dropped1", "dropped", inside(3), None, 0, "")
    _news(pg_conn, schema, kmib, "empty1", "empty", inside(3), None, 0, "")
    # --- 건드리면 안 되는 행
    _news(pg_conn, schema, kmib, "before_ok", "ok", BOUNDARY - timedelta(hours=1), "hb", 6, "정상 본문")  # 붕괴 전
    # 붕괴 전 템플릿으로 받은 같은 글(before_ok)을 옛 추출기가 정확히 중복으로 잡은 행 - 구 URL·신 URL 이중 수집
    _news(pg_conn, schema, kmib, "dup_of_before_ok", "duplicate", inside(2), "hb", 0, "")
    _news(pg_conn, schema, kmib, "after_until_ok", "ok", UNTIL + timedelta(hours=1), "ha", 6, "고친 뒤 본문")  # 고친 추출기가 받음
    _news(pg_conn, schema, kmib, "purged_ok", "ok", inside(4), "hp", 99, None, purged=True)  # 본문을 이미 비움
    _news(pg_conn, schema, kmib, "expired_ok", "ok", inside(4), "he", 6, "기한 지남", crawled_days_ago=40)  # 수집 30일 넘음
    _news(pg_conn, schema, kmib, "failed1", "fetch_failed", inside(4), None, 0, "", attempts=2)
    _news(pg_conn, schema, kmib, "pending1", None, None, None, None, "", attempts=0)
    _news(pg_conn, schema, other, "other_ok", "ok", inside(1), "oh1", 6, "다른 언론사")
    _news(pg_conn, schema, other, "other_dup", "duplicate", inside(1), "oh1", 0, "")
    return kmib, other


RESET_KEYS = {"ok1", "ok2", "ok3", "dup1", "dup2", "dup3", "dup4", "dropped1", "empty1"}


def _rows(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT raw_news_title, raw_news_extract_status, raw_news_content_sha256, raw_news_content_length, "
            "raw_news_extract_attempts, raw_news_extracted_at, raw_news_content, raw_news_content_purged_at "
            f'FROM "{schema}".news_raw ORDER BY raw_news_id')
        return {r[0]: r[1:] for r in cur.fetchall()}


def _has_audit(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f'"{schema}".news_raw_kmib_extract_fix',))
        return cur.fetchone()[0] is not None


UNATTEMPTED = (None, None, None, 0, None, "", None)  # 상태, 해시, 길이, 시도 횟수, 추출 시각, 본문, 비운 시각


def test_default_run_is_a_rehearsal_that_changes_nothing(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG)

    assert done.returncode == 0, done.stderr
    assert FIX_NOTICE.search(done.stderr).groups() == ("9", "3", "4", "1", "1")
    assert "ROLLBACK" in done.stdout
    assert _rows(pg_conn, schema) == before and not _has_audit(pg_conn, schema)


def test_apply_resets_only_the_broken_window_rows_and_keeps_their_old_metadata(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1")

    assert done.returncode == 0, done.stderr
    assert "COMMIT" in done.stdout
    after = _rows(pg_conn, schema)
    for key in RESET_KEYS:  # 수집기가 막 넣은 행과 같은 값
        assert after[key][0:4] == (None, None, None, 0) and after[key][4:] == (None, "", None), key
    for key in set(before) - RESET_KEYS:
        assert after[key] == before[key], key  # 붕괴 전·고친 뒤·비운 행·30일 넘은 행·다운로드 실패·다른 언론사는 그대로
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT n.raw_news_title, f.extract_status_before, f.content_sha256_before, f.content_length_before, "
            "f.extract_attempts_before, f.press_name, f.window_start, f.window_end "
            f'FROM "{schema}".news_raw_kmib_extract_fix f JOIN "{schema}".news_raw n USING (raw_news_id)')
        audit = {r[0]: r[1:] for r in cur.fetchall()}
    assert set(audit) == RESET_KEYS
    assert audit["dup1"][:5] == ("duplicate", "h1", 0, 1, KMIB)
    assert audit["ok2"][:5] == ("ok", "h2", len("특집 글을 뽑은 지어낸 본문"), 1, KMIB)
    assert audit["ok1"][5] == BOUNDARY and audit["ok1"][6] == UNTIL


def _picked_by_the_extractor(monkeypatch, database_url, schema):
    """실제 본문 추출기의 대상 조회(crawler/content_extractor/extractor.py)를 이 스키마에서 돌려 다음 ingest가 고를 기사의 제목을
    돌려준다. 쿼리를 손으로 옮겨 적지 않는다 - 추출기의 조건이 바뀌면 이 테스트도 따라 바뀐다. 기사를 내려받지 않도록 기사
    처리만 가짜로 바꾼다."""
    import psycopg2
    from config.settings import Settings
    from crawler.content_extractor import extractor
    from crawler.content_extractor.extractor import ContentExtractor, ExtractionResult

    picked = []

    def fake_process(article):
        picked.append(article[3])  # (raw_news_id, url, press_name, title)
        return ExtractionResult(article[0], article[2], "fetch_failed")

    monkeypatch.setattr(Settings, "MAX_EXTRACT_ATTEMPTS", 3)
    monkeypatch.setattr(extractor, "get_connection",
                        lambda: psycopg2.connect(database_url, options=f"-c search_path={schema}"))
    monkeypatch.setattr(extractor, "release_connection", lambda conn: conn.close())
    monkeypatch.setattr(ContentExtractor, "process_single_article", staticmethod(fake_process))
    ContentExtractor().extract_parallel_with_stats(num_workers=1)
    return set(picked)


def test_reset_rows_are_exactly_what_the_extractor_picks_up_next(psql_bin, database_url, pg_conn, schema, monkeypatch):
    """복구한 행은 모두 다음 ingest가 고르고(실제 추출기의 대상 조회), 이미 끝난 행은 고르지 않는다.
    스크립트의 30일은 config.retention.BODY_RETENTION_DAYS(ADR 0023)와 같아야 한다."""
    from config.retention import BODY_RETENTION_DAYS

    assert f"interval '{BODY_RETENTION_DAYS} days'" in open(FIX_SQL, encoding="utf-8").read()
    _seed(pg_conn, schema)
    assert _picked_by_the_extractor(monkeypatch, database_url, schema) == {"pending1", "failed1"}  # 복구 전: 원래 대기 중인 행뿐

    assert _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1").returncode == 0

    # 복구한 9행 + 원래 대기 중이던 pending1 + 시도 여지가 남은 failed1(attempts=2 < MAX_EXTRACT_ATTEMPTS)
    assert _picked_by_the_extractor(monkeypatch, database_url, schema) == RESET_KEYS | {"pending1", "failed1"}


def test_boundary_without_an_offset_is_read_as_utc_even_from_a_kst_session(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    naive = [f"boundary={BOUNDARY:%Y-%m-%d %H:%M:%S}", f"until={UNTIL:%Y-%m-%d %H:%M:%S}"]

    done = _run(psql_bin, database_url, schema, FIX_SQL, *naive, timezone_name="Asia/Seoul")

    assert done.returncode == 0, done.stderr
    assert FIX_NOTICE.search(done.stderr).group(1) == "9"


def test_second_apply_stops_while_the_audit_table_exists(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1").returncode == 0
    after_first = _rows(pg_conn, schema)

    again = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1")

    assert again.returncode == PSQL_SCRIPT_ERROR and "already exists" in again.stderr
    assert _rows(pg_conn, schema) == after_first


def test_apply_stops_on_empty_wide_or_linked_targets_and_missing_arguments(psql_bin, database_url, pg_conn, schema):
    kmib, _ = _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    # 고친 추출기의 ingest(NOW-1h)가 until 뒤에 있어 가드는 통과하고, 이 창(UNTIL+2h ~ +3h)에는 추출된 행이 없다
    nothing = _run(psql_bin, database_url, schema, FIX_SQL, f"boundary={_utc(UNTIL + timedelta(hours=2))}",
                   f"until={_utc(UNTIL + timedelta(hours=3))}", "apply=1")
    assert nothing.returncode == PSQL_SCRIPT_ERROR and "0행" in nothing.stderr

    wide = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "max_rows=8", "apply=1")
    assert wide.returncode == PSQL_SCRIPT_ERROR and "max_rows=8" in wide.stderr

    no_boundary = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1")
    assert no_boundary.returncode == PSQL_SCRIPT_ERROR and "boundary" in no_boundary.stderr
    no_until = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, "apply=1")
    assert no_until.returncode == PSQL_SCRIPT_ERROR and "until" in no_until.stderr

    _news(pg_conn, schema, kmib, "letter_ok", "ok", BOUNDARY + timedelta(hours=5), "hl", 6, "본문", letter_id=7)
    linked = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1")
    assert linked.returncode == PSQL_SCRIPT_ERROR and "임베딩이 있거나 뉴스레터" in linked.stderr
    with pg_conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{schema}".news_raw WHERE raw_news_title = %s', ("letter_ok",))
    _news(pg_conn, schema, kmib, "embedded_ok", "ok", BOUNDARY + timedelta(hours=5), "he2", 6, "본문", embedded=True)
    embedded = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1")
    assert embedded.returncode == PSQL_SCRIPT_ERROR and "임베딩이 있거나 뉴스레터" in embedded.stderr

    with pg_conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{schema}".news_raw WHERE raw_news_title = %s', ("embedded_ok",))
    assert _rows(pg_conn, schema) == before and not _has_audit(pg_conn, schema)  # 어느 실패도 아무것도 커밋하지 않았다


def test_duplicate_of_an_ok_row_from_before_the_boundary_is_not_a_target(psql_bin, database_url, pg_conn, schema):
    """구 URL·신 URL로 이중 수집된 기사를 옛 추출기가 정확히 중복으로 잡은 행은 정상이다. 되돌려 다시 받으면 새 템플릿 추출이
    붕괴 전 추출과 글자까지 같다는 보장이 없어 같은 기사가 ok 두 벌로 풀릴 수 있다. 특집 글 해시의 duplicate(원본이 boundary
    이후)는 그대로 대상이다."""
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1")

    assert done.returncode == 0, done.stderr
    assert FIX_NOTICE.search(done.stderr).groups() == ("9", "3", "4", "1", "1")  # duplicate 4행 = dup1~4. dup_of_before_ok는 아니다
    after = _rows(pg_conn, schema)
    assert after["dup_of_before_ok"] == before["dup_of_before_ok"] and before["dup_of_before_ok"][0] == "duplicate"
    assert after["dup1"][0] is None  # 같은 duplicate라도 원본(ok1)이 붕괴 기간이면 되돌린다


def test_apply_refuses_until_the_fixed_extractor_has_run_the_ingest_after_until(psql_bin, database_url, pg_conn, schema):
    """옛 추출기가 도는 채로 되돌리면 다음 ingest가 되돌린 행을 같은 특집 글로 다시 채운다. 고친 추출기가 배포됐는지는 job_runs의
    stats.extract.body_container(고친 추출기만 남기는 키)로 확인한다."""
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    future = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, f"until={_utc(NOW + timedelta(hours=1))}",
                  "apply=1")
    assert future.returncode == PSQL_SCRIPT_ERROR and "보다 뒤다" in future.stderr

    # until 뒤에 ingest 실행이 없다 = 고친 추출기를 아직 배포하지 않았거나 until이 틀렸다
    not_yet = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, f"until={_utc(NOW - timedelta(minutes=30))}",
                   "apply=1")
    assert not_yet.returncode == PSQL_SCRIPT_ERROR and "고친 추출기" in not_yet.stderr and "ingest가 없다" in not_yet.stderr

    # until 뒤에 옛 추출기로 돈 ingest가 있다 = until이 첫 ingest보다 이르거나 옛 추출기로 되돌렸다
    _job_run(pg_conn, schema, "ingest", NOW - timedelta(hours=2), OLD_STATS)
    old_after = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1")
    assert old_after.returncode == PSQL_SCRIPT_ERROR and "옛 추출기로 돈 ingest가 1회" in old_after.stderr

    assert _rows(pg_conn, schema) == before and not _has_audit(pg_conn, schema)  # 어느 거부도 아무것도 커밋하지 않았다


def test_rehearsal_also_runs_the_deployment_guard(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, f"until={_utc(NOW - timedelta(minutes=30))}")

    assert done.returncode == PSQL_SCRIPT_ERROR and "ingest가 없다" in done.stderr


def test_other_press_can_be_selected_with_the_press_variable(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, f"press={OTHER}", "apply=1")

    assert done.returncode == 0, done.stderr
    rows = _rows(pg_conn, schema)
    assert rows["other_ok"][0:4] == (None, None, None, 0) and rows["other_dup"][0] is None
    assert rows["ok1"][0] == "ok" and rows["dup1"][0] == "duplicate"  # 국민일보는 그대로


def test_revert_restores_rows_that_had_no_body_and_leaves_reextracted_and_ok_rows(psql_bin, database_url, pg_conn,
                                                                                  schema):
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1").returncode == 0
    # 다음 ingest가 ok1과 dup3(원래 duplicate)을 다시 받은 상태를 흉내 낸다(ok 행끼리 해시는 유일해야 한다)
    with pg_conn.cursor() as cur:
        for key, sha in (("ok1", "fresh1"), ("dup3", "fresh2")):
            cur.execute(
                f'UPDATE "{schema}".news_raw SET raw_news_extract_status = \'ok\', raw_news_content = \'새 본문\', '
                "raw_news_content_sha256 = %s, raw_news_content_length = 4, raw_news_extracted_at = now(), "
                "raw_news_extract_attempts = 1 WHERE raw_news_title = %s", (sha, key))
    after_ingest = _rows(pg_conn, schema)

    rehearsal = _run(psql_bin, database_url, schema, REVERT_SQL)
    assert rehearsal.returncode == 0, rehearsal.stderr
    assert REVERT_NOTICE.search(rehearsal.stderr).groups() == ("5", "2", "2", "0")
    assert _rows(pg_conn, schema) == after_ingest and _has_audit(pg_conn, schema)

    reverted = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert reverted.returncode == 0, reverted.stderr
    assert not _has_audit(pg_conn, schema)
    rows = _rows(pg_conn, schema)
    for key in ("dup1", "dup2", "dup4", "dropped1", "empty1"):  # 본문이 원래 없던 행은 원래대로
        assert rows[key] == before[key], key
    for key in ("ok2", "ok3"):  # 원래 ok였던 행은 본문을 보관하지 않아 되돌리지 않는다 - 미시도로 남아 다시 받는다
        assert rows[key][0:4] == (None, None, None, 0) and rows[key][5] == "", key
    for key in ("ok1", "dup3"):  # 이미 다시 받은 행은 새 결과를 지키고 건드리지 않는다
        assert rows[key] == after_ingest[key] and rows[key][0] == "ok", key
    for key in set(before) - RESET_KEYS:
        assert rows[key] == before[key], key

    nothing = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert nothing.returncode == PSQL_SCRIPT_ERROR and "보관 표" in nothing.stderr


def _wait_until_waiting_on_a_lock(pg_conn, application_name, seconds=4.0):
    """psql(application_name)의 쿼리가 다른 트랜잭션의 락을 기다리는 상태가 될 때까지 기다린다. 되돌리기의 lock_timeout이 5초라
    그 안에 락을 풀어 줘야 한다."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_stat_activity WHERE application_name = %s AND wait_event_type = 'Lock'",
                        (application_name,))
            if cur.fetchone():
                return True
        time.sleep(0.05)
    return False


def test_revert_does_not_overwrite_a_row_the_ingest_commits_while_it_waits(psql_bin, database_url, pg_conn, schema):
    """되돌리기가 행 락을 기다리는 동안 ingest가 그 행을 다시 받아 커밋하면, 되돌리기는 그 행을 건드리지 않는다. 미리 찍어 둔
    사본으로 행을 고르면 새 본문이 든 행의 상태·해시·길이만 옛 duplicate 값(길이 0)으로 덮여 '본문이 있는 duplicate'가 되고,
    임베딩 단계(본문이 비어 있지 않으면 집는다)가 그 행을 집는다."""
    import psycopg2

    _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1").returncode == 0
    ingest = psycopg2.connect(database_url)  # autocommit이 아니다 - 커밋하기 전까지 행 락을 쥔다
    application_name = f"fix_kmib_revert_{uuid.uuid4().hex[:8]}"
    try:
        with ingest.cursor() as cur:  # 다음 ingest가 dup3(원래 duplicate)을 다시 받는 중 - 아직 커밋하지 않았다
            cur.execute(
                f'UPDATE "{schema}".news_raw SET raw_news_extract_status = \'ok\', raw_news_content = \'새 본문\', '
                "raw_news_content_sha256 = 'fresh', raw_news_content_length = 4, raw_news_extracted_at = now(), "
                "raw_news_extract_attempts = 1 WHERE raw_news_title = %s", ("dup3",))
        args, env = _command(psql_bin, database_url, schema, REVERT_SQL, ["apply=1"],
                             extra_env={"PGAPPNAME": application_name})
        revert = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                  env=env)
        try:
            blocked = _wait_until_waiting_on_a_lock(pg_conn, application_name)
            ingest.commit()  # 되돌리기가 기다리던 락을 푼다
            stdout, stderr = revert.communicate(timeout=60)
        finally:
            if revert.poll() is None:
                revert.kill()
        assert blocked, "되돌리기가 ingest의 행 락을 기다리는 상태가 되지 않았다"
    finally:
        ingest.close()

    assert revert.returncode == 0, stderr
    rows = _rows(pg_conn, schema)
    assert rows["dup3"][0:4] == ("ok", "fresh", 4, 1) and rows["dup3"][5] == "새 본문"  # 새 추출 결과 그대로
    for key in ("dup1", "dup2", "dup4", "dropped1", "empty1"):  # 다시 받지 않은 행은 원래대로
        assert rows[key][0] in ("duplicate", "dropped", "empty") and rows[key][5] == "", key
    assert REVERT_NOTICE.search(stderr).groups() == ("5", "3", "1", "0")


def test_after_a_revert_the_range_can_be_fixed_again(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1").returncode == 0
    assert _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1").returncode == 0

    again = _run(psql_bin, database_url, schema, FIX_SQL, BOUNDARY_ARG, UNTIL_ARG, "apply=1")

    assert again.returncode == 0, again.stderr
    # 되돌리기가 돌려놓은 duplicate 4 + dropped 1 + empty 1만 다시 대상이다. 원래 ok였던 3행은 미시도로 남아 있다
    assert FIX_NOTICE.search(again.stderr).groups() == ("6", "0", "4", "1", "1")
