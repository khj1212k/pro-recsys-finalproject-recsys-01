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
