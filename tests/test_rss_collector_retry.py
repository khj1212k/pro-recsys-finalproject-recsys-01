import sys
import os
from types import SimpleNamespace

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.rss_collector import parse_feed_with_retry


def test_parse_feed_with_retry_succeeds_after_bozo_failures():
    calls = []

    def flaky_parse(url):
        calls.append(url)
        if len(calls) < 2:
            return SimpleNamespace(bozo=1, bozo_exception=Exception("temp fail"), entries=[])
        return SimpleNamespace(bozo=0, entries=[{"title": "a"}])

    sleeps = []
    feed = parse_feed_with_retry(
        "http://example.com/rss", parse_fn=flaky_parse, max_attempts=5, sleep_fn=sleeps.append
    )

    assert feed.bozo == 0
    assert len(calls) == 2
    assert len(sleeps) == 1


def test_parse_feed_with_retry_gives_up_after_max_attempts_and_returns_last_feed():
    def always_bozo(url):
        return SimpleNamespace(bozo=1, bozo_exception=Exception("permanent fail"), entries=[])

    sleeps = []
    feed = parse_feed_with_retry(
        "http://example.com/rss", parse_fn=always_bozo, max_attempts=3, sleep_fn=sleeps.append
    )

    assert feed.bozo == 1
    assert len(sleeps) == 2


def test_default_fetch_identifies_with_project_user_agent_not_feedparser_default(monkeypatch):
    """한국경제 RSS는 feedparser 기본 UA에 403을 준다(2026-09-26 첫 수집에서 확인) -
    기본 파서는 프로젝트 UA를 명시해서 요청해야 한다."""
    import crawler.rss_collector as rss_collector

    seen = {}

    def fake_parse(url, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(bozo=0, entries=[])

    monkeypatch.setattr(rss_collector.feedparser, "parse", fake_parse)
    parse_feed_with_retry("http://example.com/rss", max_attempts=1)

    assert seen.get("agent") == rss_collector.Settings.HTTP_USER_AGENT
    assert "feedparser" not in seen["agent"].lower()


def test_unreadable_feed_error_includes_http_status():
    from unittest.mock import MagicMock, patch

    from crawler.rss_collector import collect_rss

    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (1,)
    blocked = SimpleNamespace(bozo=1, bozo_exception=Exception("undefined entity"), entries=[], status=403)

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {"차단언론": ("direct", "http://blocked/rss")}), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=blocked):
        stats = collect_rss()

    assert "HTTP 403" in stats["per_feed"]["차단언론"]["error"]
