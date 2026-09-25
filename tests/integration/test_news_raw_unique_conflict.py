"""backend/alembic/versions/e725a62ffef1_...py가 news_raw.raw_news_url에 추가한
UNIQUE 제약과, ai_workspace/crawler/rss_collector.py가 쓰는
INSERT ... ON CONFLICT (raw_news_url) DO NOTHING 동작을 실제 DB에서 검증한다.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import psycopg2
import pytest


class _FakeEntry(dict):
    def __init__(self, link, title, published):
        super().__init__(published=published)
        self.link = link
        self.title = title


def _seed_press(pg_conn, press_name):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO press (press_name) VALUES (%s) RETURNING press_id",
            (press_name,),
        )
        return cur.fetchone()[0]


def _cleanup(pg_conn, press_id=None, url=None):
    with pg_conn.cursor() as cur:
        if url:
            cur.execute("DELETE FROM news_raw WHERE raw_news_url = %s", (url,))
        if press_id:
            cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))


def test_raw_news_url_unique_constraint_rejects_plain_duplicate_insert(database_url, pg_conn):
    press_id = _seed_press(pg_conn, f"unique-test-press-{uuid.uuid4().hex[:8]}")
    url = f"http://example.com/{uuid.uuid4().hex}"

    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at) "
                "VALUES (%s, %s, %s, %s, NOW())",
                (press_id, "제목1", "", url),
            )

        with pytest.raises(psycopg2.errors.UniqueViolation):
            with pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at) "
                    "VALUES (%s, %s, %s, %s, NOW())",
                    (press_id, "제목2", "", url),
                )
    finally:
        _cleanup(pg_conn, press_id=press_id, url=url)


def test_raw_news_url_on_conflict_do_nothing_skips_duplicate_without_error(database_url, pg_conn):
    press_id = _seed_press(pg_conn, f"conflict-test-press-{uuid.uuid4().hex[:8]}")
    url = f"http://example.com/{uuid.uuid4().hex}"

    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at) "
                "VALUES (%s, %s, %s, %s, NOW()) ON CONFLICT (raw_news_url) DO NOTHING",
                (press_id, "제목1", "", url),
            )
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at) "
                "VALUES (%s, %s, %s, %s, NOW()) ON CONFLICT (raw_news_url) DO NOTHING",
                (press_id, "제목2-중복", "", url),
            )
            cur.execute(
                "SELECT COUNT(*), MIN(raw_news_title) FROM news_raw WHERE raw_news_url = %s", (url,)
            )
            count, title = cur.fetchone()

        assert count == 1
        assert title == "제목1"
    finally:
        _cleanup(pg_conn, press_id=press_id, url=url)


def test_collect_rss_end_to_end_is_idempotent_against_real_db(database_url, pg_conn, monkeypatch):
    """collect_rss()를 같은 기사에 대해 두 번 실행해도(재수집 시나리오) 실제 DB에서
    UniqueViolation 없이 두 번째 실행은 skipped로 집계되고, 행은 1건만 남아야 한다."""
    from crawler.rss_collector import collect_rss
    from config.settings import Settings

    press_name = f"e2e-press-{uuid.uuid4().hex[:8]}"
    press_id = _seed_press(pg_conn, press_name)

    unique_link = f"http://example.com/{uuid.uuid4().hex}"
    published = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")

    fake_feed = MagicMock()
    fake_feed.entries = [_FakeEntry(unique_link, "E2E 기사", published)]

    monkeypatch.setattr(Settings, "RSS_FEEDS", {press_name: ("direct", "http://fake/rss")})

    try:
        with patch("crawler.rss_collector.parse_feed_with_retry", return_value=fake_feed):
            first_stats = collect_rss()
            second_stats = collect_rss()

        assert first_stats["inserted"] == 1
        assert first_stats["skipped"] == 0
        assert second_stats["inserted"] == 0
        assert second_stats["skipped"] == 1

        with pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM news_raw WHERE raw_news_url = %s", (unique_link,))
            (count,) = cur.fetchone()
        assert count == 1
    finally:
        _cleanup(pg_conn, press_id=press_id, url=unique_link)
