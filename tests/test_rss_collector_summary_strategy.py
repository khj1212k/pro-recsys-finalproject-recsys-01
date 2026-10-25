# 'rss_summary' 피드(동아일보 - VM 출발지의 본문 요청을 거절한다)는 본문을 받지 않고 RSS 요약을 본문 칸에 바로 넣는다.
# 지어낸 텍스트만 쓴다.
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from config.retention import RETRYABLE_EXTRACT_STATUSES
from crawler.rss_collector import RSS_SUMMARY_STATUS, collect_rss, rss_summary_text


class FakeEntry(dict):
    def __init__(self, link, title, published, summary=None):
        super().__init__(published=published)
        if summary is not None:
            self["summary"] = summary
        self.link = link
        self.title = title


def _recent():
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S %z")


def _run(feeds, entries):
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    cur.fetchone.return_value = (1,)
    feed = MagicMock()
    feed.entries = entries
    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", feeds), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=feed), \
         patch("crawler.rss_collector.execute_values", return_value=[]) as ev:
        collect_rss()
    _, args, _ = ev.mock_calls[0]
    return args[1], args[2]


def test_summary_text_strips_tags_entities_and_whitespace():
    e = {"summary": "<p>지어낸&nbsp;요약 <b>첫 문장</b>.\n둘째 &amp; 셋째</p>"}
    assert rss_summary_text(e) == "지어낸 요약 첫 문장 . 둘째 & 셋째"
    assert rss_summary_text({}) == ""


def test_rss_summary_feed_stores_the_summary_as_content_and_closes_extraction():
    summary = "지어낸 요약 문장이다. " * 10
    sql, rows = _run({"요약언론": ("rss_summary", "http://x/rss")},
                     [FakeEntry("http://s.example/1", "제목", _recent(), summary=summary)])
    assert "raw_news_extract_status" in sql and "raw_news_content_sha256" in sql
    (pid, title, content, link, _dt, _crawled, status, digest, length, extracted_at), = rows
    assert content == rss_summary_text({"summary": summary})
    assert status == RSS_SUMMARY_STATUS
    assert digest == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert length == len(content)
    assert extracted_at is not None


def test_rss_summary_feed_with_empty_summary_falls_back_to_normal_extraction():
    _, rows = _run({"요약언론": ("rss_summary", "http://x/rss")},
                   [FakeEntry("http://s.example/2", "제목", _recent(), summary="  ")])
    (_, _, content, _, _, _, status, digest, length, extracted_at), = rows
    assert (content, status, digest, length, extracted_at) == ("", None, None, None, None)


def test_direct_feeds_are_unchanged_even_if_the_feed_has_a_summary():
    _, rows = _run({"직접언론": ("direct", "http://x/rss")},
                   [FakeEntry("http://d.example/1", "제목", _recent(), summary="지어낸 요약")])
    (_, _, content, _, _, _, status, *_rest), = rows
    assert (content, status) == ("", None)


def test_rss_summary_rows_are_not_picked_up_again_by_the_extractor():
    # 추출기 대상 = 상태 NULL 또는 재시도 가능한 상태(crawler/content_extractor/extractor.py, jobs/tasks/retention.py)
    assert RSS_SUMMARY_STATUS not in RETRYABLE_EXTRACT_STATUSES


def test_donga_uses_the_summary_strategy():
    from config.settings import Settings
    assert Settings.RSS_FEEDS["동아일보"][0] == "rss_summary"
