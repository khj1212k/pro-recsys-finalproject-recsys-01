"""본문 추출기가 "미래 발행 시각"을 기사 페이지 값으로 바꾸는 SQL을 실제 PostgreSQL에서 돌린다 (ADR 0008 "2026-10-25 갱신 2").

테스트 DB 안에 스키마 하나를 만들고 public.press / public.news_raw를 `LIKE ... INCLUDING ALL`로 복제한다('ok' 부분 unique 인덱스와
CHECK 제약이 따라온다). 추출기의 커넥션만 그 스키마로 돌리고(search_path), 기사 다운로드와 trafilatura만 가짜로 바꾼다 - 대상 조회와
UPDATE는 실제 문장이다. public에는 아무것도 쓰지 않는다. 페이지와 본문은 모두 지어낸 것이다(ADR 0023).

런북 6.10의 SQL(2차 보정 대상 질의, 수집기가 바꾼 행 되돌리기 질의)은 런북에서 그대로 꺼내 psql로 돌린다 - 문서와 테스트가 달라질 수 없다.
"""
import json
import os
import re
import shutil
import subprocess
import textwrap
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from crawler.content_extractor.page_pubdate import CreatedAtChange

RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "runbook-hosting.md"
UTC = timezone.utc
KMIB, OTHER = "국민일보", "세계일보"
CRAWLED = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0, tzinfo=None)  # 시간대 없는 UTC
FUTURE = CRAWLED.replace(tzinfo=UTC) + timedelta(hours=23)  # RSS가 준 미래 값
NORMAL = CRAWLED.replace(tzinfo=UTC) - timedelta(hours=1)  # 정상 값
PAGE = CRAWLED.replace(tzinfo=UTC) - timedelta(minutes=40)  # 기사 페이지의 발행 시각


def iso_z(value: datetime) -> str:
    """stats.created_at_fixed_rows의 시각 모양: UTC ISO, 끝이 Z."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def page_html(marker: str, published: str | None, meta: str | None = None) -> str:
    ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "시험 제목"}
    if published:
        ld["datePublished"] = published
    meta_tag = f'<meta property="article:published_time" content="{meta}">' if meta else ""
    return (f'<html><head><script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>{meta_tag}</head>'
            f"<body><!--{marker}--><p>지어낸 문장</p></body></html>")


def runbook_sql(name: str) -> str:
    """docs/runbook-hosting.md의 ```sql 블록 가운데 첫 줄이 `-- <name>`인 것을 그대로 꺼낸다(목록 안의 들여쓰기는 걷는다)."""
    text = RUNBOOK.read_text(encoding="utf-8")
    for match in re.finditer(r"^( *)```sql\n(.*?)^\1```", text, re.S | re.M):
        body = textwrap.dedent(match.group(2))
        if body.startswith(f"-- {name}"):
            return body
    raise AssertionError(f"런북에 `-- {name}` SQL 블록이 없다")


@pytest.fixture(scope="module")
def psql_bin():
    path = shutil.which("psql")
    if not path:
        pytest.skip("psql 클라이언트가 없어 런북 SQL 테스트를 건너뜁니다")
    return path


@pytest.fixture
def schema(pg_conn):
    name = f"extract_ts_{uuid.uuid4().hex[:8]}"
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


@pytest.fixture
def session_timezone():
    """추출기 연결의 세션 TimeZone. 시간대를 따지는 테스트가 @pytest.mark.parametrize("session_timezone", [...])로 바꾼다."""
    return "UTC"


@pytest.fixture
def extractor(database_url, schema, session_timezone, monkeypatch):
    """추출기의 DB 연결을 이 스키마로 돌린다. 다운로드와 본문 추출만 가짜: URL -> 페이지, 페이지의 표지 -> 본문."""
    import psycopg2
    from config.settings import Settings
    from crawler.content_extractor import extractor as module
    from crawler.content_extractor.fetch import FetchOutcome

    pages = {}
    monkeypatch.setattr(Settings, "MAX_EXTRACT_ATTEMPTS", 3)
    monkeypatch.setattr(module, "get_connection", lambda: psycopg2.connect(
        database_url, options=f"-c search_path={schema} -c timezone={session_timezone}"))
    monkeypatch.setattr(module, "release_connection", lambda conn: conn.close())
    monkeypatch.setattr(module, "fetch_page", lambda url: FetchOutcome(pages[url]) if pages.get(url) else FetchOutcome(None, "http_404"))

    def fake_extract(html, **_):
        marker = re.search(r"<!--(\w+)-->", html).group(1)
        return "짧은" if marker.startswith("short") else f"{marker} 문장에서 경제 지표가 발표됐다. " * 60

    monkeypatch.setattr(module.trafilatura, "extract", fake_extract)
    return module, pages


def _seed(pg_conn, schema, key, press, created_at, pages, html):
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT press_id FROM "{schema}".press WHERE press_name = %s', (press,))
        found = cur.fetchone()
        if found:
            pid = found[0]
        else:
            cur.execute(f'INSERT INTO "{schema}".press (press_name) VALUES (%s) RETURNING press_id', (press,))
            pid = cur.fetchone()[0]
        url = f"https://example.test/{schema}/{key}"
        cur.execute(
            f'INSERT INTO "{schema}".news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, '
            "raw_news_crawled_at) VALUES (%s, %s, '', %s, %s, %s) RETURNING raw_news_id",
            (pid, key, url, created_at, CRAWLED))
        pages[url] = html
        return cur.fetchone()[0]


def _rows(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT raw_news_title, raw_news_created_at, raw_news_extract_status FROM "{schema}".news_raw')
        return {title: (created, status) for title, created, status in cur.fetchall()}


def test_only_future_rows_take_the_page_time_in_the_same_run_that_extracts_them(pg_conn, schema, extractor):
    module, pages = extractor
    page_value = PAGE.astimezone(timezone(timedelta(hours=9))).isoformat()
    fixed_ids = [
        _seed(pg_conn, schema, "future_ok", KMIB, FUTURE, pages, page_html("fok", page_value)),
        _seed(pg_conn, schema, "future_dropped", KMIB, FUTURE, pages, page_html("short1", page_value)),
        _seed(pg_conn, schema, "future_other_press", OTHER, FUTURE, pages, page_html("fother", page_value)),
    ]
    _seed(pg_conn, schema, "future_no_page_time", KMIB, FUTURE, pages, page_html("fnone", None))
    _seed(pg_conn, schema, "future_naive_page_time", KMIB, FUTURE, pages, page_html("fnaive", "2026-10-25T18:50:00"))
    # 페이지가 두 시각을 말한다(JSON-LD와 meta가 하루 차이) - 어느 쪽도 쓰지 않는다
    _seed(pg_conn, schema, "future_conflict", KMIB, FUTURE, pages,
          page_html("fconflict", page_value, meta=(PAGE - timedelta(days=1)).isoformat()))
    # 페이지 값도 수집보다 뒤 - 같은 이상일 수 있어 쓰지 않는다
    _seed(pg_conn, schema, "future_page_after_crawl", KMIB, FUTURE, pages,
          page_html("fafter", (CRAWLED.replace(tzinfo=UTC) + timedelta(hours=1)).isoformat()))
    # 정상 피드의 값은 페이지 값과 달라도 덮지 않는다
    _seed(pg_conn, schema, "normal_row", OTHER, NORMAL, pages, page_html("normal", page_value))
    # 수집 시각 + 5분: 서버 시계 차이로 보는 범위 - 미래가 아니다
    slightly_ahead = CRAWLED.replace(tzinfo=UTC) + timedelta(minutes=5)
    _seed(pg_conn, schema, "slightly_ahead", OTHER, slightly_ahead, pages, page_html("ahead", page_value))

    stats = module.ContentExtractor().extract_parallel_with_stats(num_workers=1)

    rows = _rows(pg_conn, schema)
    assert rows["future_ok"] == (PAGE, "ok")
    assert rows["future_dropped"] == (PAGE, "dropped")
    assert rows["future_other_press"] == (PAGE, "ok")  # 언론사를 가리지 않는다
    assert rows["future_no_page_time"] == (FUTURE, "ok")
    assert rows["future_naive_page_time"] == (FUTURE, "ok")
    assert rows["future_conflict"] == (FUTURE, "ok")
    assert rows["future_page_after_crawl"] == (FUTURE, "ok")
    assert rows["normal_row"] == (NORMAL, "ok")
    assert rows["slightly_ahead"] == (slightly_ahead, "ok")

    assert stats["created_at_from_page"] == {KMIB: 2, OTHER: 1}
    assert stats["created_at_unfixed"] == {KMIB: {"no_page_time": 1, "no_tz": 1, "conflict": 1, "after_crawl": 1}}
    assert (stats["targets"], stats["ok"], stats["dropped"], stats["duplicate"]) == (9, 8, 1, 0)
    # 바꾼 행의 원래 RSS 값(되돌리는 근거)이 행 번호순으로 남는다 - 못 바꾼 행은 없다
    assert stats["created_at_fixed_rows"] == [[rid, iso_z(FUTURE), iso_z(PAGE)] for rid in sorted(fixed_ids)]
    assert stats["created_at_fixed_rows_omitted"] == 0


def test_duplicate_body_keeps_the_page_time_written_after_the_rolled_back_first_attempt(pg_conn, schema, extractor):
    """같은 본문이 이미 'ok'면 uq_news_raw_content_sha256_ok가 'ok' 기록을 막아 롤백한다 - 'duplicate'로 다시 쓸 때 발행 시각도 남아야 한다."""
    module, pages = extractor
    page_value = PAGE.isoformat()
    _seed(pg_conn, schema, "first", KMIB, NORMAL, pages, page_html("same", page_value))
    _seed(pg_conn, schema, "copy", KMIB, FUTURE, pages, page_html("same", page_value))

    stats = module.ContentExtractor().extract_parallel_with_stats(num_workers=1)

    rows = _rows(pg_conn, schema)
    assert rows["first"] == (NORMAL, "ok")
    assert rows["copy"] == (PAGE, "duplicate")
    assert stats["duplicate"] == 1 and stats["created_at_from_page"] == {KMIB: 1}


def test_a_value_fixed_elsewhere_after_the_target_was_chosen_is_not_overwritten(pg_conn, schema, extractor):
    """대상을 고른 뒤 런북 6.10의 보정이 먼저 고친 행: 넘겨받은 값은 낡았지만 UPDATE의 WHERE가 지금 값을 보고 0행으로 둔다."""
    module, pages = extractor
    corrected = CRAWLED.replace(tzinfo=UTC) - timedelta(minutes=55)  # 보정이 쓴 값(페이지 값과 다르다고 하자)
    rid = _seed(pg_conn, schema, "race", KMIB, corrected, pages, page_html("race", PAGE.isoformat()))

    stale = (rid, f"https://example.test/{schema}/race", KMIB, "race", FUTURE, CRAWLED)  # 고르던 때의 값
    result = module.ContentExtractor.process_single_article(stale)

    assert result.status == "ok" and result.created_at_fix == "" and result.created_at_change is None
    assert _rows(pg_conn, schema)["race"] == (corrected, "ok")


def test_boundary_ten_minutes_and_one_second_is_the_first_future_value(pg_conn, schema, extractor):
    module, pages = extractor
    crawled_utc = CRAWLED.replace(tzinfo=UTC)
    at_edge = crawled_utc + timedelta(minutes=10)
    past_edge = crawled_utc + timedelta(minutes=10, seconds=1)
    _seed(pg_conn, schema, "edge", OTHER, at_edge, pages, page_html("edge", PAGE.isoformat()))
    _seed(pg_conn, schema, "past_edge", OTHER, past_edge, pages, page_html("pastedge", PAGE.isoformat()))

    module.ContentExtractor().extract_parallel_with_stats(num_workers=1)

    rows = _rows(pg_conn, schema)
    assert rows["edge"] == (at_edge, "ok")
    assert rows["past_edge"] == (PAGE, "ok")


@pytest.mark.parametrize("session_timezone", ["UTC", "Asia/Seoul", "America/Los_Angeles"])
def test_the_original_value_is_listed_in_utc_whatever_the_session_time_zone(pg_conn, schema, extractor, session_timezone):
    """UPDATE ... RETURNING은 시간대 있는 값을 세션 시간대로 돌려준다 - 목록에는 항상 UTC로 적는다. 가드(`AT TIME ZONE 'UTC'`)도 같다."""
    module, pages = extractor
    rid = _seed(pg_conn, schema, "tz_row", KMIB, FUTURE, pages, page_html("tzrow", PAGE.isoformat()))

    stats = module.ContentExtractor().extract_parallel_with_stats(num_workers=1)

    assert _rows(pg_conn, schema)["tz_row"] == (PAGE, "ok")
    assert stats["created_at_fixed_rows"] == [[rid, iso_z(FUTURE), iso_z(PAGE)]]
    assert stats["created_at_from_page"] == {KMIB: 1}


@pytest.mark.parametrize("session_timezone", ["UTC", "Asia/Seoul"])
def test_the_sql_guard_is_exact_at_the_ten_minute_edge_when_the_worker_holds_a_stale_future_value(
        pg_conn, schema, extractor, session_timezone):
    """대상을 고를 때 받은 값(낡은 미래 값)으로는 코드의 판정이 '고친다'가 되지만, 실제 행이 수집 + 10분이면 SQL 가드가 0행으로 막는다.
    수집 + 10분 1초인 행은 가드를 넘고, 목록의 '전' 값은 낡은 값이 아니라 지금 행의 값(RETURNING)이다."""
    module, pages = extractor
    crawled_utc = CRAWLED.replace(tzinfo=UTC)
    at_edge = crawled_utc + timedelta(minutes=10)
    past_edge = crawled_utc + timedelta(minutes=10, seconds=1)
    edge_id = _seed(pg_conn, schema, "stale_edge", OTHER, at_edge, pages, page_html("staleedge", PAGE.isoformat()))
    past_id = _seed(pg_conn, schema, "stale_past", OTHER, past_edge, pages, page_html("stalepast", PAGE.isoformat()))
    url = lambda key: f"https://example.test/{schema}/{key}"  # noqa: E731

    edge = module.ContentExtractor.process_single_article((edge_id, url("stale_edge"), OTHER, "stale_edge", FUTURE, CRAWLED))
    past = module.ContentExtractor.process_single_article((past_id, url("stale_past"), OTHER, "stale_past", FUTURE, CRAWLED))

    assert (edge.status, edge.created_at_fix, edge.created_at_change) == ("ok", "", None)
    assert (past.status, past.created_at_fix) == ("ok", "fixed")
    assert past.created_at_change == CreatedAtChange(past_id, past_edge, PAGE)  # 낡은 FUTURE가 아니라 행의 값
    rows = _rows(pg_conn, schema)
    assert rows["stale_edge"] == (at_edge, "ok")
    assert rows["stale_past"] == (PAGE, "ok")


def _psql(psql_bin, database_url, schema, sql_text, tmp_path, *variables):
    sql_file = tmp_path / "query.sql"
    sql_file.write_text(sql_text, encoding="utf-8")
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema}", "PGCLIENTENCODING": "UTF8"}
    args = [psql_bin, "-X", "-v", "ON_ERROR_STOP=1"]
    for v in variables:
        args += ["-v", v]
    result = subprocess.run([*args, "-f", str(sql_file), database_url], capture_output=True, text=True, encoding="utf-8",
                            env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_runbook_query_turns_the_listed_original_values_back_into_updates_that_restore_the_rows(
        pg_conn, schema, extractor, psql_bin, database_url, tmp_path):
    """job_runs.stats에 남은 [행, 전, 후]가 유일한 기록이다 - 런북 6.10의 질의가 만든 UPDATE가 행을 정확히 되돌리고, 그 뒤 다시 바뀐 행은 덮지 않는다."""
    module, pages = extractor
    originals = {f"revert{i}": FUTURE + timedelta(minutes=i, seconds=i) for i in range(3)}
    ids = {key: _seed(pg_conn, schema, key, KMIB, value, pages, page_html(key, PAGE.isoformat()))
           for key, value in originals.items()}
    stats = module.ContentExtractor().extract_parallel_with_stats(num_workers=1)
    assert [row[0] for row in stats["created_at_fixed_rows"]] == sorted(ids.values())
    assert all(value == (PAGE, "ok") for key, value in _rows(pg_conn, schema).items() if key in originals)

    with pg_conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{schema}".job_runs (job, started_at, finished_at, status, stats) '
                    "VALUES ('ingest', now(), now(), 'succeeded', %s::jsonb) RETURNING id", (json.dumps({"extract": stats}),))
        run_id = cur.fetchone()[0]
        # 다른 잡의 회차는 같은 id 목록에 있어도 읽지 않는다
        cur.execute(f'INSERT INTO "{schema}".job_runs (job, started_at, finished_at, status, stats) '
                    "VALUES ('embed', now(), now(), 'succeeded', %s::jsonb) RETURNING id", (json.dumps({"extract": stats}),))
        other_run_id = cur.fetchone()[0]
        # 보정 뒤 다른 곳이 한 행의 값을 다시 바꿨다
        cur.execute(f'UPDATE "{schema}".news_raw SET raw_news_created_at = %s WHERE raw_news_id = %s', (NORMAL, ids["revert2"]))

    query = runbook_sql("revert_created_at_from_stats.sql").replace("<run_ids>", f"{run_id}, {other_run_id}")
    statements = _psql(psql_bin, database_url, schema, query, tmp_path)
    updates = [line for line in statements.splitlines() if line.strip()]
    assert len(updates) == 3 and all(line.startswith("UPDATE news_raw SET raw_news_created_at = '") for line in updates)

    applied = _psql(psql_bin, database_url, schema, statements, tmp_path)
    assert sorted(re.findall(r"UPDATE (\d+)", applied)) == ["0", "1", "1"]  # 다시 바뀐 행은 0행
    rows = _rows(pg_conn, schema)
    assert rows["revert0"][0] == originals["revert0"] and rows["revert1"][0] == originals["revert1"]
    assert rows["revert2"][0] == NORMAL


def test_the_runbook_second_pass_query_and_the_extractor_target_query_leave_no_future_row_out_and_never_overlap(
        pg_conn, schema, extractor, psql_bin, database_url, tmp_path, monkeypatch):
    """2차 보정은 '수집기가 다시 고르지 않는' 미래 행을 고친다. 추출기의 대상 조회(시도 상한 + 본문을 비운 적 없음 + 수집 30일 안)와 한 행도
    겹치거나 빠지지 않아야 한다 - 30일이 지났거나 본문을 비운 fetch_failed·error 행이 양쪽에서 빠지면 영영 못 고친다."""
    module, pages = extractor
    old_crawl = (datetime.now(UTC) - timedelta(days=31)).replace(microsecond=0, tzinfo=None)
    with pg_conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{schema}".press (press_name) VALUES (%s) RETURNING press_id', (KMIB,))
        press_id = cur.fetchone()[0]

    def seed(key, status, attempts, crawled=CRAWLED, purged=False, future=True):
        created = crawled.replace(tzinfo=UTC) + timedelta(hours=23 if future else -1)
        with pg_conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{schema}".news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, '
                "raw_news_crawled_at, raw_news_extract_status, raw_news_extract_attempts, raw_news_content_length, "
                "raw_news_content_purged_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING raw_news_id",
                (press_id, key, None if purged else "", f"https://example.test/{schema}/{key}", created, crawled, status,
                 attempts, 10 if purged else None, datetime.now(UTC) if purged else None))
            return cur.fetchone()[0]

    # 수집기가 다시 고르는 미래 행: 대기 · 시도 여지가 남은 fetch_failed / error
    picked_by_extractor = {seed("pending", None, 0), seed("retry_failed", "fetch_failed", 1), seed("retry_error", "error", 2)}
    # 수집기가 다시 고르지 않는 미래 행 = 2차 보정 대상: 이미 끝난 상태 · 시도를 다 쓴 것 · 수집 30일이 지난 것 · 본문을 비운 것.
    # 상태가 NULL(대기)이어도 수집 30일이 지났거나 본문을 비웠으면 수집기가 영영 고르지 않는다 - 시도 횟수가 얼마든 같다
    left_for_runbook = {
        seed("ok", "ok", 1), seed("dropped", "dropped", 1), seed("empty", "empty", 1),
        seed("spent_failed", "fetch_failed", 3), seed("spent_error", "error", 3),
        seed("old_failed", "fetch_failed", 1, crawled=old_crawl),
        seed("purged_failed", "fetch_failed", 1, purged=True),
        seed("old_pending", None, 0, crawled=old_crawl), seed("old_pending_tried", None, 1, crawled=old_crawl),
        seed("purged_pending", None, 0, purged=True),
        seed("old_purged_pending", None, 0, crawled=old_crawl, purged=True),
    }
    not_future = seed("normal_failed", "fetch_failed", 1, future=False)  # 수집기만 다시 고른다
    every_future_row = picked_by_extractor | left_for_runbook

    query = runbook_sql("future_targets.sql")
    output = _psql(psql_bin, database_url, schema, query, tmp_path)
    runbook_ids = {int(line.split(",")[0]) for line in output.splitlines() if line.strip()}

    seen = []

    def record(article):
        seen.append(article[0])
        return module.ExtractionResult(article[0], article[2], "ok")

    monkeypatch.setattr(module.ContentExtractor, "process_single_article", staticmethod(record))
    module.ContentExtractor().extract_parallel_with_stats(num_workers=1)
    extractor_ids = set(seen)

    assert runbook_ids == left_for_runbook
    assert extractor_ids == picked_by_extractor | {not_future}
    assert runbook_ids.isdisjoint(extractor_ids)
    # 미래 행은 수집기와 2차 보정 질의 가운데 정확히 한 쪽이 맡는다 - 어느 쪽도 못 고치는 행이 없다
    assert (runbook_ids | extractor_ids) >= every_future_row
