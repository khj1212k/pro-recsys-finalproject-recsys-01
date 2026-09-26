"""`python -m jobs.run ingest` 전체 경로(RSS -> 본문 추출 -> 임베딩 -> job_runs 기록)를 실제
Postgres에서 검증한다. 네트워크(RSS/기사 페이지)와 BGE-M3는 가짜로 바꾸고, DB 쓰기와
재실행 안전성(idempotency)은 진짜로 확인한다.
"""
import sys
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

DIM = 1024
LONG_BODY = "정부가 오늘 새 경제 정책을 발표했다. 전문가들은 물가와 고용에 미칠 영향을 분석하고 있다. " * 12


class _FakeEntry(dict):
    def __init__(self, link, title, published):
        super().__init__(published=published)
        self.link = link
        self.title = title


class _FakeEmbedder:
    device = "cpu"

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def generate_embeddings_batch(self, texts, batch_size=8):
        vecs = []
        for t in texts:
            v = np.zeros(DIM, dtype=np.float32)
            v[len(t) % DIM] = 1.0
            vecs.append(v.tolist())
        return vecs, 0.01


@pytest.fixture
def fake_world(pg_conn, isolated_news_raw, monkeypatch):
    from config.settings import Settings

    suffix = uuid.uuid4().hex[:8]
    press_name = f"itest-press-{suffix}"
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (press_name,))
        press_id = cur.fetchone()[0]

    urls = {
        "ok": f"http://itest.example.com/{suffix}/list/ok",
        # 동아일보처럼 같은 기사를 섹션 경로만 바꾼 URL로 다시 내보내는 경우
        "ok_variant": f"http://itest.example.com/{suffix}/Economy/ok",
        "short": f"http://itest.example.com/{suffix}/short",
        "unreachable": f"http://itest.example.com/{suffix}/unreachable",
        "crash": f"http://itest.example.com/{suffix}/crash",
    }
    published = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    feed = MagicMock()
    feed.bozo = 0
    feed.entries = [
        _FakeEntry(urls["ok"], "경제 정책 발표", published),
        _FakeEntry(urls["ok_variant"], "경제 정책 발표", published),
        _FakeEntry(urls["short"], "짧은 기사", published),
        _FakeEntry(urls["unreachable"], "막힌 기사", published),
        _FakeEntry(urls["crash"], "파서가 죽는 기사", published),
    ]
    pages = {
        urls["ok"]: "<html>ok</html>", urls["ok_variant"]: "<html>ok</html>",
        urls["short"]: "<html>short</html>", urls["crash"]: "<html>crash</html>",
    }
    bodies = {"<html>ok</html>": LONG_BODY, "<html>short</html>": "한 줄짜리 본문"}

    def fake_extract(html, **kwargs):
        if html == "<html>crash</html>":
            raise ValueError("parser crashed")
        return bodies[html]

    monkeypatch.setattr(Settings, "RSS_FEEDS", {press_name: ("direct", "http://itest/rss")})
    monkeypatch.setattr(Settings, "MAX_EXTRACT_ATTEMPTS", 3)
    fake_embedder_module = types.ModuleType("core.embedder")
    fake_embedder_module.NewsEmbedder = _FakeEmbedder
    monkeypatch.setitem(sys.modules, "core.embedder", fake_embedder_module)

    with patch("crawler.rss_collector.parse_feed_with_retry", return_value=feed), \
         patch("crawler.content_extractor.extractor.fetch_url_with_retry", side_effect=lambda u: pages.get(u)), \
         patch("crawler.content_extractor.extractor.trafilatura.extract", side_effect=fake_extract):
        yield {"press_name": press_name, "press_id": press_id, "urls": urls}

    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM news_raw WHERE press_id = %s", (press_id,))
        cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))


def _latest_ingest_run(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id, status, stats FROM job_runs WHERE job = 'ingest' ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def test_ingest_job_collects_extracts_embeds_and_is_idempotent(database_url, pg_conn, fake_world):
    from jobs.run import main

    run_ids = []
    try:
        assert main(["ingest", "--workers", "1"]) == 0
        run_id, status, stats = _latest_ingest_run(pg_conn)
        run_ids.append(run_id)

        assert status == "succeeded"
        feed_stats = stats["rss"]["per_feed"][fake_world["press_name"]]
        assert (feed_stats["entries"], feed_stats["inserted"], feed_stats["skipped"]) == (5, 5, 0)
        extract = stats["extract"]
        assert (extract["ok"], extract["duplicate"], extract["dropped"], extract["fetch_failed"], extract["error"]) \
            == (1, 1, 1, 1, 1)
        assert stats["embed"]["embedded"] == 1

        urls = fake_world["urls"]
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT raw_news_url, raw_news_extract_status, raw_news_extract_attempts,
                       raw_news_extracted_at IS NOT NULL,
                       CASE WHEN embedding_result IS NULL THEN NULL ELSE vector_dims(embedding_result) END
                FROM news_raw WHERE press_id = %s
                """,
                (fake_world["press_id"],),
            )
            rows = {r[0]: r[1:] for r in cur.fetchall()}
        assert rows[urls["ok"]] == ("ok", 1, True, DIM)
        assert rows[urls["ok_variant"]] == ("duplicate", 1, True, None)
        assert rows[urls["short"]] == ("dropped", 1, True, None)
        assert rows[urls["unreachable"]] == ("fetch_failed", 1, True, None)
        assert rows[urls["crash"]] == ("error", 1, True, None)

        # 두 번째 실행: 새 기사 없음, 버린/중복 기사는 다시 받지 않고 다운로드 실패·오류 건만 재시도
        assert main(["ingest", "--workers", "1"]) == 0
        run_id, status, stats = _latest_ingest_run(pg_conn)
        run_ids.append(run_id)
        feed_stats = stats["rss"]["per_feed"][fake_world["press_name"]]
        assert (feed_stats["inserted"], feed_stats["skipped"]) == (0, 5)
        assert stats["extract"]["targets"] == 2
        assert (stats["extract"]["fetch_failed"], stats["extract"]["error"]) == (1, 1)
        assert stats["embed"]["targets"] == 0

        def attempts(url):
            with pg_conn.cursor() as cur:
                cur.execute("SELECT raw_news_extract_attempts FROM news_raw WHERE raw_news_url = %s", (url,))
                return cur.fetchone()[0]

        assert attempts(urls["unreachable"]) == 2 and attempts(urls["crash"]) == 2

        # 시도 횟수 상한(MAX_EXTRACT_ATTEMPTS=3)에 닿으면 더 받지 않는다
        assert main(["ingest", "--workers", "1"]) == 0
        run_id, _, stats = _latest_ingest_run(pg_conn)
        run_ids.append(run_id)
        assert main(["ingest", "--workers", "1"]) == 0
        run_id, _, stats = _latest_ingest_run(pg_conn)
        run_ids.append(run_id)
        assert stats["extract"]["targets"] == 0
        assert attempts(urls["unreachable"]) == 3 and attempts(urls["crash"]) == 3
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM job_runs WHERE id = ANY(%s)", (run_ids,))


def _latest_run(pg_conn, job):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id, status, stats FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1", (job,))
        return cur.fetchone()


def _embedding_dims(pg_conn, url):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT CASE WHEN embedding_result IS NULL THEN NULL ELSE vector_dims(embedding_result) END "
            "FROM news_raw WHERE raw_news_url = %s",
            (url,),
        )
        return cur.fetchone()[0]


def test_scheduler_split_collects_without_embedding_then_embed_job_fills_vectors(database_url, pg_conn, fake_world):
    """스케줄러 구성(docker/crontab): 수집 잡은 rss,extract만 돌고 임베딩은 embed 잡이 따로 한다.
    두 잡은 각자 job_runs 행과 advisory lock을 쓴다."""
    from jobs.run import main

    run_ids = []
    ok_url = fake_world["urls"]["ok"]
    try:
        assert main(["ingest", "--stages", "rss,extract", "--workers", "1"]) == 0
        run_id, status, stats = _latest_run(pg_conn, "ingest")
        run_ids.append(run_id)
        assert status == "succeeded"
        assert "embed" not in stats
        assert _embedding_dims(pg_conn, ok_url) is None

        assert main(["embed", "--time-budget-s", "60"]) == 0
        run_id, status, stats = _latest_run(pg_conn, "embed")
        run_ids.append(run_id)
        assert status == "succeeded"
        assert stats["embed"]["embedded"] >= 1
        assert stats["embed"]["remaining"] == 0 and stats["embed"]["stopped_by_budget"] is False
        assert _embedding_dims(pg_conn, ok_url) == DIM
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM job_runs WHERE id = ANY(%s)", (run_ids,))
