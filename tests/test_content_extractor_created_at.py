"""본문 추출기가 "미래 발행 시각"을 기사 페이지 값으로 바꾸는 연결 (ADR 0008 "2026-10-25 갱신 2").

실제 SQL은 integration 테스트(tests/integration/test_extract_created_at_from_page_db.py)가 PostgreSQL에서 돌린다. 여기서는 어떤 경우에
어떤 문장을 어느 트랜잭션에서 쓰는지, 통계 키가 무엇인지를 가짜 커넥션으로 확인한다. 페이지는 모두 지어낸 것이다(ADR 0023).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import psycopg2.errors
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.content_extractor import extractor as extractor_module
from crawler.content_extractor.extractor import CREATED_AT_FIXED_ROWS_LIMIT, ContentExtractor, ExtractionResult
from crawler.content_extractor.page_pubdate import CreatedAtChange
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


def run_single(article, html, extract_return=LONG_BODY, updated=True, side_effect=None, before=FUTURE):
    """updated=False는 UPDATE ... RETURNING이 0행인 경우(그 사이 다른 곳이 고쳤다). 1행이면 (바꾸기 전 값, 바꾼 뒤 값)을 돌려준다."""
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value = cur
    cur.fetchone.return_value = (before, PAGE_VALUE) if updated else None
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
    assert "SET raw_news_created_at = %s" in sql and "WHERE raw_news_id = %s FOR UPDATE" in sql
    # 지금 행의 값이 아직 미래일 때만 바꾼다 - 대상을 고른 뒤 다른 곳이 고친 값을 덮지 않는다
    assert "n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + make_interval(mins => %s)" in sql
    # 바꾸기 전 값은 행을 잠근 부질의에서, 바꾼 뒤 값은 UPDATE에서 한 문장으로 받는다
    assert "RETURNING locked.raw_news_created_at, n.raw_news_created_at" in sql
    assert params == (PAGE_VALUE, 7, 10)
    assert result.created_at_change == CreatedAtChange(7, FUTURE, PAGE_VALUE)
    # 두 문장이 한 번의 커밋으로 묶인다
    assert [c[0] for c in conn.order.mock_calls] == ["execute", "execute", "commit"]


def test_a_row_that_is_not_in_the_future_is_not_touched():
    result, conn, cur = run_single(article(NORMAL), fake_page())

    assert result.status == "ok" and result.created_at_fix == "" and result.created_at_change is None
    assert created_at_updates(cur) == []
    assert cur.execute.call_count == 1


def test_a_row_ten_minutes_ahead_is_not_yet_a_future_row():
    exactly = datetime(2026, 10, 25, 10, 15, 8, tzinfo=UTC)
    _, _, cur = run_single(article(exactly), fake_page())
    assert created_at_updates(cur) == []


def test_future_row_with_a_page_time_after_the_crawl_stays_and_is_reported():
    result, _, cur = run_single(article(FUTURE), fake_page("2026-10-25T20:00:00+09:00"))
    assert (result.status, result.created_at_fix) == ("ok", "after_crawl")
    assert created_at_updates(cur) == [] and result.created_at_change is None


def test_future_row_whose_page_says_two_different_times_stays_and_is_reported_as_a_conflict():
    ld = {"@type": "NewsArticle", "datePublished": "2026-10-25T18:50:00+09:00"}
    html = (f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script>'
            '<meta property="article:published_time" content="2026-10-24T18:50:00+09:00"></head><body></body></html>')
    result, conn, cur = run_single(article(FUTURE), html)
    assert (result.status, result.created_at_fix) == ("ok", "conflict")
    assert created_at_updates(cur) == [] and result.created_at_change is None and conn.commit.call_count == 1


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
    assert dropped.created_at_change == CreatedAtChange(7, FUTURE, PAGE_VALUE)

    empty, conn, cur = run_single(article(FUTURE), fake_page(), extract_return=None)
    assert empty.status == "empty" and empty.created_at_fix == "fixed"
    assert len(created_at_updates(cur)) == 1 and conn.commit.call_count == 1
    assert empty.created_at_change == CreatedAtChange(7, FUTURE, PAGE_VALUE)


def test_duplicate_body_still_gets_the_page_time_after_the_rolled_back_first_attempt():
    """'ok'로 쓰다 uq_news_raw_content_sha256_ok에 막혀 롤백하면 앞의 시도에 쓴 것이 사라진다 - 'duplicate'로 다시 쓸 때 함께 쓴다."""
    result, conn, cur = run_single(article(FUTURE), fake_page(),
                                   side_effect=[psycopg2.errors.UniqueViolation("duplicate key"), None, None])

    assert result.status == "duplicate" and result.created_at_fix == "fixed"
    assert result.created_at_change == CreatedAtChange(7, FUTURE, PAGE_VALUE)
    first, second, third = [c[0] for c in cur.execute.call_args_list]
    assert first[1][1] == "ok" and second[1][1] == "duplicate" and "SET raw_news_created_at" in third[0]
    assert third[1] == (PAGE_VALUE, 7, 10)
    assert [c[0] for c in conn.order.mock_calls] == ["execute", "rollback", "execute", "execute", "commit"]


def test_row_fixed_by_someone_else_in_the_meantime_is_left_and_not_counted():
    """UPDATE의 WHERE가 걸러(0행) 그 사이 다른 곳이 고친 값을 덮지 않는다. 바꾼 행으로 세지도 않는다."""
    result, conn, cur = run_single(article(FUTURE), fake_page(), updated=False)
    assert result.status == "ok" and result.created_at_fix == "" and result.created_at_change is None
    assert len(created_at_updates(cur)) == 1 and conn.commit.call_count == 1


def test_failed_download_never_touches_the_published_time_but_a_future_row_is_reported():
    result, conn, cur = run_single(article(FUTURE), None)
    assert result.status == "fetch_failed" and result.created_at_fix == "fetch_failed" and result.created_at_change is None
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
    assert result.status == "error" and result.created_at_fix == "error" and result.created_at_change is None
    assert created_at_updates(cur) == []
    result, cur = _run_with_parser_crash(article(NORMAL))
    assert result.status == "error" and result.created_at_fix == "" and created_at_updates(cur) == []


def test_four_item_articles_from_older_callers_are_still_accepted():
    result, _, cur = run_single((7, "http://example.com/a", "국민일보", "일반 제목"), fake_page())
    assert result.status == "ok" and result.created_at_fix == "" and result.created_at_change is None
    assert created_at_updates(cur) == []


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


def _runbook_sql(name):
    import re
    import textwrap
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "docs" / "runbook-hosting.md").read_text(encoding="utf-8")
    for match in re.finditer(r"^( *)```sql\n(.*?)^\1```", text, re.S | re.M):
        body = textwrap.dedent(match.group(2))
        if body.startswith(f"-- {name}"):
            return body
    raise AssertionError(f"런북에 `-- {name}` SQL 블록이 없다")


def test_the_runbook_second_pass_query_leaves_out_exactly_the_rows_the_extractor_target_query_selects_again():
    """2차 보정 대상 질의의 '수집기가 다시 고르는 행' 제외 조건은 추출기의 대상 조회와 같은 조건이다(상태 NULL 또는 재시도 상태 + 시도 상한 · 본문을 비운
    적 없음 · 수집 30일 안). 한 가지라도 빠지면 그 행은 수집기도 2차 보정도 못 고친다. 특히 상태가 NULL이어도 수집 30일이 지났거나 본문을 비운 행은
    수집기가 영영 고르지 않으므로 이 질의의 대상이어야 한다. 실제 행으로 겹침·빠짐을 보는 것은 integration 테스트다."""
    from config.retention import BODY_RETENTION_DAYS
    from crawler.content_extractor.extractor import RETRYABLE_STATUSES

    runbook = " ".join(_runbook_sql("future_targets.sql").split())
    statuses = ", ".join(f"'{status}'" for status in RETRYABLE_STATUSES)
    assert f"NOT ((n.raw_news_extract_status IS NULL OR (n.raw_news_extract_status IN ({statuses}) AND n.raw_news_extract_attempts < 3)) " \
           "AND n.raw_news_content_purged_at IS NULL " \
           f"AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '{BODY_RETENTION_DAYS} days')" in runbook
    assert "extract_status IS NOT NULL" not in runbook  # 상태가 NULL인 미래 행을 통째로 빼면 수집 30일이 지난 NULL 행이 양쪽에서 빠진다
    # 추출기의 대상 조회 쪽 (test_extract_parallel_uses_spawn_pool_selects_pending_and_aggregates_stats가 SQL 조각을 고정한다)
    articles = [(1, "u1", "국민일보", "t1", FUTURE, CRAWLED)]
    _, _, seen = _extract(articles, {1: ExtractionResult(1, "국민일보", "ok")}, num_workers=1)
    select_sql, select_params = seen["cur"].execute.call_args[0]
    assert "N.raw_news_extract_attempts < %s" in select_sql and "N.raw_news_content_purged_at IS NULL" in select_sql
    assert "N.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - make_interval(days => %s)" in select_sql
    assert select_params == (list(RETRYABLE_STATUSES), 3, BODY_RETENTION_DAYS)


def test_the_runbook_revert_query_reads_the_keys_the_extractor_writes_into_the_stats():
    sql = " ".join(_runbook_sql("revert_created_at_from_stats.sql").split())
    assert "j.stats -> 'extract' -> 'created_at_fixed_rows'" in sql and "j.job = 'ingest'" in sql
    # 행은 [raw_news_id, 전, 후]: 0 = id, 1 = 되돌릴 값, 2 = 지금 있어야 하는 값(수집기가 쓴 값)
    assert "r.value ->> 1, r.value ->> 0, r.value ->> 2" in sql
    assert "SET raw_news_created_at = %L WHERE raw_news_id = %s AND raw_news_created_at = %L" in sql


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
    assert stats["created_at_fixed_rows"] == [] and stats["created_at_fixed_rows_omitted"] == 0


def test_stats_keys_exist_when_there_are_no_targets():
    stats, _, _ = _extract([], {}, num_workers=1)
    assert stats["created_at_from_page"] == {} and stats["created_at_unfixed"] == {}
    assert stats["created_at_fixed_rows"] == [] and stats["created_at_fixed_rows_omitted"] == 0


def _fixed(raw_news_id, before=FUTURE, after=PAGE_VALUE, press="국민일보"):
    change = CreatedAtChange(raw_news_id, before, after)
    return ExtractionResult(raw_news_id, press, "ok", created_at_fix="fixed", created_at_change=change)


def test_the_original_value_of_every_changed_row_is_kept_in_the_stats_in_row_order_and_as_json():
    """원래 RSS 값은 DB 어디에도 남지 않는다 - 되돌리려면 job_runs.stats(매일 백업된다)의 이 목록이 필요하다."""
    kst = timezone(timedelta(hours=9))
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in (30, 10, 20)]
    results = {
        30: _fixed(30, before=FUTURE.astimezone(kst)),                    # 세션 시간대가 KST여도 UTC로 적는다
        10: _fixed(10, before=datetime(2026, 10, 26, 8, 30, 1, 500000, tzinfo=UTC), after=datetime(2026, 10, 25, 9, 41, tzinfo=UTC)),
        20: _fixed(20),
    }

    stats, _, _ = _extract(articles, results, num_workers=1)

    assert stats["created_at_fixed_rows"] == [
        [10, "2026-10-26T08:30:01.500000Z", "2026-10-25T09:41:00Z"],
        [20, "2026-10-26T09:00:00Z", "2026-10-25T09:50:00Z"],
        [30, "2026-10-26T09:00:00Z", "2026-10-25T09:50:00Z"],
    ]
    assert stats["created_at_fixed_rows_omitted"] == 0
    assert stats["created_at_from_page"] == {"국민일보": 3}
    assert json.loads(json.dumps(stats))["created_at_fixed_rows"] == stats["created_at_fixed_rows"]


def test_only_rows_that_were_really_changed_are_listed():
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in range(1, 5)]
    results = {
        1: _fixed(1),
        2: ExtractionResult(2, "국민일보", "ok"),                                         # 그 사이 다른 곳이 고쳤다 / 미래가 아니었다
        3: ExtractionResult(3, "국민일보", "ok", created_at_fix="no_tz"),
        4: ExtractionResult(4, "국민일보", "ok", created_at_fix="conflict"),
    }
    stats, _, _ = _extract(articles, results, num_workers=1)
    assert [row[0] for row in stats["created_at_fixed_rows"]] == [1]
    assert stats["created_at_unfixed"] == {"국민일보": {"no_tz": 1, "conflict": 1}}


def test_the_list_is_capped_and_the_rows_over_the_cap_are_only_counted(caplog):
    over = CREATED_AT_FIXED_ROWS_LIMIT + 7
    assert CREATED_AT_FIXED_ROWS_LIMIT == 200
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in range(1, over + 1)]
    results = {i: _fixed(i) for i in range(1, over + 1)}

    with caplog.at_level("WARNING", logger=extractor_module.logger.name):
        stats, _, _ = _extract(articles, results, num_workers=1)

    assert len(stats["created_at_fixed_rows"]) == CREATED_AT_FIXED_ROWS_LIMIT
    assert stats["created_at_fixed_rows_omitted"] == 7
    # 건수 통계는 상한과 상관없이 전부 센다 - 목록 + 생략 = 바꾼 행 수
    assert stats["created_at_from_page"] == {"국민일보": over}
    assert len(stats["created_at_fixed_rows"]) + stats["created_at_fixed_rows_omitted"] == over
    assert [row[0] for row in stats["created_at_fixed_rows"]] == sorted(row[0] for row in stats["created_at_fixed_rows"])
    # 목록에 못 들어간 행의 원래 값은 로그에 남는다
    omitted_logs = [r.getMessage() for r in caplog.records if "목록 상한" in r.getMessage()]
    assert len(omitted_logs) == 7 and all("2026-10-26T09:00:00Z" in m for m in omitted_logs)


def test_every_changed_row_is_also_logged_as_a_last_resort_for_a_run_that_dies_midway(caplog):
    """SIGKILL/OOM으로 죽으면 메모리의 목록이 사라진다(중간 저장이 마지막이다) - 행마다 로그 줄도 남긴다. 컨테이너를 다시 만들면 로그도 사라지는 보조 수단이다."""
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in (1, 2)]
    with caplog.at_level("INFO", logger=extractor_module.logger.name):
        _extract(articles, {1: _fixed(1), 2: _fixed(2)}, num_workers=1)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("발행 시각을 기사 페이지 값으로 바꿨다")]
    assert lines == ["발행 시각을 기사 페이지 값으로 바꿨다: id=1 전=2026-10-26T09:00:00Z 후=2026-10-25T09:50:00Z",
                     "발행 시각을 기사 페이지 값으로 바꿨다: id=2 전=2026-10-26T09:00:00Z 후=2026-10-25T09:50:00Z"]


def test_exactly_the_cap_leaves_nothing_omitted():
    count = CREATED_AT_FIXED_ROWS_LIMIT
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in range(1, count + 1)]
    stats, _, _ = _extract(articles, {i: _fixed(i) for i in range(1, count + 1)}, num_workers=1)
    assert len(stats["created_at_fixed_rows"]) == count and stats["created_at_fixed_rows_omitted"] == 0


def test_the_change_record_survives_the_trip_through_a_worker_process():
    """ProcessPoolExecutor는 결과를 pickle로 돌려받는다 - 기록(NamedTuple)과 그 안의 시각이 그대로 와야 한다."""
    import pickle

    result = _fixed(5, before=FUTURE.astimezone(timezone(timedelta(hours=9))))
    again = pickle.loads(pickle.dumps(result))
    assert again == result and again.created_at_change.as_stats_row() == result.created_at_change.as_stats_row()


# ----------------------------------------------------------------------------- 도중에 끊겨도 원래 값이 남는다 (호출한 쪽의 stats · 중간 저장 · 진행 중이던 작업)


class _Terminated(BaseException):
    """jobs.runtime.JobTerminated 대역(종료 신호는 BaseException이다)."""


def test_the_given_stats_dict_is_filled_and_returned_and_stale_keys_are_reset():
    given = {"ok": 99, "created_at_fixed_rows": [[1, "x", "y"]], "unrelated": "kept"}
    articles = [(1, "u1", "국민일보", "t", FUTURE, CRAWLED)]
    stats, _, _ = _extract(articles, {1: _fixed(1)}, num_workers=1, stats=given)
    assert stats is given
    assert given["ok"] == 1 and given["created_at_fixed_rows"] == [[1, "2026-10-26T09:00:00Z", "2026-10-25T09:50:00Z"]]
    assert given["unrelated"] == "kept"


def test_the_given_stats_dict_is_returned_even_when_there_is_nothing_to_extract():
    given = {}
    stats, _, _ = _extract([], {}, num_workers=1, stats=given)
    assert stats is given and given["targets"] == 0 and given["created_at_fixed_rows"] == []


def test_a_run_interrupted_by_a_termination_signal_leaves_the_original_values_in_the_callers_stats():
    """종료 신호(JobTerminated)로 빠져나와도 호출한 쪽의 dict에 지금까지 바꾼 행의 원래 값이 있다 - runtime이 그 dict를 job_runs에 쓴다."""
    for workers, expected in ((1, [1, 2]), (2, [1, 2, 4])):  # 워커 둘이면 4번이 3번과 함께 진행 중이었다 - 그 결과도 거둔다
        given = {}
        articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in (1, 2, 3, 4)]

        def process(article):
            if article[0] == 3:
                raise _Terminated(15)
            return _fixed(article[0])

        with pytest.raises(_Terminated):
            _extract(articles, {i: (lambda a, i=i: process(a)) for i in (1, 2, 3, 4)}, num_workers=workers, stats=given)
        listed = [row[0] for row in given["created_at_fixed_rows"]]
        assert sorted(listed) == expected, workers
        assert given["created_at_from_page"] == {"국민일보": len(expected)} and given["ok"] == len(expected)


def test_results_of_work_still_in_flight_when_the_run_is_interrupted_are_collected_before_leaving():
    """워커는 이미 DB에 커밋했다 - 결과를 부모가 받기 전에 끊기면 그 행의 원래 값은 어디에도 남지 않는다. 종료 신호가 오면 진행 중이던 작업을 마칠 때까지
    기다린 뒤(executor 종료) 그 결과도 거둔다."""
    from tests import test_content_extractor_wiring as wiring

    given = {}
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in (1, 2, 3, 4)]
    calls = []

    def interrupting_wait(fs, return_when):
        calls.append(len(fs))
        if len(calls) == 2:
            raise _Terminated(15)  # 2번째 대기 중에 신호가 온다 - 이때 3·4번 작업이 진행 중이다
        from concurrent.futures import wait as concurrent_wait
        return concurrent_wait(fs, return_when=return_when)

    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value = cur
    cur.fetchall.return_value = list(articles)
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.multiprocessing.get_context", return_value=object()), \
         patch("crawler.content_extractor.extractor.ProcessPoolExecutor", wiring._InlineExecutor), \
         patch("crawler.content_extractor.extractor.wait", side_effect=interrupting_wait), \
         patch.object(ContentExtractor, "process_single_article", side_effect=lambda a: _fixed(a[0])):
        with pytest.raises(_Terminated):
            ContentExtractor().extract_parallel_with_stats(num_workers=2, stats=given)

    assert [row[0] for row in given["created_at_fixed_rows"]] == [1, 2, 3, 4]
    assert given["created_at_from_page"] == {"국민일보": 4} and given["ok"] == 4


def test_a_failure_to_collect_leftover_work_does_not_hide_the_original_interruption(caplog):
    from tests import test_content_extractor_wiring as wiring

    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in (1, 2)]
    boom = RuntimeError("worker crashed")

    def process(article):
        if article[0] == 2:
            raise boom
        return _fixed(article[0])

    def interrupting_wait(fs, return_when):
        raise _Terminated(15)

    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value = cur
    cur.fetchall.return_value = list(articles)
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.multiprocessing.get_context", return_value=object()), \
         patch("crawler.content_extractor.extractor.ProcessPoolExecutor", wiring._InlineExecutor), \
         patch("crawler.content_extractor.extractor.wait", side_effect=interrupting_wait), \
         patch.object(ContentExtractor, "process_single_article", side_effect=process), \
         caplog.at_level("WARNING", logger=extractor_module.logger.name):
        given = {}
        with pytest.raises(_Terminated):  # 워커 예외(RuntimeError)가 종료 신호를 가리지 않는다
            ContentExtractor().extract_parallel_with_stats(num_workers=2, stats=given)

    assert [row[0] for row in given["created_at_fixed_rows"]] == [1]
    assert any("거두지 못했다" in r.getMessage() for r in caplog.records)


def test_checkpoint_is_called_each_time_ten_changed_rows_have_piled_up_and_sees_them_already_counted():
    from crawler.content_extractor.extractor import CREATED_AT_CHECKPOINT_EVERY

    assert CREATED_AT_CHECKPOINT_EVERY == 10
    given, seen = {}, []
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in range(1, 26)]
    stats, _, _ = _extract(articles, {i: _fixed(i) for i in range(1, 26)}, num_workers=1, stats=given,
                           checkpoint=lambda: seen.append((len(given["created_at_fixed_rows"]), given["created_at_from_page"].get("국민일보"))))
    # 10번째 · 20번째 바꾼 행에서 부른다. 부를 때 목록과 건수가 같이 차 있다
    assert seen == [(10, 10), (20, 20)]


def test_checkpoint_is_not_called_for_rows_that_did_not_change_or_did_not_fit_in_the_list():
    from crawler.content_extractor.extractor import CREATED_AT_CHECKPOINT_EVERY

    seen = []
    articles = [(i, f"u{i}", "국민일보", "t", NORMAL, CRAWLED) for i in range(1, 31)]
    _extract(articles, {i: ExtractionResult(i, "국민일보", "ok") for i in range(1, 31)}, num_workers=1, checkpoint=lambda: seen.append(1))
    assert seen == []
    over = CREATED_AT_FIXED_ROWS_LIMIT + 15
    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in range(1, over + 1)]
    _extract(articles, {i: _fixed(i) for i in range(1, over + 1)}, num_workers=1, checkpoint=lambda: seen.append(1))
    assert len(seen) == CREATED_AT_FIXED_ROWS_LIMIT // CREATED_AT_CHECKPOINT_EVERY  # 목록에 못 들어간 행은 중간 저장으로 지킬 값이 없다


def test_a_failing_checkpoint_does_not_stop_the_extraction(caplog):
    def broken():
        raise RuntimeError("db gone")

    articles = [(i, f"u{i}", "국민일보", "t", FUTURE, CRAWLED) for i in range(1, 13)]
    with caplog.at_level("WARNING", logger=extractor_module.logger.name):
        stats, processed, _ = _extract(articles, {i: _fixed(i) for i in range(1, 13)}, num_workers=1, checkpoint=broken)
    assert processed == list(range(1, 13)) and stats["ok"] == 12 and len(stats["created_at_fixed_rows"]) == 12
    assert any("중간 stats 저장 실패" in r.getMessage() for r in caplog.records)
