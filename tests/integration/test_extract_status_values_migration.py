"""b7e4a1c93d52(추출 상태 'rss_summary'/'blocked_origin', ADR 0026 2026-10-25 C-31)를 실제 DB에서 검증한다.

- 이전 리비전의 제약은 두 상태를 거절하고(동아일보 요약 수집이 CheckViolation으로 0건이 되던 결함), head는 받는다
- upgrade 뒤 제약은 검증된 상태이고 허용 목록이 모델 선언(NEWS_RAW_EXTRACT_STATUSES)과 같다
- downgrade: 'blocked_origin' -> 'fetch_failed'(시도 횟수를 상한 이상으로), 'rss_summary' -> 'dropped'(본문·임베딩을 비움,
  보존 잡이 이미 비운 행은 본문을 건드리지 않는다). 다른 상태의 행은 그대로이고, 이전 코드의 추출기가 다시 집지 않는다
- 다시 upgrade해도 되돌린 행이 남는다

모든 본문은 지어낸 문장이다.
"""
import hashlib
import os
import re
import uuid

import numpy as np
import psycopg2
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BEFORE = "f1db3221e45f"
CAP = 3


def _alembic_config():
    from alembic.config import Config

    cfg = Config(os.path.join(REPO_ROOT, "backend", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "backend", "alembic"))
    return cfg


def _vec(axis):
    v = np.zeros(1024, dtype=np.float32)
    v[axis] = 1.0
    return "[" + ",".join(str(x) for x in v) + "]"


def _constraint(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
            "WHERE conrelid = 'news_raw'::regclass AND conname = 'ck_news_raw_extract_status'"
        )
        definition, validated = cur.fetchone()
    return set(re.findall(r"'([a-z_]+)'", definition)), validated


def _insert(pg_conn, press_id, url, status, content, attempts=0, length=None, purged=False, embedding_axis=None,
            sha=None):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_crawled_at, "
            "raw_news_extract_status, raw_news_extract_attempts, raw_news_content_length, raw_news_content_sha256, "
            "raw_news_content_purged_at, embedding_result) "
            "VALUES (%s, 't', %s, %s, NOW(), %s, %s, %s, %s, CASE WHEN %s THEN NOW() END, %s::vector) "
            "RETURNING raw_news_id",
            (press_id, content, url, status, attempts, length, sha, purged,
             None if embedding_axis is None else _vec(embedding_axis)),
        )
        return cur.fetchone()[0]


def _row(pg_conn, raw_news_id):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT raw_news_extract_status, raw_news_content, raw_news_extract_attempts, raw_news_content_length, "
            "raw_news_content_sha256, raw_news_content_purged_at IS NOT NULL, embedding_result IS NOT NULL "
            "FROM news_raw WHERE raw_news_id = %s",
            (raw_news_id,),
        )
        return cur.fetchone()


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _new_press(pg_conn, suffix):
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"extract-status-{suffix}",))
        return cur.fetchone()[0]


def _cleanup(pg_conn, press_id):
    if press_id is None:
        return
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM news_raw WHERE press_id = %s", (press_id,))
        cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))


def test_previous_revision_rejects_the_new_statuses_and_head_accepts_them(database_url, pg_conn, monkeypatch):
    from alembic import command
    from app.models.news import NEWS_RAW_EXTRACT_STATUSES

    monkeypatch.setenv("DATABASE_URL", database_url)
    cfg = _alembic_config()
    suffix = uuid.uuid4().hex[:8]
    press_id = None
    try:
        command.downgrade(cfg, BEFORE)
        press_id = _new_press(pg_conn, suffix)
        allowed, validated = _constraint(pg_conn)
        assert allowed == {"ok", "dropped", "empty", "fetch_failed", "error", "duplicate"} and validated
        for status in ("rss_summary", "blocked_origin"):
            with pytest.raises(psycopg2.errors.CheckViolation):
                _insert(pg_conn, press_id, f"http://status/{suffix}/old-{status}", status, "지어낸 요약")
    finally:
        command.upgrade(cfg, "head")

    try:
        allowed, validated = _constraint(pg_conn)
        assert allowed == set(NEWS_RAW_EXTRACT_STATUSES) and validated
        for status in ("rss_summary", "blocked_origin"):
            _insert(pg_conn, press_id, f"http://status/{suffix}/new-{status}", status, "지어낸 요약")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert(pg_conn, press_id, f"http://status/{suffix}/weird", "weird", "")
    finally:
        _cleanup(pg_conn, press_id)


def test_downgrade_maps_the_new_statuses_and_leaves_other_rows_alone(database_url, pg_conn, monkeypatch):
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("MAX_EXTRACT_ATTEMPTS", str(CAP))
    cfg = _alembic_config()
    suffix = uuid.uuid4().hex[:8]
    summary = f"지어낸 요약 {suffix}. " * 30
    body = f"지어낸 본문 {suffix}. " * 60
    press_id = _new_press(pg_conn, suffix)
    url = lambda key: f"http://status/{suffix}/{key}"  # noqa: E731
    try:
        ids = {
            "summary": _insert(pg_conn, press_id, url("summary"), "rss_summary", summary, length=len(summary),
                               sha=_sha(summary), embedding_axis=0),
            "summary_purged": _insert(pg_conn, press_id, url("summary_purged"), "rss_summary", None, length=480,
                                      purged=True, sha=_sha("purged " + suffix)),
            "blocked_new": _insert(pg_conn, press_id, url("blocked_new"), "blocked_origin", "", attempts=0),
            "blocked_retry": _insert(pg_conn, press_id, url("blocked_retry"), "blocked_origin", "", attempts=1),
            "blocked_over": _insert(pg_conn, press_id, url("blocked_over"), "blocked_origin", "", attempts=CAP + 2),
            "ok": _insert(pg_conn, press_id, url("ok"), "ok", body, attempts=1, length=len(body), sha=_sha(body),
                          embedding_axis=1),
            "duplicate": _insert(pg_conn, press_id, url("duplicate"), "duplicate", "", attempts=1, length=0,
                                 sha=_sha(body)),
        }
        before_other = {k: _row(pg_conn, ids[k]) for k in ("ok", "duplicate")}

        try:
            command.downgrade(cfg, BEFORE)
            allowed, validated = _constraint(pg_conn)
            assert allowed == {"ok", "dropped", "empty", "fetch_failed", "error", "duplicate"} and validated

            # 'rss_summary' -> 'dropped': 본문 ''(길이 0), 임베딩 NULL, 해시는 남는다
            assert _row(pg_conn, ids["summary"]) == ("dropped", "", 0, 0, _sha(summary), False, False)
            # 보존 잡이 이미 비운 행: 본문 NULL·길이·비운 시각을 그대로 둔다(ck_news_raw_purged_body_gone)
            assert _row(pg_conn, ids["summary_purged"]) == ("dropped", None, 0, 480, _sha("purged " + suffix), True, False)
            # 'blocked_origin' -> 'fetch_failed', 시도 횟수는 상한 이상으로(이미 넘은 값은 그대로)
            assert _row(pg_conn, ids["blocked_new"])[:3] == ("fetch_failed", "", CAP)
            assert _row(pg_conn, ids["blocked_retry"])[:3] == ("fetch_failed", "", CAP)
            assert _row(pg_conn, ids["blocked_over"])[:3] == ("fetch_failed", "", CAP + 2)
            for key, row in before_other.items():
                assert _row(pg_conn, ids[key]) == row, key

            # 이전 코드의 추출기 대상 조회(status NULL 또는 재시도 가능 상태 + 시도 횟수 < 상한)에 걸리는 행이 없다
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM news_raw WHERE raw_news_id = ANY(%s) AND (raw_news_extract_status IS NULL "
                    "OR (raw_news_extract_status IN ('fetch_failed', 'error') AND raw_news_extract_attempts < %s))",
                    (list(ids.values()), CAP),
                )
                assert cur.fetchone() == (0,)
            with pytest.raises(psycopg2.errors.CheckViolation):
                _insert(pg_conn, press_id, url("late"), "rss_summary", "지어낸 요약")
        finally:
            command.upgrade(cfg, "head")

        allowed, validated = _constraint(pg_conn)
        assert "rss_summary" in allowed and "blocked_origin" in allowed and validated
        assert _row(pg_conn, ids["summary"])[:2] == ("dropped", "")  # 되돌린 행은 다시 올려도 돌아오지 않는다
        assert _row(pg_conn, ids["blocked_new"])[:3] == ("fetch_failed", "", CAP)
    finally:
        _cleanup(pg_conn, press_id)


def test_downgrade_refuses_a_bad_attempt_cap(database_url, pg_conn, monkeypatch):
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("MAX_EXTRACT_ATTEMPTS", "0")
    with pytest.raises(RuntimeError, match="MAX_EXTRACT_ATTEMPTS"):
        command.downgrade(_alembic_config(), BEFORE)
    with pg_conn.cursor() as cur:  # 아무것도 바꾸지 않았다
        cur.execute("SELECT version_num FROM alembic_version")
        assert cur.fetchone()[0] != BEFORE
