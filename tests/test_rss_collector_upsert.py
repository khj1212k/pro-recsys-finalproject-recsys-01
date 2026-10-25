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

    assert (stats["inserted"], stats["skipped"]) == (2, 1)

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

    assert (stats["inserted"], stats["skipped"]) == (1, 0)
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
    assert (stats["inserted"], stats["skipped"]) == (0, 0)


def test_collect_rss_reports_per_feed_breakdown_of_new_duplicate_stale_and_undated_entries():
    conn, cur = _make_cursor_conn()
    cur.fetchone.return_value = (1,)

    old_dt = (datetime.now(timezone.utc) - timedelta(hours=200)).strftime("%a, %d %b %Y %H:%M:%S %z")
    fake_feed = MagicMock()
    fake_feed.bozo = 0
    fake_feed.entries = [
        FakeEntry("http://a.com/new", "신규", _recent()),
        FakeEntry("http://a.com/dup", "중복", _recent()),
        FakeEntry("http://a.com/old", "오래됨", old_dt),
        FakeEntry("http://a.com/nodate", "날짜깨짐", "not a date"),
    ]

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {"전자신문_IT": ("direct", "http://x/rss")}), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=fake_feed), \
         patch("crawler.rss_collector.execute_values", return_value=[("http://a.com/new",)]):
        stats = collect_rss(hours=100)

    feed = stats["per_feed"]["전자신문_IT"]
    assert feed["press"] == "전자신문"
    assert feed["entries"] == 4
    assert feed["inserted"] == 1
    assert feed["skipped"] == 1
    assert feed["too_old"] == 1
    assert feed["bad_date"] == 1
    assert feed["error"] is None


def test_collect_rss_records_feed_error_per_feed_and_counts_failed_feeds():
    conn, cur = _make_cursor_conn()
    cur.fetchone.return_value = (1,)

    def parse_feed_side_effect(url):
        raise RuntimeError("network boom")

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {"깨진언론": ("direct", "http://broken/rss")}), \
         patch("crawler.rss_collector.parse_feed_with_retry", side_effect=parse_feed_side_effect):
        stats = collect_rss()

    assert "network boom" in stats["per_feed"]["깨진언론"]["error"]
    assert stats["failed_feeds"] == 1


def test_collect_rss_flags_unreachable_feed_that_returns_bozo_without_entries():
    conn, cur = _make_cursor_conn()
    cur.fetchone.return_value = (1,)

    bozo_feed = MagicMock()
    bozo_feed.bozo = 1
    bozo_feed.bozo_exception = OSError("connection refused")
    bozo_feed.entries = []

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {"먹통언론": ("direct", "http://down/rss")}), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=bozo_feed):
        stats = collect_rss()

    assert "connection refused" in stats["per_feed"]["먹통언론"]["error"]
    assert stats["failed_feeds"] == 1


def test_feeds_on_a_host_that_failed_earlier_in_the_run_are_skipped_without_retry():
    # 같은 호스트의 피드가 여럿(국민일보 여섯·전자신문 셋)일 때 사이트 하나가 막히면 피드마다 재시도를 곱하지 않는다
    conn, cur = _make_cursor_conn()
    cur.fetchone.return_value = (1,)

    bozo_feed = MagicMock()
    bozo_feed.bozo = 1
    bozo_feed.bozo_exception = OSError("timed out")
    bozo_feed.entries = []
    ok_feed = MagicMock()
    ok_feed.bozo = 0
    ok_feed.entries = []
    calls = []

    def fake_parse(url):
        calls.append(url)
        return bozo_feed if "down.example" in url else ok_feed

    feeds = {
        "막힌언론": ("direct", "http://down.example/all.xml"),
        "막힌언론_정치": ("direct", "http://down.example/pol.xml"),
        "막힌언론_경제": ("direct", "http://down.example/eco.xml"),
        "멀쩡언론": ("direct", "http://up.example/rss"),
    }
    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", feeds), \
         patch("crawler.rss_collector.parse_feed_with_retry", side_effect=fake_parse):
        stats = collect_rss()

    assert calls == ["http://down.example/all.xml", "http://up.example/rss"]
    assert "timed out" in stats["per_feed"]["막힌언론"]["error"]
    for name in ("막힌언론_정치", "막힌언론_경제"):
        assert stats["per_feed"][name]["error"].startswith("skipped: down.example")
    assert stats["per_feed"]["멀쩡언론"]["error"] is None


# ---------------------------------------------------------------------------- 피드 부분집합 (jobs.run rss_retry, ADR 0026 2026-10-25 갱신 2)


def _subset_run(feeds, selected, parse_calls):
    """feeds 설정에서 selected(None이면 전부)만 읽는다. 읽은 URL은 parse_calls에 담긴다."""
    conn, cur = _make_cursor_conn()
    cur.fetchone.return_value = (1,)
    ok_feed = MagicMock()
    ok_feed.bozo = 0
    ok_feed.entries = []
    bozo_feed = MagicMock()
    bozo_feed.bozo = 1
    bozo_feed.bozo_exception = OSError("timed out")
    bozo_feed.entries = []

    def fake_parse(url):
        parse_calls.append(url)
        return bozo_feed if "down.example" in url else ok_feed

    with patch("crawler.rss_collector.get_connection", return_value=conn) as get_conn, \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", feeds), \
         patch("crawler.rss_collector.parse_feed_with_retry", side_effect=fake_parse):
        stats = collect_rss() if selected is None else collect_rss(feeds=selected)
    return stats, get_conn


SUBSET_FEEDS = {
    "동아일보": ("rss_summary", "http://donga.example/total.xml"),
    "경향신문": ("direct", "http://khan.example/rss"),
    "막힌언론": ("direct", "http://down.example/all.xml"),
    "막힌언론_정치": ("direct", "http://down.example/pol.xml"),
    "세계일보": ("direct", "http://segye.example/rss"),
}


def test_collect_rss_without_feeds_reads_every_configured_feed():
    calls = []

    stats, _ = _subset_run(SUBSET_FEEDS, None, calls)

    assert calls == ["http://donga.example/total.xml", "http://khan.example/rss", "http://down.example/all.xml",
                     "http://segye.example/rss"]  # 막힌언론_정치는 같은 호스트라 건너뜀
    assert list(stats["per_feed"]) == list(SUBSET_FEEDS)


def test_collect_rss_reads_only_the_named_feeds_in_configured_order():
    calls = []

    stats, _ = _subset_run(SUBSET_FEEDS, ["세계일보", "동아일보"], calls)  # 준 순서가 아니라 설정 순서

    assert calls == ["http://donga.example/total.xml", "http://segye.example/rss"]
    assert list(stats["per_feed"]) == ["동아일보", "세계일보"]
    assert set(stats) == {"inserted", "skipped", "failed_feeds", "per_feed"}  # 통계 키는 전체 수집과 같다
    assert set(stats["per_feed"]["동아일보"]) >= {"press", "entries", "inserted", "skipped", "too_old", "bad_date", "error"}
    assert stats["failed_feeds"] == 0


def test_collect_rss_with_an_empty_feed_list_reads_nothing_and_does_not_open_the_database():
    calls = []

    stats, get_conn = _subset_run(SUBSET_FEEDS, [], calls)

    assert calls == []
    get_conn.assert_not_called()
    assert stats == {"inserted": 0, "skipped": 0, "failed_feeds": 0, "per_feed": {}}


def test_collect_rss_rejects_an_unknown_feed_name_before_touching_anything():
    import pytest

    calls = []
    with pytest.raises(ValueError, match="없는피드"):
        _subset_run(SUBSET_FEEDS, ["동아일보", "없는피드"], calls)

    assert calls == []


def test_same_host_skip_rule_applies_inside_the_named_subset():
    calls = []

    stats, _ = _subset_run(SUBSET_FEEDS, ["막힌언론", "막힌언론_정치"], calls)

    assert calls == ["http://down.example/all.xml"]  # 첫 피드만 요청하고 같은 호스트의 다음 피드는 요청 없이 건너뛴다
    assert "timed out" in stats["per_feed"]["막힌언론"]["error"]
    assert stats["per_feed"]["막힌언론_정치"]["error"].startswith("skipped: down.example")
    assert stats["failed_feeds"] == 2


def test_a_feed_skipped_for_its_host_is_not_blocked_by_a_failure_outside_the_subset():
    # 전체 수집에서 막힌언론이 실패했어도, 부분집합으로 막힌언론_정치만 다시 읽으면 그 피드를 실제로 읽는다
    calls = []

    stats, _ = _subset_run(SUBSET_FEEDS, ["막힌언론_정치"], calls)

    assert calls == ["http://down.example/pol.xml"]
    assert "timed out" in stats["per_feed"]["막힌언론_정치"]["error"]
