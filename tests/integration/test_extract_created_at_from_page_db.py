"""본문 추출기가 "미래 발행 시각"을 기사 페이지 값으로 바꾸는 SQL을 실제 PostgreSQL에서 돌린다 (ADR 0008 "2026-10-25 갱신 2").

테스트 DB 안에 스키마 하나를 만들고 public.press / public.news_raw를 `LIKE ... INCLUDING ALL`로 복제한다('ok' 부분 unique 인덱스와
CHECK 제약이 따라온다). 추출기의 커넥션만 그 스키마로 돌리고(search_path), 기사 다운로드와 trafilatura만 가짜로 바꾼다 - 대상 조회와
UPDATE는 실제 문장이다. public에는 아무것도 쓰지 않는다. 페이지와 본문은 모두 지어낸 것이다(ADR 0023).
"""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest

UTC = timezone.utc
KMIB, OTHER = "국민일보", "세계일보"
CRAWLED = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0, tzinfo=None)  # 시간대 없는 UTC
FUTURE = CRAWLED.replace(tzinfo=UTC) + timedelta(hours=23)  # RSS가 준 미래 값
NORMAL = CRAWLED.replace(tzinfo=UTC) - timedelta(hours=1)  # 정상 값
PAGE = CRAWLED.replace(tzinfo=UTC) - timedelta(minutes=40)  # 기사 페이지의 발행 시각


def page_html(marker: str, published: str | None) -> str:
    ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "시험 제목"}
    if published:
        ld["datePublished"] = published
    return (f'<html><head><script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script></head>'
            f"<body><!--{marker}--><p>지어낸 문장</p></body></html>")


@pytest.fixture
def schema(pg_conn):
    name = f"extract_ts_{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'CREATE TABLE "{name}".press (LIKE public.press INCLUDING ALL)')
        cur.execute(f'CREATE TABLE "{name}".news_raw (LIKE public.news_raw INCLUDING ALL)')
    try:
        yield name
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture
def extractor(database_url, schema, monkeypatch):
    """추출기의 DB 연결을 이 스키마로 돌린다. 다운로드와 본문 추출만 가짜: URL -> 페이지, 페이지의 표지 -> 본문."""
    import psycopg2
    from config.settings import Settings
    from crawler.content_extractor import extractor as module
    from crawler.content_extractor.fetch import FetchOutcome

    pages = {}
    monkeypatch.setattr(Settings, "MAX_EXTRACT_ATTEMPTS", 3)
    monkeypatch.setattr(module, "get_connection", lambda: psycopg2.connect(database_url, options=f"-c search_path={schema}"))
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
    _seed(pg_conn, schema, "future_ok", KMIB, FUTURE, pages, page_html("fok", page_value))
    _seed(pg_conn, schema, "future_dropped", KMIB, FUTURE, pages, page_html("short1", page_value))
    _seed(pg_conn, schema, "future_other_press", OTHER, FUTURE, pages, page_html("fother", page_value))
    _seed(pg_conn, schema, "future_no_page_time", KMIB, FUTURE, pages, page_html("fnone", None))
    _seed(pg_conn, schema, "future_naive_page_time", KMIB, FUTURE, pages, page_html("fnaive", "2026-10-25T18:50:00"))
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
    assert rows["future_page_after_crawl"] == (FUTURE, "ok")
    assert rows["normal_row"] == (NORMAL, "ok")
    assert rows["slightly_ahead"] == (slightly_ahead, "ok")

    assert stats["created_at_from_page"] == {KMIB: 2, OTHER: 1}
    assert stats["created_at_unfixed"] == {KMIB: {"no_page_time": 1, "no_tz": 1, "after_crawl": 1}}
    assert (stats["targets"], stats["ok"], stats["dropped"], stats["duplicate"]) == (8, 7, 1, 0)


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

    assert result.status == "ok" and result.created_at_fix == ""
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
