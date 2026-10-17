import hashlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.content_extractor.extractor import ContentExtractor, DispatchPlan, ExtractionResult
from crawler.content_extractor.fetch import FetchOutcome


def test_process_single_article_drops_low_quality_content():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur

    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_page", return_value=FetchOutcome("<html/>")), \
         patch("crawler.content_extractor.extractor.trafilatura.extract", return_value="너무 짧은 본문"):

        # (raw_news_id, url, press_name, title) - "너무 짧은 본문"은 DROP_LEN(350)보다 훨씬 짧음
        result = ContentExtractor.process_single_article((1, "http://example.com", "테스트언론사", "일반 제목"))

    assert result.status == "dropped"
    assert any(r.startswith("too_short") for r in result.reasons)
    # 저품질로 판정되면 raw_news_content는 빈 문자열, 추출 상태는 'dropped'로 기록돼야
    # 다음 실행에서 같은 기사를 다시 내려받지 않는다.
    executed_sql, executed_params = cur.execute.call_args[0]
    assert "raw_news_extract_status" in executed_sql
    assert executed_params == ("", "dropped", None, 0, 1)


def _run_single(fetch_return=None, extract_return=None, fetch_exc=None, fetch_cause="disconnect"):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    outcome = FetchOutcome(fetch_return) if fetch_return else FetchOutcome(None, fetch_cause, 2)
    fetch_patch = (
        patch("crawler.content_extractor.extractor.fetch_page", side_effect=fetch_exc)
        if fetch_exc else
        patch("crawler.content_extractor.extractor.fetch_page", return_value=outcome)
    )
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         fetch_patch, \
         patch("crawler.content_extractor.extractor.trafilatura.extract", return_value=extract_return):
        result = ContentExtractor.process_single_article((7, "http://example.com/a", "동아일보", "일반 제목"))
    return result, conn, cur


def test_process_single_article_records_ok_status_with_cleaned_content():
    long_body = "경제 지표가 발표됐다. " * 60
    result, conn, cur = _run_single(fetch_return="<html/>", extract_return=long_body)

    assert result.status == "ok"
    assert result.press_name == "동아일보"
    _, params = cur.execute.call_args[0]
    content, status, digest, length, raw_news_id = params
    assert status == "ok" and raw_news_id == 7
    assert len(content) > 350
    # 본문 길이(글자 수)를 함께 쓴다 - 보존 기한이 지나 본문을 비워도 남는다(ADR 0023)
    assert length == len(content)
    # 정제된 본문의 sha256 - URL만 다른 같은 기사를 DB의 부분 unique 인덱스로 걸러낸다
    assert digest == hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn.commit.assert_called_once()


def test_process_single_article_marks_duplicate_when_same_body_already_saved():
    """다른 URL로 이미 저장된 본문이면 uq_news_raw_content_sha256_ok가 막는다 - 사본은 본문 없이
    'duplicate'로 남겨 임베딩·클러스터에 두 번 들어가지 않게 한다."""
    import psycopg2.errors

    long_body = "경제 지표가 발표됐다. " * 60
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.execute.side_effect = [psycopg2.errors.UniqueViolation("duplicate key"), None]
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_page", return_value=FetchOutcome("<html/>")), \
         patch("crawler.content_extractor.extractor.trafilatura.extract", return_value=long_body):
        result = ContentExtractor.process_single_article((8, "http://example.com/b", "동아일보", "일반 제목"))

    assert result.status == "duplicate"
    (_, first), (_, second) = [c[0] for c in cur.execute.call_args_list]
    assert first[1] == "ok"
    assert second == ("", "duplicate", first[2], 0, 8)
    conn.rollback.assert_called_once()
    conn.commit.assert_called_once()


def test_process_single_article_marks_fetch_failure_for_bounded_retry():
    result, conn, cur = _run_single(fetch_return=None, fetch_cause="read_timeout")

    assert result.status == "fetch_failed"
    # 실패 원인은 결과로만 돌려준다(stats에 언론사별 개수로 남는다) - DB에는 쓰지 않는다
    assert result.fetch_cause == "read_timeout"
    _, params = cur.execute.call_args[0]
    assert params == ("", "fetch_failed", None, 0, 7)


def test_process_single_article_marks_empty_when_nothing_extracted():
    result, conn, cur = _run_single(fetch_return="<html/>", extract_return=None)

    assert result.status == "empty"
    _, params = cur.execute.call_args[0]
    assert params == ("", "empty", None, 0, 7)


def test_process_single_article_records_error_so_retries_are_bounded():
    """예외(파서 크래시 등)도 시도 횟수를 올려 fetch_failed처럼 상한까지만 다시 시도한다 -
    상태를 남기지 않으면 같은 URL을 매시 영원히 다시 받는다."""
    result, conn, cur = _run_single(fetch_exc=RuntimeError("parser crashed"))

    assert result.status == "error"
    assert "parser crashed" in result.detail
    conn.rollback.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "raw_news_extract_attempts + 1" in sql
    assert params == ("", "error", None, 0, 7)
    conn.commit.assert_called_once()


def test_process_single_article_error_still_reported_when_status_cannot_be_recorded():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.execute.side_effect = RuntimeError("db gone")
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_page", return_value=FetchOutcome(None, "disconnect")):
        result = ContentExtractor.process_single_article((9, "http://example.com/c", "동아일보", "제목"))

    assert result.status == "error" and "db gone" in result.detail
    assert conn.rollback.call_count == 2


class _InlineExecutor:
    """spawn ProcessPoolExecutor 대역: submit을 같은 프로세스에서 바로 실행한다."""

    instances = []

    def __init__(self, max_workers=None, mp_context=None):
        self.max_workers, self.mp_context = max_workers, mp_context
        self.submitted = []
        _InlineExecutor.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args):
        from concurrent.futures import Future

        future = Future()
        self.submitted.append(args[0][0])
        try:
            future.set_result(fn(*args))
        except BaseException as e:  # noqa: BLE001
            future.set_exception(e)
        return future


def _extract(articles, results, num_workers=2, **kwargs):
    """대상 조회(DB)와 기사 처리를 가짜로 바꾸고 extract_parallel_with_stats를 돌린다."""
    from concurrent.futures import wait as real_wait

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.fetchall.return_value = list(articles)
    processed, waits = [], []
    _InlineExecutor.instances.clear()

    def process(article):
        processed.append(article[0])
        result = results[article[0]]
        return result(article) if callable(result) else result

    def recording_wait(fs, return_when):
        waits.append(len(fs))
        return real_wait(fs, return_when=return_when)

    spawn_ctx = object()
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.multiprocessing.get_context", return_value=spawn_ctx) as get_ctx, \
         patch("crawler.content_extractor.extractor.ProcessPoolExecutor", _InlineExecutor), \
         patch("crawler.content_extractor.extractor.wait", side_effect=recording_wait), \
         patch.object(ContentExtractor, "process_single_article", side_effect=process):
        stats = ContentExtractor().extract_parallel_with_stats(num_workers=num_workers, **kwargs)
    return stats, processed, {"cur": cur, "get_ctx": get_ctx, "spawn_ctx": spawn_ctx, "waits": waits}


def test_extract_parallel_uses_spawn_pool_selects_pending_and_aggregates_stats():
    """리눅스 기본값(fork)이면 부모의 커넥션 풀(열린 소켓)이 자식에 복제돼 여러 워커가
    같은 DB 소켓을 동시에 쓰게 된다 - spawn 컨텍스트로 자식마다 새 풀을 만들게 한다."""
    articles = [
        (1, "u1", "동아일보", "t1"), (2, "u2", "동아일보", "t2"),
        (3, "u3", "경향신문", "t3"), (4, "u4", "경향신문", "t4"),
    ]
    results = {
        1: ExtractionResult(1, "동아일보", "ok"),
        2: ExtractionResult(2, "동아일보", "dropped", ("too_short<350", "photo_title_short")),
        3: ExtractionResult(3, "경향신문", "dropped", ("too_short<350",)),
        4: ExtractionResult(4, "경향신문", "fetch_failed", fetch_cause="http_403"),
    }

    stats, processed, seen = _extract(articles, results, num_workers=2)

    seen["get_ctx"].assert_called_once_with("spawn")
    (executor,) = _InlineExecutor.instances
    assert (executor.max_workers, executor.mp_context) == (2, seen["spawn_ctx"])
    # 결과를 보고 다음 기사를 고르므로 한 번에 떠 있는 작업은 워커 수를 넘지 않는다
    assert seen["waits"] and max(seen["waits"]) <= 2
    select_sql, select_params = seen["cur"].execute.call_args[0]
    assert "raw_news_extract_status IS NULL" in select_sql
    assert "N.raw_news_extract_status = ANY(%s)" in select_sql
    # 본문 보존 기한(ADR 0023)이 지난 기사와 본문을 비운 기사는 내려받지 않는다
    # - (재시도할 상태, 재시도 상한, 보존 일 수 = ADR 상수 30)
    assert "raw_news_content_purged_at IS NULL" in select_sql
    assert "raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - make_interval(days => %s)" in select_sql
    assert select_params == (["fetch_failed", "error"], 3, 30)

    assert sorted(processed) == [1, 2, 3, 4]
    assert stats["targets"] == 4
    assert (stats["ok"], stats["dropped"], stats["fetch_failed"], stats["empty"], stats["error"]) == (1, 2, 1, 0, 0)
    assert stats["deferred"] == 0 and stats["deferred_reasons"] == {} and stats["press_breaker"] == {}
    assert stats["drop_reasons"] == {"too_short<350": 2, "photo_title_short": 1}
    assert stats["per_press"]["동아일보"] == {"ok": 1, "dropped": 1}
    assert stats["per_press"]["경향신문"] == {"dropped": 1, "fetch_failed": 1}
    assert stats["fetch_fail_causes"] == {"경향신문": {"http_403": 1}}
    assert stats["time_budget_exhausted"] is False


def test_extract_with_single_worker_runs_inline_without_process_pool():
    """--workers 1: 디버깅/테스트용으로 프로세스 풀 없이 같은 프로세스에서 순서대로 처리한다."""
    stats, processed, seen = _extract([(1, "u1", "동아일보", "t1")],
                                      {1: ExtractionResult(1, "동아일보", "ok")}, num_workers=1)

    seen["get_ctx"].assert_not_called()
    assert _InlineExecutor.instances == []
    assert stats["ok"] == 1 and processed == [1]


def _press_block(press, start, n, cause="disconnect"):
    return {i: ExtractionResult(i, press, "fetch_failed", fetch_cause=cause) for i in range(start, start + n)}


@pytest.mark.parametrize("workers", [1, 2])
def test_press_breaker_defers_the_rest_of_a_blocked_press_without_fetching(workers, monkeypatch):
    """2026-10-16 Tier 0: 동아일보 기사가 연달아 실패하는 동안 기사마다 약 4분씩 워커를 붙잡았다(ADR 0026 증거 10).
    연속 3건 실패하면 그 언론사의 남은 기사는 보내지 않는다 - DB에 쓰지 않으므로 시도 횟수도 쓰지 않는다."""
    from config.settings import Settings

    monkeypatch.setattr(Settings, "EXTRACT_PRESS_BREAKER_FAILURES", 3)
    # RSS가 동아일보를 먼저 넣으므로 동아일보 기사가 raw_news_id 순으로 한 덩어리다
    articles = [(i, f"u{i}", "동아일보", "t") for i in range(1, 9)] + [(i, f"u{i}", "경향신문", "t") for i in (9, 10)]
    results = {**_press_block("동아일보", 1, 8), 9: ExtractionResult(9, "경향신문", "ok"),
               10: ExtractionResult(10, "경향신문", "ok")}
    results[2] = ExtractionResult(2, "동아일보", "fetch_failed", fetch_cause="read_timeout")

    stats, processed, _ = _extract(articles, results, num_workers=workers)

    # 워커가 2개면 셋째 실패를 받을 때 이미 보낸 넷째 기사 하나는 끝까지 처리된다(요청 상한 안에서)
    fetched = 3 if workers == 1 else 4
    assert sorted(processed) == [*range(1, fetched + 1), 9, 10]  # 나머지 동아일보 기사는 요청하지 않았다
    assert stats["targets"] == 10
    assert (stats["fetch_failed"], stats["ok"], stats["deferred"]) == (fetched, 2, 8 - fetched)
    assert stats["per_press"]["동아일보"] == {"fetch_failed": fetched, "deferred": 8 - fetched}
    assert stats["per_press"]["경향신문"] == {"ok": 2}
    assert stats["fetch_fail_causes"] == {"동아일보": {"disconnect": fetched - 1, "read_timeout": 1}}
    assert stats["press_breaker"] == {"동아일보": {"failures": 3, "deferred": 8 - fetched}}
    assert stats["deferred_reasons"] == {"press_breaker": 8 - fetched}


def test_press_breaker_counts_consecutive_failures_only(monkeypatch):
    """성공이 끼면 0부터 다시 센다. 'error'(파서·DB 예외)는 네트워크 문제가 아니라 세지도 되돌리지도 않는다."""
    plan = DispatchPlan([], breaker_failures=3)
    for status in ("fetch_failed", "fetch_failed", "ok", "fetch_failed", "fetch_failed", "dropped"):
        plan.record(ExtractionResult(0, "A", status))
    assert plan.tripped == {}
    for status in ("fetch_failed", "error", "fetch_failed", "error", "fetch_failed"):
        plan.record(ExtractionResult(0, "A", status))
    assert plan.tripped == {"A": 3}
    plan.record(ExtractionResult(0, "B", "fetch_failed"))
    assert "B" not in plan.tripped  # 언론사마다 따로 센다


def test_press_breaker_can_be_disabled(monkeypatch):
    from config.settings import Settings

    monkeypatch.setattr(Settings, "EXTRACT_PRESS_BREAKER_FAILURES", 0)
    articles = [(i, f"u{i}", "동아일보", "t") for i in range(1, 6)]
    stats, processed, _ = _extract(articles, _press_block("동아일보", 1, 5), num_workers=1)
    assert processed == [1, 2, 3, 4, 5] and stats["deferred"] == 0 and stats["press_breaker"] == {}


@pytest.mark.parametrize("workers", [1, 2])
def test_time_budget_stops_dispatching_and_defers_the_rest(workers):
    """스케줄러는 앞 실행이 안 끝났으면 다음 정시 회차를 시작하지 않는다 - 예산이 지나면 새 기사를 보내지 않는다."""
    now = [0.0]

    def clock():
        return now[0]

    def slow_ok(article):
        now[0] += 10.0  # 기사 1건 처리에 10초
        return ExtractionResult(article[0], article[2], "ok")

    articles = [(i, f"u{i}", "세계일보", "t") for i in range(1, 8)]
    stats, processed, _ = _extract(articles, {i: slow_ok for i in range(1, 8)}, num_workers=workers,
                                   time_budget_s=25, clock=clock)

    # 시각 0·10·20에 보낸 3건만 처리하고, 시각 30(예산 25초 뒤)부터는 보내지 않는다
    assert processed == [1, 2, 3]
    assert stats["ok"] == 3 and stats["deferred"] == 4
    assert stats["per_press"]["세계일보"] == {"ok": 3, "deferred": 4}
    assert stats["deferred_reasons"] == {"time_budget": 4}
    assert stats["time_budget_exhausted"] is True and stats["time_budget_s"] == 25


def test_without_time_budget_everything_is_dispatched():
    articles = [(i, f"u{i}", "세계일보", "t") for i in range(1, 4)]
    stats, processed, _ = _extract(articles, {i: ExtractionResult(i, "세계일보", "ok") for i in range(1, 4)})
    assert len(processed) == 3 and stats["time_budget_exhausted"] is False and stats["time_budget_s"] is None


def test_deferred_articles_are_not_written_to_the_db():
    """미룬 기사는 상태·시도 횟수를 바꾸지 않아야 다음 실행에서 시도 상한(3회)을 그대로 쓴다.
    process_single_article(유일하게 DB에 쓰는 곳)을 부르지 않는 것으로 확인한다 - 실제 행은 integration 테스트."""
    plan = DispatchPlan([(1, "u", "A", "t"), (2, "u", "A", "t")], breaker_failures=1)
    first = plan.next_article()
    plan.record(ExtractionResult(first[0], "A", "fetch_failed"))
    assert plan.next_article() is None
    assert plan.deferred == [((2, "u", "A", "t"), "press_breaker")]
