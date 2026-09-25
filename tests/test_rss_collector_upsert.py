import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.rss_collector import collect_rss


class FakeEntry(dict):
    """feedparser 엔트리 흉내: e.get('published')과 e.link/e.title 둘 다 필요."""

    def __init__(self, link, title, published):
        super().__init__(published=published)
        self.link = link
        self.title = title


def _make_cursor_conn():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


def _recent(hours_ago=1):
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def test_collect_rss_uses_on_conflict_do_nothing_and_counts_inserted_vs_skipped():
    conn, cur = _make_cursor_conn()
    cur.fetchone.return_value = (1,)  # press_id 조회 결과

    fake_feed = MagicMock()
    fake_feed.entries = [
        FakeEntry("http://a.com/1", "기사1", _recent()),
        FakeEntry("http://a.com/2", "기사2", _recent()),
        FakeEntry("http://a.com/3", "기사3", _recent()),
    ]

    # 3건 중 2건만 실제로 삽입되고(1건은 이미 존재해 ON CONFLICT로 스킵) RETURNING된 상황을 흉내낸다.
    fake_execute_values_return = [("http://a.com/1",), ("http://a.com/2",)]

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {"테스트언론": ("direct", "http://x/rss")}), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=fake_feed), \
         patch("crawler.rss_collector.execute_values", return_value=fake_execute_values_return) as mock_execute_values:
        stats = collect_rss()

    assert stats == {"inserted": 2, "skipped": 1}

    # ON CONFLICT DO NOTHING + RETURNING을 쓰는 단일 INSERT로 처리했는지 확인
    assert mock_execute_values.call_count == 1
    _, args, kwargs = mock_execute_values.mock_calls[0]
    executed_sql = args[1]
    assert "INSERT INTO news_raw" in executed_sql
    assert "ON CONFLICT (raw_news_url) DO NOTHING" in executed_sql
    assert "RETURNING raw_news_url" in executed_sql
    assert kwargs.get("fetch") is True

    conn.commit.assert_called_once()


def test_collect_rss_keeps_per_press_savepoint_isolation_on_error():
    conn, cur = _make_cursor_conn()

    call_state = {"n": 0}

    def fetchone_side_effect():
        call_state["n"] += 1
        return (call_state["n"],)  # 언론사마다 다른 press_id

    cur.fetchone.side_effect = fetchone_side_effect

    fake_feed_ok = MagicMock()
    fake_feed_ok.entries = [FakeEntry("http://b.com/1", "기사", _recent())]

    def parse_feed_side_effect(url):
        if url == "http://broken/rss":
            raise RuntimeError("network boom")
        return fake_feed_ok

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {
             "깨진언론": ("direct", "http://broken/rss"),
             "정상언론": ("direct", "http://ok/rss"),
         }), \
         patch("crawler.rss_collector.parse_feed_with_retry", side_effect=parse_feed_side_effect), \
         patch("crawler.rss_collector.execute_values", return_value=[("http://b.com/1",)]) as mock_execute_values:
        stats = collect_rss()

    # 깨진 언론사는 SAVEPOINT까지 롤백되고, 정상 언론사는 정상 처리된다.
    rollback_calls = [c for c in cur.execute.call_args_list if c[0][0] == "ROLLBACK TO SAVEPOINT sp_press"]
    assert len(rollback_calls) == 1
    savepoint_calls = [c for c in cur.execute.call_args_list if c[0][0] == "SAVEPOINT sp_press"]
    assert len(savepoint_calls) == 2

    assert stats == {"inserted": 1, "skipped": 0}
    mock_execute_values.assert_called_once()
    conn.commit.assert_called_once()


def test_collect_rss_skips_entries_older_than_cutoff_without_inserting():
    conn, cur = _make_cursor_conn()
    cur.fetchone.return_value = (1,)

    old_dt = (datetime.now(timezone.utc) - timedelta(hours=200)).strftime("%a, %d %b %Y %H:%M:%S %z")
    fake_feed = MagicMock()
    fake_feed.entries = [FakeEntry("http://old.com/1", "오래된기사", old_dt)]

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {"테스트언론": ("direct", "http://x/rss")}), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=fake_feed), \
         patch("crawler.rss_collector.execute_values") as mock_execute_values:
        stats = collect_rss(hours=100)

    mock_execute_values.assert_not_called()
    assert stats == {"inserted": 0, "skipped": 0}
