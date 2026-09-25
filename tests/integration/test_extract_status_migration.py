"""f87f7378672e(job_runs + news_raw 추출 상태)의 기존 행 백필 규칙을 실제 DB에서 검증한다:
본문이 있는 기존 행은 'ok'로 확정하고, 빈 본문 행은 "미처리"와 구분할 수 없으므로 NULL로
남겨 새 추출기가 한 번 더 시도하게 한다.
"""
import os
import uuid

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREVIOUS = "e725a62ffef1"


def _alembic_config(database_url):
    from alembic.config import Config

    cfg = Config(os.path.join(REPO_ROOT, "backend", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "backend", "alembic"))
    return cfg


def test_upgrade_backfills_ok_for_rows_with_content_and_leaves_empty_rows_pending(database_url, pg_conn, monkeypatch):
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", database_url)
    cfg = _alembic_config(database_url)
    suffix = uuid.uuid4().hex[:8]
    press_id = None
    try:
        command.downgrade(cfg, PREVIOUS)
        with pg_conn.cursor() as cur:
            cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"mig-{suffix}",))
            press_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at) "
                "VALUES (%s, 't', %s, %s, NOW()), (%s, 't', '', %s, NOW())",
                (press_id, "본문이 있는 기사", f"http://mig/{suffix}/full", press_id, f"http://mig/{suffix}/empty"),
            )
    finally:
        command.upgrade(cfg, "head")

    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT raw_news_url, raw_news_extract_status, raw_news_extract_attempts, "
                "raw_news_extracted_at IS NOT NULL FROM news_raw WHERE press_id = %s",
                (press_id,),
            )
            rows = {r[0]: r[1:] for r in cur.fetchall()}
        assert rows[f"http://mig/{suffix}/full"] == ("ok", 1, True)
        assert rows[f"http://mig/{suffix}/empty"] == (None, 0, False)
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE press_id = %s", (press_id,))
            cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))


def test_extract_status_check_constraint_rejects_unknown_value(pg_conn):
    import psycopg2

    suffix = uuid.uuid4().hex[:8]
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"chk-{suffix}",))
        press_id = cur.fetchone()[0]
    try:
        with pytest.raises(psycopg2.errors.CheckViolation):
            with pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, "
                    "raw_news_crawled_at, raw_news_extract_status) VALUES (%s, 't', '', %s, NOW(), 'weird')",
                    (press_id, f"http://chk/{suffix}"),
                )
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))
