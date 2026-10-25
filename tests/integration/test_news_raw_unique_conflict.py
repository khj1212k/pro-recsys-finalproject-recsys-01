"""backend/alembic/versions/e725a62ffef1_...py가 news_raw.raw_news_url에 추가한
UNIQUE 제약과, ai_workspace/crawler/rss_collector.py가 쓰는
INSERT ... ON CONFLICT (raw_news_url) DO NOTHING 동작을 실제 DB에서 검증한다.
"""
import hashlib
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import psycopg2
import pytest


class _FakeEntry(dict):
    def __init__(self, link, title, published, summary=None):
        super().__init__(published=published)
        if summary is not None:
            self["summary"] = summary
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


def test_collect_rss_summary_feed_inserts_summary_rows_against_real_db(database_url, pg_conn, monkeypatch):
    """'rss_summary' 피드(동아일보)는 요약을 본문 칸에 넣는다. 실제 DB에서 확인한다:
    - 'rss_summary'·'dropped'·'duplicate' 상태가 CHECK 제약(ck_news_raw_extract_status)을 통과한다 - 제약에 없으면 피드째
      CheckViolation으로 INSERT가 0건이 된다(b7e4a1c93d52 전에는 그랬다)
    - 같은 요약(URL만 다른 기사)과 'ok' 행의 본문과 같은 요약은 'duplicate', 짧거나 없는 요약은 'dropped'
    - 다시 돌려도 멱등이고, 앞 실행이 넣은 'rss_summary' 행과 같은 요약도 'duplicate'다"""
    from crawler.rss_collector import collect_rss, rss_summary_text
    from config.settings import Settings

    suffix = uuid.uuid4().hex[:8]
    press_name = f"summary-press-{suffix}"
    press_id = _seed_press(pg_conn, press_name)
    published = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    url = lambda key: f"http://example.com/{suffix}/{key}"  # noqa: E731

    def summary(tag):
        return f"{suffix} {tag} 지어낸 요약 문장이다. " * 25  # 350자를 넘는다

    def digest(text):
        return hashlib.sha256(rss_summary_text({"summary": text}).encode("utf-8")).hexdigest()

    ok_summary = summary("ok")
    monkeypatch.setattr(Settings, "RSS_FEEDS", {press_name: ("rss_summary", "http://fake/rss")})

    def run(entries):
        feed = MagicMock()
        feed.entries = entries
        with patch("crawler.rss_collector.parse_feed_with_retry", return_value=feed):
            return collect_rss()

    def rows():
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT raw_news_url, raw_news_extract_status, raw_news_content, raw_news_content_sha256, "
                "raw_news_content_length, raw_news_extracted_at IS NOT NULL, raw_news_extract_attempts "
                "FROM news_raw WHERE press_id = %s", (press_id,))
            return {r[0]: r[1:] for r in cur.fetchall()}

    try:
        # 본문 추출기가 이미 'ok'로 저장한 행 - 같은 본문의 요약은 uq_news_raw_content_sha256_ok 밖이라 수집기가 중복으로 본다
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at, "
                "raw_news_extract_status, raw_news_content_sha256, raw_news_extract_attempts) "
                "VALUES (%s, 't', %s, %s, NOW(), 'ok', %s, 1)",
                (press_id, rss_summary_text({"summary": ok_summary}), url("ok"), digest(ok_summary)))

        first = run([
            _FakeEntry(url("a"), "기사A", published, summary=summary("a")),
            _FakeEntry(url("a-copy"), "기사A 다른 경로", published, summary=summary("a")),
            _FakeEntry(url("same-as-ok"), "기사C", published, summary=ok_summary),
            _FakeEntry(url("short"), "기사D", published, summary="지어낸 짧은 요약이다."),
            _FakeEntry(url("none"), "기사E", published),
        ])
        assert first["per_feed"][press_name]["error"] is None
        assert (first["inserted"], first["skipped"]) == (5, 0)
        assert first["per_feed"][press_name]["summary"] == {"stored": 1, "dropped": 2, "duplicate": 2}

        got = rows()
        a_text = rss_summary_text({"summary": summary("a")})
        assert got[url("a")] == ("rss_summary", a_text, digest(summary("a")), len(a_text), True, 0)
        assert got[url("a-copy")] == ("duplicate", "", digest(summary("a")), 0, True, 0)
        assert got[url("same-as-ok")] == ("duplicate", "", digest(ok_summary), 0, True, 0)
        assert got[url("short")] == ("dropped", "", None, 0, True, 0)
        assert got[url("none")] == ("dropped", "", None, 0, True, 0)

        # 같은 피드를 다시 읽어도 새 행이 없다 - 이미 들어간 URL은 건너뛴다
        second = run([_FakeEntry(url("a"), "기사A", published, summary=summary("a"))])
        assert (second["inserted"], second["skipped"]) == (0, 1)
        assert second["per_feed"][press_name]["summary"] == {"stored": 0, "dropped": 0, "duplicate": 0}

        # 앞 실행이 넣은 'rss_summary' 행과 같은 요약이 새 URL로 나오면 데이터베이스 쪽 조회로 중복을 잡는다
        third = run([_FakeEntry(url("a-again"), "기사A 또 다른 경로", published, summary=summary("a"))])
        assert third["inserted"] == 1
        assert rows()[url("a-again")][:2] == ("duplicate", "")
        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM news_raw WHERE press_id = %s AND raw_news_extract_status = 'rss_summary'",
                        (press_id,))
            assert cur.fetchone() == (1,)

        # 배포 전에 들어온 행(이미 있는 URL, 상태 NULL·본문 '')이 같은 묶음의 새 URL보다 먼저 요약 해시를 차지하지 않는다 -
        # 그 행은 ON CONFLICT로 건너뛰어 요약을 받지 못하므로, 새 URL이 'duplicate'가 되면 그 기사는 어느 행에도 본문이 없다
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at) "
                "VALUES (%s, '배포 전', '', %s, NOW())", (press_id, url("pre-deploy")))
        fourth = run([
            _FakeEntry(url("pre-deploy"), "배포 전", published, summary=summary("p")),
            _FakeEntry(url("fresh-same-summary"), "새 기사", published, summary=summary("p")),
        ])
        assert (fourth["inserted"], fourth["skipped"]) == (1, 1)
        assert fourth["per_feed"][press_name]["summary"] == {"stored": 1, "dropped": 0, "duplicate": 0}
        got = rows()
        assert got[url("pre-deploy")][:2] == (None, "")  # 기존 행은 그대로다(수집기는 기존 행을 고치지 않는다)
        assert got[url("fresh-same-summary")][0] == "rss_summary"
        assert got[url("fresh-same-summary")][1] == rss_summary_text({"summary": summary("p")})
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE press_id = %s", (press_id,))
        _cleanup(pg_conn, press_id=press_id)
