import sys
import os
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

    assert "🚫" in result
    # 저품질로 판정되면 raw_news_content는 빈 문자열로 UPDATE 되어야 함
    executed_sql, executed_params = cur.execute.call_args[0]
    assert "raw_news_content = ''" in executed_sql
    assert executed_params == (1,)
