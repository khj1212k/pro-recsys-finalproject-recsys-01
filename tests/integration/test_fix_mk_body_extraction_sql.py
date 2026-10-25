"""scripts/oci/fix_mk_body_extraction.sql(과 되돌리기)을 실제 PostgreSQL에서 돌린다 (ADR 0026 결정 9, 런북 6.12).

런북과 같은 경로: `psql -X -v ON_ERROR_STOP=1 -v until=... [-v min_length=...] [-v batch_limit=...] [-v apply=1] -f <파일>`.
테스트 DB 안에 스키마 하나를 만들고 public.press / public.news_raw / public.job_runs를 `LIKE ... INCLUDING ALL`로 복제한다
(CHECK 제약과 'ok' 부분 unique 인덱스도 따라온다). 스크립트는 테이블 이름에 스키마를 붙이지 않으므로 PGOPTIONS의 search_path로 그
스키마를 고른다(보관 표도 그 스키마에 생긴다). public에는 아무것도 쓰지 않는다. 본문은 모두 지어낸 짧은 글이다(ADR 0023).
"""
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "fix_mk_body_extraction.sql")
REVERT_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "fix_mk_body_extraction_revert.sql")
PSQL_SCRIPT_ERROR = 3
FIX_NOTICE = re.compile(r"fix_mk batch=(\d+) reset=(\d+) remaining=(\d+) archived_total=(\d+)")
REVERT_NOTICE = re.compile(
    r"fix_mk_revert audited=(\d+) unattempted=(\d+) reextracted_ok=(\d+) dropped=(\d+) empty=(\d+) duplicate=(\d+) "
    r"failed=(\d+) purged_or_missing=(\d+)")
UTC = timezone.utc
MK, OTHER = "매일경제", "세계일보"


@pytest.fixture(scope="module")
def psql_bin():
    path = shutil.which("psql")
    if not path:
        pytest.skip("psql 클라이언트가 없어 재추출 SQL 테스트를 건너뜁니다 (CI integration-test 잡은 설치돼 있는지 확인한다)")
    return path


@pytest.fixture
def schema(pg_conn):
    name = f"fix_mk_{uuid.uuid4().hex[:8]}"
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


def _command(psql_bin, database_url, schema, sql_file, variables, timezone_name="UTC"):
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema} -c timezone={timezone_name}", "PGCLIENTENCODING": "UTF8"}
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
UNTIL = NOW - timedelta(days=1)  # 고친 추출기가 매일경제 기사를 처음 처리한 ingest
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


def _job_run(pg_conn, schema, job, started_at, stats, status="succeeded"):
    finished = None if status == "running" else started_at + timedelta(minutes=3)
    with pg_conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{schema}".job_runs (job, started_at, finished_at, status, stats) '
            "VALUES (%s, %s, %s, %s, %s::jsonb)",
            (job, started_at, finished, status, json.dumps(stats)))


def _mk_stats(per_press=None, container=None, missing=None):
    """ingest 실행의 stats. 고친 추출기만 body_container에 매일경제 키를 남긴다(규칙이 있는 언론사만 센다)."""
    body_container = {}
    if container is not None or missing is not None:
        body_container[MK] = {}
        if container is not None:
            body_container[MK]["container"] = container
        if missing is not None:
            body_container[MK]["container_missing"] = missing
    return {"extract": {"targets": 3, "per_press": per_press or {}, "body_container": body_container}}


def _seed_job_runs(pg_conn, schema):
    """옛 추출기로 돈 ingest(until 앞) -> 고친 추출기의 첫 ingest(= UNTIL)와 그 뒤 ingest. 가드가 세지 않는 실행도 둔다."""
    _job_run(pg_conn, schema, "ingest", UNTIL - timedelta(hours=2),
             {"extract": {"targets": 3, "per_press": {MK: {"ok": 5}}}})  # 옛 추출기 - until 앞이라 세지 않는다
    _job_run(pg_conn, schema, "ingest", UNTIL, _mk_stats({MK: {"ok": 4}}, container=4))
    _job_run(pg_conn, schema, "ingest", NOW - timedelta(hours=1), _mk_stats({MK: {"ok": 6}}, container=6))
    # until 뒤, 매일경제 기사가 다운로드 실패뿐이었던 실행 - 어느 추출기인지 알 수 없어 옛 추출기로 세지 않는다
    _job_run(pg_conn, schema, "ingest", UNTIL + timedelta(minutes=10),
             {"extract": {"targets": 3, "per_press": {MK: {"fetch_failed": 3}}, "body_container": {}}})
    _job_run(pg_conn, schema, "ingest", UNTIL + timedelta(minutes=20), {"rss": {"new": 1}})  # extract 단계가 없다 - 세지 않는다
    _job_run(pg_conn, schema, "embed", UNTIL + timedelta(minutes=25), {"extract": {"per_press": {MK: {"ok": 2}}}})  # ingest 아님


# (키, 저장 길이) - 되돌릴 행 8개. 키 순서 = raw_news_id 순서(배치는 오래된 행부터 되돌린다)
TARGETS = [("t1", 3000), ("t2", 3499), ("t3", 3800), ("t4", 4500), ("t5", 5200), ("t6", 8000), ("t7", 9500), ("t8", 12000)]
TARGET_KEYS = {key for key, _ in TARGETS}


def _seed(pg_conn, schema):
    """UNTIL 앞에 추출된 부푼 매일경제 ok 행 8개(되돌릴 행)와 건드리면 안 되는 행들."""
    _seed_job_runs(pg_conn, schema)
    mk, other = _press(pg_conn, schema, MK), _press(pg_conn, schema, OTHER)
    before = lambda hours: UNTIL - timedelta(hours=hours)  # noqa: E731
    body = "모달 글이 섞인 지어낸 본문"
    for i, (key, length) in enumerate(TARGETS):
        _news(pg_conn, schema, mk, key, "ok", before(10 - i), f"sha-{key}", length, body)
    # --- 건드리면 안 되는 행
    _news(pg_conn, schema, mk, "ok_2999", "ok", before(3), "sha-2999", 2999, body)  # 경계 바로 아래
    _news(pg_conn, schema, mk, "ok_1200", "ok", before(3), "sha-1200", 1200, body)  # 평범한 길이
    _news(pg_conn, schema, mk, "after_until_ok", "ok", UNTIL + timedelta(hours=1), "sha-after", 9000, body)  # 고친 추출기가 받음
    _news(pg_conn, schema, mk, "purged_ok", "ok", before(3), "sha-purged", 9000, None, purged=True)  # 본문을 이미 비움
    _news(pg_conn, schema, mk, "expired_ok", "ok", before(3), "sha-expired", 9000, body, crawled_days_ago=40)  # 수집 30일 넘음
    _news(pg_conn, schema, mk, "dup1", "duplicate", before(3), "sha-t1", 0, "")  # 본문이 원래 없다
    _news(pg_conn, schema, mk, "dropped1", "dropped", before(3), None, 0, "")
    _news(pg_conn, schema, mk, "empty1", "empty", before(3), None, 0, "")
    _news(pg_conn, schema, mk, "failed1", "fetch_failed", before(3), None, 0, "", attempts=2)
    _news(pg_conn, schema, mk, "pending1", None, None, None, None, "", attempts=0)
    _news(pg_conn, schema, other, "other_ok", "ok", before(3), "sha-other", 9000, body)  # 다른 언론사는 길어도 그대로
    return mk, other


def _rows(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT raw_news_title, raw_news_extract_status, raw_news_content_sha256, raw_news_content_length, "
            "raw_news_extract_attempts, raw_news_extracted_at, raw_news_content, raw_news_content_purged_at "
            f'FROM "{schema}".news_raw ORDER BY raw_news_id')
        return {r[0]: r[1:] for r in cur.fetchall()}


def _has_audit(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f'"{schema}".news_raw_mk_extract_fix',))
        return cur.fetchone()[0] is not None


def _audit(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT n.raw_news_title, f.extract_status_before, f.content_sha256_before, f.content_length_before, "
            "f.extract_attempts_before, f.press_name, f.min_length, f.until_at, f.batch_no "
            f'FROM "{schema}".news_raw_mk_extract_fix f JOIN "{schema}".news_raw n USING (raw_news_id)')
        return {r[0]: r[1:] for r in cur.fetchall()}


def _reextract(pg_conn, schema, keys, length=1200):
    """다음 ingest가 되돌린 행을 다시 받은 상태를 흉내 낸다(고친 추출기: 컨테이너 길이, extracted_at은 UNTIL 뒤)."""
    with pg_conn.cursor() as cur:
        for key in keys:
            cur.execute(
                f'UPDATE "{schema}".news_raw SET raw_news_extract_status = \'ok\', raw_news_content = \'새 본문\', '
                "raw_news_content_sha256 = %s, raw_news_content_length = %s, raw_news_extracted_at = now(), "
                "raw_news_extract_attempts = 1 WHERE raw_news_title = %s", (f"new-{key}", length, key))


UNATTEMPTED = (None, None, None, 0, None, "", None)  # 상태, 해시, 길이, 시도 횟수, 추출 시각, 본문, 비운 시각


def test_default_run_is_a_rehearsal_that_changes_nothing(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG)

    assert done.returncode == 0, done.stderr
    assert FIX_NOTICE.search(done.stderr).groups() == ("1", "8", "0", "8")
    assert "ROLLBACK" in done.stdout
    assert _rows(pg_conn, schema) == before and not _has_audit(pg_conn, schema)


def test_apply_resets_only_the_oversized_ok_rows_and_keeps_their_old_metadata(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1")

    assert done.returncode == 0, done.stderr
    assert "COMMIT" in done.stdout
    after = _rows(pg_conn, schema)
    for key in TARGET_KEYS:  # 수집기가 막 넣은 행과 같은 값
        assert after[key][0:4] == (None, None, None, 0) and after[key][4:] == (None, "", None), key
    for key in set(before) - TARGET_KEYS:
        # 3,000자 미만·고친 추출기가 받은 행·비운 행·30일 넘은 행·본문이 원래 없던 행·다른 언론사는 그대로
        assert after[key] == before[key], key
    audit = _audit(pg_conn, schema)
    assert set(audit) == TARGET_KEYS
    for key, length in TARGETS:
        assert audit[key] == ("ok", f"sha-{key}", length, 1, MK, 3000, UNTIL, 1), key


def test_boundary_length_is_inclusive_and_min_length_can_be_changed(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)

    exactly = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "min_length=3001")
    assert exactly.returncode == 0, exactly.stderr
    assert FIX_NOTICE.search(exactly.stderr).group(2) == "7"  # t1(3000자)은 빠진다 - 이상(>=)이다

    wider = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "min_length=2500")
    assert wider.returncode == 0, wider.stderr
    assert FIX_NOTICE.search(wider.stderr).group(2) == "9"  # 2,999자 행(ok_2999)이 들어온다

    narrower = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "min_length=9000")
    assert narrower.returncode == 0, narrower.stderr
    assert FIX_NOTICE.search(narrower.stderr).group(2) == "2"  # t7(9,500)·t8(12,000)만


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
    """되돌린 행은 모두 다음 ingest가 고르고(실제 추출기의 대상 조회), 이미 끝난 행은 고르지 않는다.
    스크립트의 30일은 config.retention.BODY_RETENTION_DAYS(ADR 0023)와 같아야 한다."""
    from config.retention import BODY_RETENTION_DAYS

    assert f"interval '{BODY_RETENTION_DAYS} days'" in open(FIX_SQL, encoding="utf-8").read()
    _seed(pg_conn, schema)
    assert _picked_by_the_extractor(monkeypatch, database_url, schema) == {"pending1", "failed1"}  # 복구 전: 원래 대기 중인 행뿐

    assert _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1").returncode == 0

    # 되돌린 8행 + 원래 대기 중이던 pending1 + 시도 여지가 남은 failed1(attempts=2 < MAX_EXTRACT_ATTEMPTS)
    assert _picked_by_the_extractor(monkeypatch, database_url, schema) == TARGET_KEYS | {"pending1", "failed1"}


def test_until_without_an_offset_is_read_as_utc_even_from_a_kst_session(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, FIX_SQL, f"until={UNTIL:%Y-%m-%d %H:%M:%S}", timezone_name="Asia/Seoul")

    assert done.returncode == 0, done.stderr
    assert FIX_NOTICE.search(done.stderr).group(2) == "8"


def test_batches_reset_the_oldest_rows_first_and_wait_for_the_previous_batch_to_be_fetched(psql_bin, database_url, pg_conn,
                                                                                         schema):
    """batch_limit=3이면 오래된(raw_news_id가 작은) 행부터 3행씩 되돌린다. 앞 배치가 다 받히기 전에는 다음 배치를 거부한다 -
    미시도 행을 추출기가 한 회차에 모두 받으므로, 쌓으면 한 회차에 batch_limit을 넘는 요청이 몰린다."""
    _seed(pg_conn, schema)
    batch = ["batch_limit=3", "apply=1"]

    first = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, *batch)
    assert first.returncode == 0, first.stderr
    assert FIX_NOTICE.search(first.stderr).groups() == ("1", "3", "5", "3")
    rows = _rows(pg_conn, schema)
    assert {k for k in TARGET_KEYS if rows[k][0] is None} == {"t1", "t2", "t3"}  # 가장 오래된 3행

    # 다음 ingest가 아직 돌지 않았다 - 앞 배치가 미시도로 남아 있다
    stacked = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, *batch)
    assert stacked.returncode == PSQL_SCRIPT_ERROR and "앞 배치" in stacked.stderr and "3건" in stacked.stderr
    assert _rows(pg_conn, schema) == rows and set(_audit(pg_conn, schema)) == {"t1", "t2", "t3"}

    # 다음 ingest가 앞 배치를 다시 받았다. t3은 진짜 긴 기사라 컨테이너로 받아도 3,000자를 넘는다(3,900자)
    _reextract(pg_conn, schema, ["t1", "t2"])
    _reextract(pg_conn, schema, ["t3"], length=3900)
    second = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, *batch)
    assert second.returncode == 0, second.stderr
    assert FIX_NOTICE.search(second.stderr).groups() == ("2", "3", "2", "6")  # t3은 다시 뽑히지 않는다(고친 추출기가 받은 행)

    _reextract(pg_conn, schema, ["t4", "t5", "t6"])
    third = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, *batch)
    assert third.returncode == 0, third.stderr
    assert FIX_NOTICE.search(third.stderr).groups() == ("3", "2", "0", "8")

    _reextract(pg_conn, schema, ["t7", "t8"])
    done = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, *batch)
    assert done.returncode == PSQL_SCRIPT_ERROR and "0행" in done.stderr

    audit = _audit(pg_conn, schema)
    assert {k: v[-1] for k, v in audit.items()} == {"t1": 1, "t2": 1, "t3": 1, "t4": 2, "t5": 2, "t6": 2, "t7": 3, "t8": 3}
    assert audit["t3"][2] == 3800  # 보관 표는 처음 되돌릴 때의 값(3,800)이다 - 다시 받은 값(3,900)으로 덮이거나 두 번 보관되지 않는다


def test_after_the_audit_table_is_dropped_the_unattempted_rows_are_not_targets_again(psql_bin, database_url, pg_conn, schema):
    """보관 표를 정리한 뒤 다시 돌려도 이미 미시도인 행은 대상이 아니다(상태가 ok가 아니다). 다시 받은 행은 extracted_at이
    until 뒤라 대상이 아니다 - 보관 표의 키(raw_news_id)와 NOT EXISTS는 그 위의 안전장치다."""
    _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1").returncode == 0
    assert _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1").returncode == 0
    after_revert = _rows(pg_conn, schema)

    again = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1")

    assert again.returncode == PSQL_SCRIPT_ERROR and "0행" in again.stderr  # 8행은 아직 미시도라 대상이 아니다
    assert _rows(pg_conn, schema) == after_revert


def test_arguments_are_validated(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    no_until = _run(psql_bin, database_url, schema, FIX_SQL, "apply=1")
    assert no_until.returncode == PSQL_SCRIPT_ERROR and "until" in no_until.stderr

    low = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "min_length=2499", "apply=1")
    assert low.returncode == PSQL_SCRIPT_ERROR and "min_length=2499" in low.stderr

    for value in ("0", "1001", "-5"):
        bad = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, f"batch_limit={value}", "apply=1")
        assert bad.returncode == PSQL_SCRIPT_ERROR and f"batch_limit={value}" in bad.stderr, value

    top = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "batch_limit=1000")
    assert top.returncode == 0, top.stderr  # 상한 1000은 허용한다(리허설)

    assert _rows(pg_conn, schema) == before and not _has_audit(pg_conn, schema)  # 어느 실패도 아무것도 커밋하지 않았다


def test_apply_stops_on_empty_targets_and_linked_rows(psql_bin, database_url, pg_conn, schema):
    mk, _ = _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    # 고친 추출기의 ingest(NOW-1h)가 until 뒤에 있어 가드는 통과하고, 이 길이 기준(100000자 이상)에는 행이 없다
    nothing = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "min_length=100000", "apply=1")
    assert nothing.returncode == PSQL_SCRIPT_ERROR and "0행" in nothing.stderr

    _news(pg_conn, schema, mk, "letter_ok", "ok", UNTIL - timedelta(hours=20), "sha-letter", 6000, "본문", letter_id=7)
    linked = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1")
    assert linked.returncode == PSQL_SCRIPT_ERROR and "임베딩이 있거나 뉴스레터" in linked.stderr
    with pg_conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{schema}".news_raw WHERE raw_news_title = %s', ("letter_ok",))
    _news(pg_conn, schema, mk, "embedded_ok", "ok", UNTIL - timedelta(hours=20), "sha-emb", 6000, "본문", embedded=True)
    embedded = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1")
    assert embedded.returncode == PSQL_SCRIPT_ERROR and "임베딩이 있거나 뉴스레터" in embedded.stderr
    with pg_conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{schema}".news_raw WHERE raw_news_title = %s', ("embedded_ok",))

    assert _rows(pg_conn, schema) == before and not _has_audit(pg_conn, schema)  # 어느 실패도 아무것도 커밋하지 않았다


def test_apply_refuses_until_the_fixed_extractor_has_processed_mk_articles_after_until(psql_bin, database_url, pg_conn,
                                                                                        schema):
    """옛 추출기가 도는 채로 되돌리면 다음 ingest가 되돌린 행을 같은 모달 글로 다시 채운다. 고친 추출기가 배포됐는지는 job_runs의
    stats.extract.body_container의 매일경제 키(고친 추출기만 남기는 키)로 확인한다."""
    _seed(pg_conn, schema)
    before = _rows(pg_conn, schema)

    future = _run(psql_bin, database_url, schema, FIX_SQL, f"until={_utc(NOW + timedelta(hours=1))}", "apply=1")
    assert future.returncode == PSQL_SCRIPT_ERROR and "보다 뒤다" in future.stderr

    # until 뒤에 매일경제 기사를 고친 추출기로 처리한 ingest가 없다 = 아직 배포하지 않았거나 until이 틀렸다
    not_yet = _run(psql_bin, database_url, schema, FIX_SQL, f"until={_utc(NOW - timedelta(minutes=30))}", "apply=1")
    assert not_yet.returncode == PSQL_SCRIPT_ERROR and "고친 추출기" in not_yet.stderr and "ingest가 없다" in not_yet.stderr

    # until 뒤에 옛 추출기가 매일경제 기사를 처리한 ingest가 있다(body_container에 매일경제 키가 없다)
    _job_run(pg_conn, schema, "ingest", NOW - timedelta(hours=2), {"extract": {"per_press": {MK: {"ok": 3, "duplicate": 1}}}})
    old_after = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1")
    assert old_after.returncode == PSQL_SCRIPT_ERROR and "옛 추출기로 매일경제 기사를 처리한 ingest가 1회" in old_after.stderr

    assert _rows(pg_conn, schema) == before and not _has_audit(pg_conn, schema)  # 어느 거부도 아무것도 커밋하지 않았다


def test_a_run_where_every_mk_article_failed_to_download_is_not_counted_as_the_old_extractor(psql_bin, database_url,
                                                                                            pg_conn, schema):
    _seed(pg_conn, schema)  # until 뒤에 매일경제가 fetch_failed뿐이던 실행이 이미 있다(_seed_job_runs)

    done = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG)

    assert done.returncode == 0, done.stderr


def test_apply_refuses_when_the_fixed_extractor_cannot_find_the_article_node(psql_bin, database_url, pg_conn, schema):
    """container_missing(본문 노드를 못 찾아 전체 페이지로 되돌아감)이 10%를 넘으면 고친 추출기가 새 페이지에 맞지 않는 것이다 -
    되돌린 행이 같은 모달 글로 다시 채워진다."""
    _seed(pg_conn, schema)  # until 뒤 container 합계 10(4 + 6)
    _job_run(pg_conn, schema, "ingest", NOW - timedelta(minutes=30), _mk_stats({MK: {"ok": 6}}, container=0, missing=6))

    refused = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1")

    assert refused.returncode == PSQL_SCRIPT_ERROR and "본문 노드를 못 찾았다" in refused.stderr
    assert not _has_audit(pg_conn, schema)


def test_a_few_missing_article_nodes_are_tolerated(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)  # container 10
    _job_run(pg_conn, schema, "ingest", NOW - timedelta(minutes=30), _mk_stats({MK: {"ok": 3}}, container=2, missing=1))

    done = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG)  # 1 / 13 < 10%

    assert done.returncode == 0, done.stderr


def test_apply_refuses_while_an_ingest_is_running(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    _job_run(pg_conn, schema, "ingest", NOW - timedelta(minutes=5), {}, status="running")
    before = _rows(pg_conn, schema)

    running = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1")

    assert running.returncode == PSQL_SCRIPT_ERROR and "ingest가 지금 돌고 있다" in running.stderr
    assert _rows(pg_conn, schema) == before and not _has_audit(pg_conn, schema)


def test_a_running_job_that_is_not_ingest_does_not_block(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    _job_run(pg_conn, schema, "backup", NOW - timedelta(minutes=5), {}, status="running")

    done = _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG)

    assert done.returncode == 0, done.stderr


# ----------------------------------------------------------------------------- 되돌리기(정리)


def test_revert_reports_how_the_archived_rows_ended_and_drops_the_audit_table(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)
    assert _run(psql_bin, database_url, schema, FIX_SQL, UNTIL_ARG, "apply=1").returncode == 0
    # 다음 ingest가 되돌린 행을 받은 결과: t1·t2 ok, t3 dropped(컨테이너 글이 350자 미만), t4 duplicate, t5 다운로드 실패,
    # t6은 보존으로 본문을 비움, t7은 사라짐, t8은 아직 미시도
    _reextract(pg_conn, schema, ["t1", "t2"])
    with pg_conn.cursor() as cur:
        cur.execute(f'UPDATE "{schema}".news_raw SET raw_news_extract_status = \'dropped\', raw_news_extract_attempts = 1, '
                    "raw_news_extracted_at = now() WHERE raw_news_title = 't3'")
        cur.execute(f'UPDATE "{schema}".news_raw SET raw_news_extract_status = \'duplicate\', raw_news_extract_attempts = 1, '
                    "raw_news_content_length = 0, raw_news_content_sha256 = 'new-t1', raw_news_extracted_at = now() "
                    "WHERE raw_news_title = 't4'")
        cur.execute(f'UPDATE "{schema}".news_raw SET raw_news_extract_status = \'fetch_failed\', raw_news_extract_attempts = 3 '
                    "WHERE raw_news_title = 't5'")
        cur.execute(f'UPDATE "{schema}".news_raw SET raw_news_content = NULL, raw_news_content_length = 99, '
                    "raw_news_content_purged_at = now() WHERE raw_news_title = 't6'")
        cur.execute(f'DELETE FROM "{schema}".news_raw WHERE raw_news_title = \'t7\'')
    after_ingest = _rows(pg_conn, schema)

    rehearsal = _run(psql_bin, database_url, schema, REVERT_SQL)
    assert rehearsal.returncode == 0, rehearsal.stderr
    assert REVERT_NOTICE.search(rehearsal.stderr).groups() == ("8", "1", "2", "1", "0", "1", "1", "2")
    assert "ROLLBACK" in rehearsal.stdout
    assert _rows(pg_conn, schema) == after_ingest and _has_audit(pg_conn, schema)

    reverted = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert reverted.returncode == 0, reverted.stderr
    assert "COMMIT" in reverted.stdout
    assert not _has_audit(pg_conn, schema)
    assert _rows(pg_conn, schema) == after_ingest  # news_raw는 바뀌지 않는다 - 다시 받은 행도 미시도 행도 그대로

    nothing = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert nothing.returncode == PSQL_SCRIPT_ERROR and "보관 표" in nothing.stderr


def test_revert_without_a_prior_fix_stops(psql_bin, database_url, pg_conn, schema):
    _seed(pg_conn, schema)

    done = _run(psql_bin, database_url, schema, REVERT_SQL)

    assert done.returncode == PSQL_SCRIPT_ERROR and "보관 표" in done.stderr
