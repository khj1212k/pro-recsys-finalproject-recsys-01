import hashlib
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.content_extractor.extractor import ContentExtractor, fetch_url_with_retry


def test_fetch_url_with_retry_succeeds_after_transient_failures():
    calls = []

    def flaky_fetch(url):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionError("temporary network failure")
        return "<html>ok</html>"

    sleeps = []
    result = fetch_url_with_retry(
        "http://example.com", fetch_fn=flaky_fetch, max_attempts=5, sleep_fn=sleeps.append
    )

    assert result == "<html>ok</html>"
    assert len(calls) == 3
    assert len(sleeps) == 2  # 성공 전까지 2번만 대기


def test_fetch_url_with_retry_gives_up_after_max_attempts():
    def always_none(url):
        return None

    sleeps = []
    result = fetch_url_with_retry(
        "http://example.com", fetch_fn=always_none, max_attempts=3, sleep_fn=sleeps.append
    )

    assert result is None
    assert len(sleeps) == 2  # 3번 시도, 마지막 시도 후에는 대기 안 함


def test_process_single_article_drops_low_quality_content():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur

    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_url_with_retry", return_value="<html/>"), \
         patch("crawler.content_extractor.extractor.trafilatura.extract", return_value="너무 짧은 본문"):

        # (raw_news_id, url, press_name, title) - "너무 짧은 본문"은 DROP_LEN(350)보다 훨씬 짧음
        result = ContentExtractor.process_single_article((1, "http://example.com", "테스트언론사", "일반 제목"))

    assert result.status == "dropped"
    assert any(r.startswith("too_short") for r in result.reasons)
    # 저품질로 판정되면 raw_news_content는 빈 문자열, 추출 상태는 'dropped'로 기록돼야
    # 다음 실행에서 같은 기사를 다시 내려받지 않는다.
    executed_sql, executed_params = cur.execute.call_args[0]
    assert "raw_news_extract_status" in executed_sql
    assert executed_params == ("", "dropped", None, 1)


def _run_single(fetch_return=None, extract_return=None, fetch_exc=None):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    fetch_patch = (
        patch("crawler.content_extractor.extractor.fetch_url_with_retry", side_effect=fetch_exc)
        if fetch_exc else
        patch("crawler.content_extractor.extractor.fetch_url_with_retry", return_value=fetch_return)
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
    content, status, digest, raw_news_id = params
    assert status == "ok" and raw_news_id == 7
    assert len(content) > 350
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
         patch("crawler.content_extractor.extractor.fetch_url_with_retry", return_value="<html/>"), \
         patch("crawler.content_extractor.extractor.trafilatura.extract", return_value=long_body):
        result = ContentExtractor.process_single_article((8, "http://example.com/b", "동아일보", "일반 제목"))

    assert result.status == "duplicate"
    (_, first), (_, second) = [c[0] for c in cur.execute.call_args_list]
    assert first[1] == "ok"
    assert second == ("", "duplicate", first[2], 8)
    conn.rollback.assert_called_once()
    conn.commit.assert_called_once()


def test_process_single_article_marks_fetch_failure_for_bounded_retry():
    result, conn, cur = _run_single(fetch_return=None)

    assert result.status == "fetch_failed"
    _, params = cur.execute.call_args[0]
    assert params == ("", "fetch_failed", None, 7)


def test_process_single_article_marks_empty_when_nothing_extracted():
    result, conn, cur = _run_single(fetch_return="<html/>", extract_return=None)

    assert result.status == "empty"
    _, params = cur.execute.call_args[0]
    assert params == ("", "empty", None, 7)


def test_process_single_article_records_error_so_retries_are_bounded():
    """예외(파서 크래시 등)도 시도 횟수를 올려 fetch_failed처럼 상한까지만 다시 시도한다 -
    상태를 남기지 않으면 같은 URL을 매시 영원히 다시 받는다."""
    result, conn, cur = _run_single(fetch_exc=RuntimeError("parser crashed"))

    assert result.status == "error"
    assert "parser crashed" in result.detail
    conn.rollback.assert_called_once()
    sql, params = cur.execute.call_args[0]
    assert "raw_news_extract_attempts + 1" in sql
    assert params == ("", "error", None, 7)
    conn.commit.assert_called_once()


def test_process_single_article_error_still_reported_when_status_cannot_be_recorded():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.execute.side_effect = RuntimeError("db gone")
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_url_with_retry", return_value=None):
        result = ContentExtractor.process_single_article((9, "http://example.com/c", "동아일보", "제목"))

    assert result.status == "error" and "db gone" in result.detail
    assert conn.rollback.call_count == 2


class _InlinePool:
    """spawn 컨텍스트의 Pool 대역: 같은 프로세스에서 순서대로 실행한다."""

    def __init__(self, processes=None):
        self.processes = processes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def imap_unordered(self, fn, items):
        return map(fn, items)


def test_extract_parallel_uses_spawn_pool_selects_pending_and_aggregates_stats():
    """리눅스 기본값(fork)이면 부모의 커넥션 풀(열린 소켓)이 자식에 복제돼 여러 워커가
    같은 DB 소켓을 동시에 쓰게 된다 - spawn 컨텍스트로 자식마다 새 풀을 만들게 한다."""
    from crawler.content_extractor.extractor import ExtractionResult

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.fetchall.return_value = [
        (1, "u1", "동아일보", "t1"), (2, "u2", "동아일보", "t2"),
        (3, "u3", "경향신문", "t3"), (4, "u4", "경향신문", "t4"),
    ]
    results = {
        1: ExtractionResult(1, "동아일보", "ok"),
        2: ExtractionResult(2, "동아일보", "dropped", ("too_short<350", "photo_title_short")),
        3: ExtractionResult(3, "경향신문", "dropped", ("too_short<350",)),
        4: ExtractionResult(4, "경향신문", "fetch_failed"),
    }

    fake_ctx = MagicMock()
    fake_ctx.Pool = _InlinePool

    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.multiprocessing.get_context", return_value=fake_ctx) as get_ctx, \
         patch.object(ContentExtractor, "process_single_article", side_effect=lambda a: results[a[0]]):
        stats = ContentExtractor().extract_parallel_with_stats(num_workers=2)

    get_ctx.assert_called_once_with("spawn")
    select_sql, select_params = cur.execute.call_args[0]
    assert "raw_news_extract_status IS NULL" in select_sql
    assert "fetch_failed" in select_sql and "'error'" in select_sql
    assert select_params == (3,)

    assert stats["targets"] == 4
    assert (stats["ok"], stats["dropped"], stats["fetch_failed"], stats["empty"], stats["error"]) == (1, 2, 1, 0, 0)
    assert stats["drop_reasons"] == {"too_short<350": 2, "photo_title_short": 1}
    assert stats["per_press"]["동아일보"] == {"ok": 1, "dropped": 1}
    assert stats["per_press"]["경향신문"] == {"dropped": 1, "fetch_failed": 1}


def test_extract_with_single_worker_runs_inline_without_process_pool():
    """--workers 1: 디버깅/테스트용으로 프로세스 풀 없이 같은 프로세스에서 순서대로 처리한다."""
    from crawler.content_extractor.extractor import ExtractionResult

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.fetchall.return_value = [(1, "u1", "동아일보", "t1")]

    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.multiprocessing.get_context") as get_ctx, \
         patch.object(ContentExtractor, "process_single_article",
                      side_effect=lambda a: ExtractionResult(a[0], a[2], "ok")):
        stats = ContentExtractor().extract_parallel_with_stats(num_workers=1)

    get_ctx.assert_not_called()
    assert stats["ok"] == 1
