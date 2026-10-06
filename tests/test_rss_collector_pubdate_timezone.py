"""시간대 없는 pubDate는 KST로 읽고, 시간대가 붙은 pubDate는 그대로 둔다 (docs/adr/0008).

아래 발행 시각 문자열은 2026-10-06 12:16 KST에 실제 피드에서 본 형식이다. AI타임스만 시간대를
빼고 내보내고("2026-10-06 09:00:00"), 그 값은 KST 벽시계 시각이다. 예전에는 여기에 UTC를 붙여
news_raw.raw_news_created_at이 9시간 뒤(수집 시각보다 미래)로 저장됐다.
"""
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.rss_collector import collect_rss

# 수집 시각을 고정한다: 2026-10-06 03:16 UTC = 12:16 KST.
# hours=100이면 cutoff는 2026-10-01 23:16 UTC = 2026-10-02 08:16 KST.
NOW_UTC = datetime(2026, 10, 6, 3, 16, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW_UTC.astimezone(tz) if tz is not None else NOW_UTC.replace(tzinfo=None)


class FakeEntry(dict):
    """feedparser 엔트리 흉내: e.get('published')과 e.link/e.title 둘 다 필요."""

    def __init__(self, link, published):
        super().__init__(published=published)
        self.link = link
        self.title = "기사"


def _collect(published_by_link, hours=100):
    """고정한 시각에 collect_rss를 돌려 (통계, {URL: 저장하려던 raw_news_created_at})을 돌려준다."""
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = (1,)
    feed = MagicMock()
    feed.bozo = 0
    feed.entries = [FakeEntry(link, published) for link, published in published_by_link.items()]

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.datetime", _FrozenDatetime), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", {"AI타임스": ("direct", "http://x/rss")}), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=feed), \
         patch("crawler.rss_collector.execute_values", return_value=[]) as mock_execute_values:
        stats = collect_rss(hours=hours)

    stored = {}
    for call in mock_execute_values.call_args_list:
        # 행 모양: (press_id, title, content, url, raw_news_created_at, raw_news_crawled_at)
        stored.update({row[3]: row[4] for row in call.args[2]})
    return stats["per_feed"]["AI타임스"], stored


def test_naive_pubdate_is_stored_as_kst_wall_clock_time():
    _, stored = _collect({"http://a/1": "2026-10-06 09:00:00"})

    # 09:00 KST = 00:00 UTC. UTC로 읽으면 수집 시각(03:16 UTC)보다 5시간 44분 뒤가 된다.
    assert stored["http://a/1"] == datetime(2026, 10, 6, 0, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "published, expected_utc",
    [
        # 동아일보·한국경제·전자신문
        ("Tue, 06 Oct 2026 12:14:35 +0900", datetime(2026, 10, 6, 3, 14, 35, tzinfo=timezone.utc)),
        # 매일경제
        ("Tue, 06 Oct 2026 12:01:23 +09:00", datetime(2026, 10, 6, 3, 1, 23, tzinfo=timezone.utc)),
        # 세계일보 (쉼표 뒤 공백 없음)
        ("Tue,6 Oct 2026 11:52:28 +0900", datetime(2026, 10, 6, 2, 52, 28, tzinfo=timezone.utc)),
        # 경향신문 (dc:date)
        ("2026-10-06T12:00:00+09:00", datetime(2026, 10, 6, 3, 0, 0, tzinfo=timezone.utc)),
        # 국민일보 (GMT로 내보냄 - KST로 바꿔 읽으면 9시간 앞당겨진다)
        ("6 Oct  2026 03:11:00 GMT", datetime(2026, 10, 6, 3, 11, 0, tzinfo=timezone.utc)),
    ],
)
def test_pubdate_with_timezone_keeps_its_own_instant(published, expected_utc):
    _, stored = _collect({"http://a/1": published})

    assert stored["http://a/1"] == expected_utc


def test_cutoff_is_applied_to_naive_pubdate_read_as_kst():
    feed_stats, stored = _collect(
        {
            # 05:00 KST = 10-01 20:00 UTC: cutoff보다 3시간 16분 앞. UTC로 읽으면 cutoff 안으로 들어온다.
            "http://a/stale": "2026-10-02 05:00:00",
            # 09:00 KST = 10-02 00:00 UTC: cutoff보다 44분 뒤.
            "http://a/fresh": "2026-10-02 09:00:00",
        },
        hours=100,
    )

    assert feed_stats["too_old"] == 1
    assert stored == {"http://a/fresh": datetime(2026, 10, 2, 0, 0, 0, tzinfo=timezone.utc)}
