# RSS 피드는 두 곳에 있다: 수집기가 읽는 config.settings.Settings.RSS_FEEDS와 rss_uri 테이블에 넣는
# db.schema.RSS_FEED_SEEDS. 한쪽에만 피드를 더하면 수집은 되는데 rss_uri에는 없거나 그 반대가 된다.
import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from config.settings import Settings
from config.sources import press_name_for_feed


def _seeds():
    from db import schema
    return schema.RSS_FEED_SEEDS


def test_settings_feeds_and_rss_uri_seeds_are_the_same_set():
    configured = {(press_name_for_feed(name), url) for name, (_, url) in Settings.RSS_FEEDS.items()}

    assert configured == set(_seeds())


def test_feed_keys_map_to_press_names_before_the_underscore():
    assert press_name_for_feed("전자신문_IT") == "전자신문"
    assert press_name_for_feed("국민일보_정치") == "국민일보"
    assert press_name_for_feed("국민일보") == "국민일보"
    assert press_name_for_feed("AI타임스") == "AI타임스"


def test_kmib_reads_the_all_feed_and_section_feeds():
    kmib = sorted(url for name, (_, url) in Settings.RSS_FEEDS.items() if press_name_for_feed(name) == "국민일보")

    assert "https://www.kmib.co.kr/rss/data/kmibRssAll.xml" in kmib
    assert len(kmib) == 6
