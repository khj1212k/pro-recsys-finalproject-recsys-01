"""장기 프로필의 증분 상태(ADR 0033)를 실제 PostgreSQL에서 검증한다.

- 리비전 a6f1e83b0d57: 테이블·컬럼·인덱스, 행이 있는 채로의 왕복, 모델 선언과의 일치
- 클릭 API가 쓰는 상태 = 클릭 로그에서 정의식으로 다시 계산한 상태(어느 시점에 읽어도)
- 재구축 잡: 채우기, 점검, 다시 돌려도 같은 결과
- 저장소가 읽는 최근 클릭·인기도 창 집계의 구간 경계
"""
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from tests.integration.test_realtime_recsys_seeded_db import api_client, engine, seeded  # noqa: F401

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REVISION = "a6f1e83b0d57"
BEFORE = "c4d2a91e7f30"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(os.path.join(REPO_ROOT, "backend", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "backend", "alembic"))
    return cfg


def _stored(pg_conn, uid):
    from app.recsys.profile_store import STATE_COLUMNS, state_from_row

    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT {STATE_COLUMNS} FROM user_profile_state WHERE user_id = %s", (uid,))
        row = cur.fetchone()
    return None if row is None else state_from_row(*row)


def _recomputed(engine, uid):  # noqa: F811
    from app.recsys.profile_store import recompute

    with engine.connect() as conn:
        return recompute(conn, uid)


# ------------------------------------------------------------------ 스키마
def test_revision_is_the_single_head_and_round_trips_with_rows_in_place(database_url, pg_conn, monkeypatch, seeded):  # noqa: F811
    from alembic import command
    from alembic.script import ScriptDirectory

    monkeypatch.setenv("DATABASE_URL", database_url)
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1  # 가지가 갈라지지 않았다
    assert script.get_revision(REVISION).down_revision == BEFORE

    uid = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])
    request_id = str(uuid.uuid4())
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recommendation_request_log (request_id, user_id, source, model_version, policy_version, "
            "slate_size, shown_count, features_as_of) VALUES (%s, %s, 'realtime', 'heuristic-v1', 'deterministic', 0, 0, NOW())",
            (request_id, uid),
        )
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE indexname IN "
            "('ix_user_newsletter_ctr_log_news_letter_id_created_at', "
            " 'ix_recommendation_impression_log_news_letter_id_created_at')"
        )
        assert len(cur.fetchall()) == 2
    try:
        command.downgrade(cfg, BEFORE)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT to_regclass('user_profile_state')")
            assert cur.fetchone() == (None,)
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND (table_name, column_name) IN (('recommendation_request_log', 'features_as_of'), "
                "('model_registry', 'feature_schema_hash'))"
            )
            assert cur.fetchall() == []
            cur.execute("SELECT count(*) FROM recommendation_request_log WHERE request_id = %s", (request_id,))
            assert cur.fetchone() == (1,)  # 요청 행은 남고 컬럼만 사라진다
    finally:
        command.upgrade(cfg, "head")
    with pg_conn.cursor() as cur:
        cur.execute("SELECT features_as_of FROM recommendation_request_log WHERE request_id = %s", (request_id,))
        assert cur.fetchone() == (None,)
        cur.execute("SELECT count(*) FROM user_profile_state WHERE user_id = %s", (uid,))
        assert cur.fetchone() == (0,)  # 상태는 캐시다: 되돌리면 사라지고 잡이 다시 만든다


def test_models_match_the_migrated_schema_for_the_serving_parity_columns(database_url):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    import app.models  # noqa: F401

    ours = ("user_profile_state", "features_as_of", "feature_schema_hash")
    eng = create_engine(database_url)
    try:
        with eng.connect() as conn:
            diffs = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    finally:
        eng.dispose()
    flat = [op for d in diffs for op in (d if isinstance(d, list) else [d])]
    related = [op for op in flat if not str(op[0]).endswith("_index") and any(name in repr(op) for name in ours)]
    assert related == []
    tables = SQLModel.metadata.tables
    assert {"hist_sum", "hist_anchor_ts", "hist_len", "hist_cat_counts"} <= set(tables["user_profile_state"].c.keys())
    assert "features_as_of" in tables["recommendation_request_log"].c
    assert "feature_schema_hash" in tables["model_registry"].c


def test_deleting_a_user_removes_the_state_row(seeded, pg_conn):  # noqa: F811
    uid = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])
    with pg_conn.cursor() as cur:
        cur.execute('DELETE FROM "user" WHERE user_id = %s', (uid,))
        cur.execute("SELECT count(*) FROM user_profile_state WHERE user_id = %s", (uid,))
        assert cur.fetchone() == (0,)


# ------------------------------------------------------------------ 클릭 API의 갱신
def test_clicks_through_the_api_keep_the_stored_state_equal_to_the_log_at_every_step(
    api_client, engine, seeded, pg_conn  # noqa: F811
):
    from recsys_core.profile import same_state

    uid = seeded.add_user()
    client = api_client(uid)
    assert _stored(pg_conn, uid) is None

    sequence = [seeded.by_topic[t][j] for t, j in ((0, 0), (1, 0), (0, 1), (2, 3), (0, 0), (3, 5))]  # 반복 클릭 포함
    for n, nid in enumerate(sequence, start=1):
        assert client.post("/logs/newsletter/click", json={"news_letter_id": nid}).status_code == 200
        stored = _stored(pg_conn, uid)
        fresh, last = _recomputed(engine, uid)
        assert stored.hist.hist_len == fresh.hist_len == n
        assert same_state(stored.hist, fresh, cos_tol=1e-12)
        assert stored.last_event_at == last

    counts = _stored(pg_conn, uid).hist.cat_counts
    assert sum(counts.values()) == len(sequence)
    assert counts == {seeded.cat_ids[0]: 3, seeded.cat_ids[1]: 1, seeded.cat_ids[2]: 1, seeded.cat_ids[3]: 1}


def test_detail_views_and_clicks_on_newsletters_without_an_embedding_do_not_change_the_state(
    api_client, seeded, pg_conn  # noqa: F811
):
    uid = seeded.add_user()
    client = api_client(uid)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO news_letter (news_letter_title, news_letter_sentence, news_letter_content, "
            "news_letter_keywords, raw_news_count, news_letter_created_at) "
            "VALUES ('no-embedding', '요약', '내용', '[]', 1, NOW()) RETURNING news_letter_id"
        )
        bare = cur.fetchone()[0]
    try:
        body = {"news_letter_id": seeded.by_topic[0][0], "event": "detail_view", "dwell_ms": 3000}
        assert client.post("/logs/newsletter/click", json=body).status_code == 200
        assert client.post("/logs/newsletter/click", json={"news_letter_id": bare}).status_code == 200
        assert _stored(pg_conn, uid) is None

        assert client.post("/logs/newsletter/click", json={"news_letter_id": seeded.by_topic[0][0]}).status_code == 200
        assert _stored(pg_conn, uid).hist.hist_len == 1
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM user_newsletter_ctr_log WHERE news_letter_id = %s", (bare,))
            cur.execute("DELETE FROM news_letter WHERE news_letter_id = %s", (bare,))


def test_concurrent_clicks_of_one_user_are_serialized_by_the_row_lock(engine, seeded, pg_conn):  # noqa: F811
    """같은 사용자의 클릭 8건이 동시에 들어와도(첫 클릭이라 상태 행도 없다) 갱신이 하나도 사라지지 않는다."""
    from sqlalchemy import text

    from app.recsys import profile_store
    from recsys_core.profile import same_state

    uid = seeded.add_user()
    targets = [seeded.by_topic[t][j] for t in range(4) for j in range(2)]
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    barrier = threading.Barrier(len(targets))
    errors = []

    def click(k, nid):
        at = base + timedelta(seconds=k, microseconds=k)
        try:
            barrier.wait(timeout=30)
            with engine.begin() as conn:
                conn.execute(
                    text("INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at) VALUES (:u, :n, :t)"),
                    {"u": uid, "n": nid, "t": at},
                )
                assert profile_store.apply_click(conn, uid, nid, at) is True
        except Exception as exc:  # 스레드의 실패를 본 스레드로 옮긴다
            errors.append(exc)

    threads = [threading.Thread(target=click, args=(k, nid)) for k, nid in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    stored = _stored(pg_conn, uid)
    fresh, last = _recomputed(engine, uid)
    assert stored.hist.hist_len == len(targets)  # 잃은 갱신이 없다
    assert same_state(stored.hist, fresh, cos_tol=1e-12) and stored.last_event_at == last


# ------------------------------------------------------------------ 재구축 잡
def test_rebuild_job_backfills_clicks_that_bypassed_the_api_and_is_idempotent(engine, seeded, pg_conn):  # noqa: F811
    from jobs.tasks.rebuild_user_state import rebuild_all
    from recsys_core.profile import same_state

    uid = seeded.add_user()
    other = seeded.add_user()
    now = datetime.now(timezone.utc)
    for k, nid in enumerate(seeded.by_topic[1][:5]):
        seeded.click(uid, nid, at=now - timedelta(days=9 - 2 * k, seconds=k))  # 시드 경로: 상태를 갱신하지 않는다
    seeded.click(other, seeded.by_topic[2][0], at=now - timedelta(hours=1))
    assert _stored(pg_conn, uid) is None

    checked = rebuild_all(engine, user_ids=[uid, other], check=True)
    assert (checked["mismatch"], checked["rebuilt"], checked["unchanged"]) == (2, 0, 0)
    assert _stored(pg_conn, uid) is None  # 점검은 쓰지 않는다

    first = rebuild_all(engine, user_ids=[uid, other])
    second = rebuild_all(engine, user_ids=[uid, other])

    assert (first["rebuilt"], first["unchanged"]) == (2, 0)
    assert (second["rebuilt"], second["unchanged"]) == (0, 2)
    stored = _stored(pg_conn, uid)
    fresh, last = _recomputed(engine, uid)
    assert stored.hist.hist_len == 5 and same_state(stored.hist, fresh, cos_tol=1e-12) and stored.last_event_at == last
    assert rebuild_all(engine, user_ids=[uid, other], check=True)["mismatch"] == 0


def test_rebuild_job_finds_every_user_with_clicks_or_state_and_repairs_a_corrupted_row(engine, seeded, pg_conn):  # noqa: F811
    from app.recsys import profile_store
    from jobs.tasks.rebuild_user_state import rebuild_all

    clicker = seeded.add_user()
    stale = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])  # 로그에 없는 상태
    seeded.click(clicker, seeded.by_topic[0][0])
    with engine.connect() as conn:
        users = profile_store.users_to_rebuild(conn)
    assert {clicker, stale} <= set(users)

    result = rebuild_all(engine, user_ids=[clicker, stale])

    assert result["rebuilt"] == 2
    assert _stored(pg_conn, clicker).hist.hist_len == 1
    assert _stored(pg_conn, stale).hist.empty  # 클릭이 없는 사용자의 상태는 비워진다


def test_the_job_is_registered_and_runs_through_the_cli(database_url, seeded, pg_conn, monkeypatch):  # noqa: F811
    from jobs.run import main

    monkeypatch.setenv("DATABASE_URL", database_url)
    uid = seeded.add_user()
    seeded.click(uid, seeded.by_topic[0][0])

    assert main(["rebuild_user_state", "--user-id", str(uid)]) == 0

    assert _stored(pg_conn, uid).hist.hist_len == 1
    with pg_conn.cursor() as cur:
        cur.execute("SELECT status, stats FROM job_runs WHERE job = 'rebuild_user_state' ORDER BY id DESC LIMIT 1")
        status, stats = cur.fetchone()
    assert status == "succeeded" and stats["user_state"]["rebuilt"] == 1


# ------------------------------------------------------------------ 저장소가 읽는 입력의 경계
def test_recent_clicks_are_the_latest_n_in_a_half_open_interval(engine, seeded):  # noqa: F811
    from sqlalchemy.orm import Session

    from app.recsys.sql_repository import SqlRecsysRepository

    uid = seeded.add_user()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    since = now - timedelta(hours=24)
    ats = [since - timedelta(microseconds=1), since, now - timedelta(hours=2), now - timedelta(microseconds=1), now]
    for nid, at in zip(seeded.by_topic[0], ats):
        seeded.click(uid, nid, at=at)

    with Session(engine) as s:
        repo = SqlRecsysRepository(s)
        inside = repo.recent_clicks(uid, since, now, 20)
        latest_two = repo.recent_clicks(uid, since, now, 2)

    assert [c.at for c in inside] == [ats[3], ats[2], ats[1]]  # 시작은 포함, 끝은 제외, 최신순
    assert [c.at for c in latest_two] == [ats[3], ats[2]]
    np.testing.assert_allclose(inside[0].embedding, seeded.vec_of[seeded.by_topic[0][3]], atol=1e-6)


def test_item_window_counts_count_clicks_and_impressions_of_all_users_per_window(engine, seeded, pg_conn):  # noqa: F811
    from sqlalchemy.orm import Session

    from app.recsys.pipeline import load_popularity
    from app.recsys.sql_repository import SqlRecsysRepository
    from app.recsys.types import WindowCounts

    a, b = seeded.add_user(), seeded.add_user()
    x, y, z = seeded.by_topic[3][:3]
    now = datetime.now(timezone.utc).replace(microsecond=300_000)
    end = now.replace(microsecond=0) - timedelta(seconds=1)  # 요청 초(올림) - 2초
    seeded.click(a, x, at=end - timedelta(microseconds=1))   # 6h 창 안(끝 직전)
    seeded.click(b, x, at=end)                               # 끝은 제외
    seeded.click(b, x, at=end - timedelta(hours=6))          # 6h 창의 시작은 포함
    seeded.click(a, x, at=end - timedelta(hours=6, microseconds=1))  # 24h 창에만
    seeded.click(a, y, at=end - timedelta(hours=47))         # 48h 창에만
    seeded.click(a, y, at=end - timedelta(hours=49))         # 어느 창에도 없음
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at, event) VALUES (%s, %s, %s, 'detail_view')",
            (a, x, end - timedelta(hours=1)),
        )
        for nid, at in ((x, end - timedelta(hours=1)), (x, end - timedelta(hours=23)), (x, end - timedelta(hours=25)),
                        (z, end - timedelta(seconds=5)), (z, end + timedelta(microseconds=1))):
            cur.execute(
                "INSERT INTO recommendation_impression_log "
                "(request_id, user_id, news_letter_id, position, source, model_version, created_at) "
                "VALUES (%s, %s, %s, 0, 'realtime', 'heuristic-v1', %s)",
                (str(uuid.uuid4()), b, nid, at),
            )

    with Session(engine) as s:
        counts = load_popularity(SqlRecsysRepository(s), [x, y, z, seeded.by_topic[3][5]], now)

    assert counts == {x: WindowCounts((2, 3, 3), 2), y: WindowCounts((0, 0, 1), 0), z: WindowCounts((0, 0, 0), 1)}


def test_profile_state_reads_the_row_written_by_the_click_api_and_items_carry_the_primary_category(
    api_client, engine, seeded, pg_conn  # noqa: F811
):
    from sqlalchemy.orm import Session

    from app.recsys.sql_repository import SqlRecsysRepository

    uid = seeded.add_user(categories=[seeded.cat_ids[1], seeded.cat_ids[0]])
    nid = seeded.by_topic[2][0]
    with pg_conn.cursor() as cur:  # 두 번째 카테고리 매핑: 대표 카테고리는 ID가 작은 쪽
        cur.execute("INSERT INTO news_letter_categories (news_letter_id, category_id) VALUES (%s, %s)",
                    (nid, seeded.cat_ids[3]))
    assert api_client(uid).post("/logs/newsletter/click", json={"news_letter_id": nid}).status_code == 200

    with Session(engine) as s:
        repo = SqlRecsysRepository(s)
        state, categories = repo.profile_state(uid)
        nobody_state, nobody_categories = repo.profile_state(-1)
        item = repo.items([nid])[nid]

    assert categories == sorted([seeded.cat_ids[0], seeded.cat_ids[1]])
    assert state.hist.hist_len == 1 and state.hist.cat_counts == {seeded.cat_ids[2]: 1}
    np.testing.assert_allclose(state.hist.direction(), seeded.vec_of[nid], atol=1e-6)
    assert state.last_event_at is not None and state.last_event_at.tzinfo is not None
    assert nobody_state.hist.empty and nobody_categories == []
    assert item.category_id == min(seeded.cat_ids[2], seeded.cat_ids[3])
