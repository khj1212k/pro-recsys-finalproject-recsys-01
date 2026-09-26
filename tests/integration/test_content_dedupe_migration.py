"""d48994e9d26e(본문 sha256 중복 제거 + 'error'/'duplicate' 상태)를 실제 DB에서 검증한다.

- upgrade: 기존 'ok' 행에 해시를 채우고, 같은 본문의 뒤 사본은 'duplicate'(본문·임베딩 비움)로 바꾼다
- downgrade: 사본에 원본 본문을 되돌린다(임베딩은 다음 embed가 다시 채움)
- 모델 선언(app.models.news.NewsRaw)이 마이그레이션 결과와 어긋나지 않는다
"""
import os
import re
import uuid

import numpy as np
import psycopg2
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BEFORE = "f87f7378672e"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(os.path.join(REPO_ROOT, "backend", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "backend", "alembic"))
    return cfg


def _vec(axis):
    v = np.zeros(1024, dtype=np.float32)
    v[axis] = 1.0
    return "[" + ",".join(str(x) for x in v) + "]"


def _rows(pg_conn, press_id):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_news_url, raw_news_extract_status, raw_news_content, embedding_result IS NOT NULL,
                   raw_news_content_sha256
            FROM news_raw WHERE press_id = %s
            """,
            (press_id,),
        )
        return {r[0]: r[1:] for r in cur.fetchall()}


def test_upgrade_marks_later_copies_duplicate_and_downgrade_restores_them(database_url, pg_conn, monkeypatch):
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", database_url)
    cfg = _alembic_config()
    suffix = uuid.uuid4().hex[:8]
    body = "같은 기사가 섹션 경로만 바뀐 URL로 다시 들어왔다. " * 20
    urls = {k: f"http://dedupe/{suffix}/{k}" for k in ("first", "copy", "other")}
    press_id = None
    try:
        command.downgrade(cfg, BEFORE)
        with pg_conn.cursor() as cur:
            cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"dedupe-{suffix}",))
            press_id = cur.fetchone()[0]
            for key, content, axis in (("first", body, 0), ("copy", body, 0), ("other", "다른 기사 본문 " * 30, 1)):
                cur.execute(
                    "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at, "
                    "raw_news_extract_status, raw_news_extract_attempts, embedding_result) "
                    "VALUES (%s, 't', %s, %s, NOW(), 'ok', 1, %s::vector)",
                    (press_id, content, urls[key], _vec(axis)),
                )
    finally:
        command.upgrade(cfg, "head")

    try:
        rows = _rows(pg_conn, press_id)
        first_status, first_content, first_embedded, first_hash = rows[urls["first"]]
        assert (first_status, first_content, first_embedded) == ("ok", body, True)
        assert rows[urls["copy"]] == ("duplicate", "", False, first_hash)
        assert rows[urls["other"]][0] == "ok" and rows[urls["other"]][3] not in (None, first_hash)

        # 부분 unique 인덱스: 같은 본문 해시로 'ok' 행을 하나 더 만들 수 없다
        with pytest.raises(psycopg2.errors.UniqueViolation), pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE news_raw SET raw_news_extract_status = 'ok' WHERE raw_news_url = %s", (urls["copy"],)
            )

        command.downgrade(cfg, BEFORE)
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT raw_news_extract_status, raw_news_content FROM news_raw WHERE raw_news_url = %s",
                (urls["copy"],),
            )
            assert cur.fetchone() == ("ok", body)
    finally:
        command.upgrade(cfg, "head")
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE press_id = %s", (press_id,))
            cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))


def test_news_raw_model_matches_migrated_schema(database_url, pg_conn):
    """alembic autogenerate가 이 브랜치가 추가한 news_raw 컬럼·인덱스, job_runs에서 차이를 보고하지 않고,
    CHECK 제약(autogenerate가 비교하지 않음)의 상태 목록이 모델 선언과 같아야 한다."""
    import sys

    sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    import app.models  # noqa: F401 - 모든 테이블을 metadata에 등록
    from app.models.news import NEWS_RAW_EXTRACT_STATUSES

    ours = ("raw_news_extract_status", "raw_news_extracted_at", "raw_news_extract_attempts",
            "raw_news_content_sha256", "uq_news_raw_content_sha256_ok", "job_runs")
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            diffs = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    finally:
        engine.dispose()
    related = [d for d in diffs if any(name in repr(d) for name in ours)]
    assert related == []

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_news_raw_extract_status'"
        )
        (definition,) = cur.fetchone()
    assert set(re.findall(r"'([a-z_]+)'", definition)) == set(NEWS_RAW_EXTRACT_STATUSES)
