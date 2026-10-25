"""본문 추출기가 "미래 발행 시각"을 기사 페이지 값으로 바꾸는 연결 (ADR 0008 "2026-10-25 갱신 2").

실제 SQL은 integration 테스트(tests/integration/test_extract_created_at_from_page_db.py)가 PostgreSQL에서 돌린다. 여기서는 어떤 경우에
어떤 문장을 어느 트랜잭션에서 쓰는지, 통계 키가 무엇인지를 가짜 커넥션으로 확인한다. 페이지는 모두 지어낸 것이다(ADR 0023).
"""
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import psycopg2.errors
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.content_extractor.extractor import ContentExtractor, ExtractionResult
from crawler.content_extractor.fetch import FetchOutcome
from tests.test_content_extractor_wiring import _extract

UTC = timezone.utc
CRAWLED = datetime(2026, 10, 25, 10, 5, 8)  # 시간대 없는 UTC
FUTURE = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # 수집보다 23시간 뒤 - RSS가 준 값
NORMAL = datetime(2026, 10, 25, 9, 30, tzinfo=UTC)  # 수집 35분 전 - 정상
PAGE_VALUE = datetime(2026, 10, 25, 9, 50, tzinfo=UTC)
LONG_BODY = "경제 지표가 발표됐다. " * 60


def fake_page(published="2026-10-25T18:50:00+09:00") -> str:
    ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "시험 제목", "datePublished": published}
    return f'<html><head><script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script></head><body><p>지어낸 문장</p></body></html>'


def run_single(article, html, extract_return=LONG_BODY, rowcount=1, side_effect=None):
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value = cur
    cur.rowcount = rowcount
    if side_effect is not None:
        cur.execute.side_effect = side_effect
    # 문장과 커밋·롤백이 불린 순서를 한 목록으로 본다(conn.order.mock_calls)
    conn.order = MagicMock()
    for name, mock in (("execute", cur.execute), ("commit", conn.commit), ("rollback", conn.rollback)):
        conn.order.attach_mock(mock, name)
    outcome = FetchOutcome(html) if html else FetchOutcome(None, "http_404")
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_page", return_value=outcome), \
         patch("crawler.content_extractor.extractor.trafilatura.extract", return_value=extract_return):
        result = ContentExtractor.process_single_article(article)
    return result, conn, cur


def created_at_updates(cur):
    return [c for c in cur.execute.call_args_list if "SET raw_news_created_at" in c[0][0]]


def article(created_at, crawled_at=CRAWLED, raw_news_id=7, press="국민일보"):
    return (raw_news_id, "http://example.com/a", press, "일반 제목", created_at, crawled_at)


# ----------------------------------------------------------------------------- 어떤 경우에 쓰나


def test_future_row_gets_the_page_time_in_the_same_transaction_as_the_extraction_result():
    result, conn, cur = run_single(article(FUTURE), fake_page())

    assert result.status == "ok" and result.created_at_fix == "fixed"
    extraction, created_at = [c[0] for c in cur.execute.call_args_list]
    assert "raw_news_extract_status" in extraction[0] and extraction[1][1] == "ok"
    sql, params = created_at
    assert "SET raw_news_created_at = %s" in sql and "WHERE raw_news_id = %s" in sql
    # 지금 행의 값이 아직 미래일 때만 바꾼다 - 대상을 고른 뒤 다른 곳이 고친 값을 덮지 않는다
    assert "raw_news_created_at > (raw_news_crawled_at AT TIME ZONE 'UTC') + make_interval(mins => %s)" in sql
    assert params == (PAGE_VALUE, 7, 10)
    # 두 문장이 한 번의 커밋으로 묶인다
    assert [c[0] for c in conn.order.mock_calls] == ["execute", "execute", "commit"]


def test_a_row_that_is_not_in_the_future_is_not_touched():
    result, conn, cur = run_single(article(NORMAL), fake_page())

    assert result.status == "ok" and result.created_at_fix == ""
    assert created_at_updates(cur) == []
    assert cur.execute.call_count == 1


def test_a_row_ten_minutes_ahead_is_not_yet_a_future_row():
    exactly = datetime(2026, 10, 25, 10, 15, 8, tzinfo=UTC)
    _, _, cur = run_single(article(exactly), fake_page())
    assert created_at_updates(cur) == []


def test_future_row_with_a_page_time_after_the_crawl_stays_and_is_reported():
    result, _, cur = run_single(article(FUTURE), fake_page("2026-10-25T20:00:00+09:00"))
    assert (result.status, result.created_at_fix) == ("ok", "after_crawl")
    assert created_at_updates(cur) == []


@pytest.mark.parametrize("html, reason", [
    ("<html><body><p>지어낸 문장</p></body></html>", "no_page_time"),
    (fake_page("2026-10-25T18:50:00"), "no_tz"),
    (fake_page("2026-10-01T09:00:00+09:00"), "too_old"),
])
def test_future_row_without_a_usable_page_time_stays_and_is_reported(html, reason):
    result, conn, cur = run_single(article(FUTURE), html)
    assert (result.status, result.created_at_fix) == ("ok", reason)
    assert created_at_updates(cur) == [] and conn.commit.call_count == 1


def test_the_page_time_is_also_written_when_the_body_is_dropped_or_empty():
    dropped, conn, cur = run_single(article(FUTURE), fake_page(), extract_return="너무 짧은 본문")
    assert dropped.status == "dropped" and dropped.created_at_fix == "fixed"
    assert len(created_at_updates(cur)) == 1 and conn.commit.call_count == 1

    empty, conn, cur = run_single(article(FUTURE), fake_page(), extract_return=None)
    assert empty.status == "empty" and empty.created_at_fix == "fixed"
    assert len(created_at_updates(cur)) == 1 and conn.commit.call_count == 1


def test_duplicate_body_still_gets_the_page_time_after_the_rolled_back_first_attempt():
    """'ok'로 쓰다 uq_news_raw_content_sha256_ok에 막혀 롤백하면 앞의 시도에 쓴 것이 사라진다 - 'duplicate'로 다시 쓸 때 함께 쓴다."""
    result, conn, cur = run_single(article(FUTURE), fake_page(),
                                   side_effect=[psycopg2.errors.UniqueViolation("duplicate key"), None, None])

    assert result.status == "duplicate" and result.created_at_fix == "fixed"
    first, second, third = [c[0] for c in cur.execute.call_args_list]
    assert first[1][1] == "ok" and second[1][1] == "duplicate" and "SET raw_news_created_at" in third[0]
    assert third[1] == (PAGE_VALUE, 7, 10)
    assert [c[0] for c in conn.order.mock_calls] == ["execute", "rollback", "execute", "execute", "commit"]


def test_row_fixed_by_someone_else_in_the_meantime_is_left_and_not_counted():
    """UPDATE의 WHERE가 걸러(0행) 그 사이 다른 곳이 고친 값을 덮지 않는다. 바꾼 행으로 세지도 않는다."""
    result, conn, cur = run_single(article(FUTURE), fake_page(), rowcount=0)
    assert result.status == "ok" and result.created_at_fix == ""
    assert len(created_at_updates(cur)) == 1 and conn.commit.call_count == 1


def test_failed_download_never_touches_the_published_time_but_a_future_row_is_reported():
    result, conn, cur = run_single(article(FUTURE), None)
    assert result.status == "fetch_failed" and result.created_at_fix == "fetch_failed"
    assert created_at_updates(cur) == []
    # 미래가 아닌 행은 세지 않는다
    result, _, cur = run_single(article(NORMAL), None)
    assert result.status == "fetch_failed" and result.created_at_fix == "" and created_at_updates(cur) == []


def _run_with_parser_crash(art):
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value = cur
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_page", return_value=FetchOutcome(fake_page())), \
         patch("crawler.content_extractor.extractor.trafilatura.extract", side_effect=RuntimeError("parser crashed")):
        return ContentExtractor.process_single_article(art), cur


def test_extraction_error_does_not_change_the_published_time_but_a_future_row_is_reported():
    result, cur = _run_with_parser_crash(article(FUTURE))
    assert result.status == "error" and result.created_at_fix == "error"
    assert created_at_updates(cur) == []
    result, cur = _run_with_parser_crash(article(NORMAL))
    assert result.status == "error" and result.created_at_fix == "" and created_at_updates(cur) == []


def test_four_item_articles_from_older_callers_are_still_accepted():
    result, _, cur = run_single((7, "http://example.com/a", "국민일보", "일반 제목"), fake_page())
    assert result.status == "ok" and result.created_at_fix == "" and created_at_updates(cur) == []


def test_any_press_is_handled_the_same_way():
    """미래 시각은 어느 언론사든 틀린 값이다 - 규칙은 언론사에 묶이지 않는다."""
    for press in ("국민일보", "세계일보", "AI타임스"):
        result, _, cur = run_single(article(FUTURE, press=press), fake_page())
        assert result.created_at_fix == "fixed" and len(created_at_updates(cur)) == 1


# ----------------------------------------------------------------------------- 대상 조회와 통계


def test_the_target_query_hands_over_the_published_and_crawl_times_without_changing_the_conditions():
    articles = [(1, "u1", "국민일보", "t1", FUTURE, CRAWLED)]
    stats, processed, seen = _extract(articles, {1: ExtractionResult(1, "국민일보", "ok", created_at_fix="fixed")}, num_workers=1)

    select_sql, select_params = seen["cur"].execute.call_args[0]
    assert "N.raw_news_created_at, N.raw_news_crawled_at" in select_sql
    # 고르는 조건은 그대로다 - 보존 잡의 "추출 대기" 조건과 같아야 한다(tests/test_retention_job.py)
    assert "raw_news_extract_status IS NULL" in select_sql and "N.raw_news_extract_status = ANY(%s)" in select_sql
    assert select_params == (["fetch_failed", "error"], 3, 30)
    assert "created_at" not in select_sql.split("WHERE", 1)[1]  # 발행 시각은 고르는 조건이 아니다


def test_stats_count_the_rows_changed_per_press_and_the_reasons_for_those_that_could_not_be_changed():
    articles = [(i, f"u{i}", press, "t", FUTURE, CRAWLED) for i, press in
                enumerate(["국민일보"] * 4 + ["경향신문", "경향신문", "세계일보"], start=1)]
    results = {
        1: ExtractionResult(1, "국민일보", "ok", created_at_fix="fixed"),
        2: ExtractionResult(2, "국민일보", "dropped", ("too_short<350",), created_at_fix="fixed"),
        3: ExtractionResult(3, "국민일보", "ok", created_at_fix="no_page_time"),
        4: ExtractionResult(4, "국민일보", "ok"),  # 미래가 아니었다
        5: ExtractionResult(5, "경향신문", "duplicate", created_at_fix="fixed"),
        6: ExtractionResult(6, "경향신문", "ok", created_at_fix="after_crawl"),
        7: ExtractionResult(7, "세계일보", "fetch_failed", fetch_cause="http_404"),  # 미래가 아니었다
    }

    stats, _, _ = _extract(articles, results, num_workers=1)

    assert stats["created_at_from_page"] == {"국민일보": 2, "경향신문": 1}
    assert stats["created_at_unfixed"] == {"국민일보": {"no_page_time": 1}, "경향신문": {"after_crawl": 1}}
    # 본문 통계는 그대로다
    assert (stats["ok"], stats["dropped"], stats["duplicate"], stats["fetch_failed"]) == (4, 1, 1, 1)


def test_future_rows_that_ended_before_the_page_was_read_are_counted_as_unfixed():
    """못 고친 행이 모두 created_at_unfixed에 보여야 한다 - 시도 상한을 다 쓰고 미래인 채 끝나는 행도 통계에 있다."""
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in range(1, 5)]
    results = {
        1: ExtractionResult(1, "국민일보", "fetch_failed", fetch_cause="http_404", created_at_fix="fetch_failed"),
        2: ExtractionResult(2, "국민일보", "fetch_failed", fetch_cause="timeout", created_at_fix="fetch_failed"),
        3: ExtractionResult(3, "국민일보", "error", detail="x", created_at_fix="error"),
        4: ExtractionResult(4, "국민일보", "ok", created_at_fix="no_tz"),
    }
    stats, _, _ = _extract(articles, results, num_workers=1)
    assert stats["created_at_unfixed"] == {"국민일보": {"fetch_failed": 2, "error": 1, "no_tz": 1}}
    assert stats["created_at_from_page"] == {}


def test_stats_keys_exist_and_are_empty_when_nothing_was_in_the_future():
    stats, _, _ = _extract([(1, "u1", "국민일보", "t1", NORMAL, CRAWLED)], {1: ExtractionResult(1, "국민일보", "ok")}, num_workers=1)
    assert stats["created_at_from_page"] == {} and stats["created_at_unfixed"] == {}


def test_stats_keys_exist_when_there_are_no_targets():
    stats, _, _ = _extract([], {}, num_workers=1)
    assert stats["created_at_from_page"] == {} and stats["created_at_unfixed"] == {}
