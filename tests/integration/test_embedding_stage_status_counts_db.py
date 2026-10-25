"""embed 단계(pipeline/stages.py embed_pending_articles)가 본문이 없어 임베딩하지 않은 기사를 상태별로 센다 - 실제 DB에서 확인한다.
새 추출 상태(ADR 0026 2026-10-25): 'blocked_origin'은 영구 제외(no_content), 보존 기한으로 본문을 비운 'rss_summary'는
'ok'처럼 body_purged다. 본문이 있는 대상이 없어 모델은 올리지 않는다. 지어낸 제목만 쓴다."""
import uuid

# 이름표 -> (추출 상태, 본문, 시도 횟수, 본문 비움) / 기대하는 칸
ROWS = {
    "pending":        (None,            "",   0, False),   # awaiting
    "retrying":       ("fetch_failed",  "",   1, False),   # awaiting
    "gave_up":        ("fetch_failed",  "",   3, False),   # no_content
    "dropped":        ("dropped",       "",   1, False),   # no_content
    "empty":          ("empty",         "",   1, False),   # no_content
    "duplicate":      ("duplicate",     "",   1, False),   # no_content
    "blocked_origin": ("blocked_origin", "",  0, False),   # no_content
    "ok_purged":      ("ok",            None, 1, True),    # body_purged
    "summary_purged": ("rss_summary",   None, 0, True),    # body_purged
}
EXPECTED = {"awaiting_extraction": 2, "no_content": 5, "body_purged": 2, "targets": 0}


def test_status_counts_cover_blocked_origin_and_purged_rss_summary(database_url, pg_conn, isolated_news_raw, monkeypatch):
    from config.settings import Settings
    from pipeline.stages import embed_pending_articles

    monkeypatch.setattr(Settings, "MAX_EXTRACT_ATTEMPTS", 3)
    suffix = uuid.uuid4().hex[:8]
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"embed-count-{suffix}",))
        press_id = cur.fetchone()[0]
    try:
        with pg_conn.cursor() as cur:
            for key, (status, content, attempts, purged) in ROWS.items():
                cur.execute(
                    "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, "
                    "raw_news_crawled_at, raw_news_extract_status, raw_news_extract_attempts, raw_news_content_length, "
                    "raw_news_content_purged_at) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, CASE WHEN %s THEN NOW() END)",
                    (press_id, key, content, f"http://embed-count/{suffix}/{key}", status, attempts,
                     400 if purged else None, purged),
                )
        stats = embed_pending_articles(Settings, limit=100)
        assert {k: stats[k] for k in EXPECTED} == EXPECTED
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE press_id = %s", (press_id,))
            cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))
