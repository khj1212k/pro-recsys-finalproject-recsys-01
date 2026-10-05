"""c4d2a91e7f30(로그 v2, ADR 0025)을 실제 DB에서 검증한다.

- upgrade: 이전 스키마에서 쓰인 클릭·노출·모델 행이 그대로 읽히고, 모델의 역할이 채워진다
- downgrade: 새 로그에 행이 있어도 되돌아가고, 기존 테이블의 행은 남는다
- 모델 선언(app.models)이 마이그레이션 결과와 어긋나지 않는다
"""
import os
import uuid

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BEFORE = "8b7f830013b7"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(os.path.join(REPO_ROOT, "backend", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "backend", "alembic"))
    return cfg


def _columns(pg_conn, table):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def test_upgrade_keeps_old_rows_readable_backfills_roles_and_downgrade_survives_new_rows(
    database_url, pg_conn, monkeypatch
):
    from alembic import command

    monkeypatch.setenv("DATABASE_URL", database_url)
    cfg = _alembic_config()
    suffix = uuid.uuid4().hex[:8]
    model = f"mig-{suffix}"
    old_request = str(uuid.uuid4())
    vec = np.zeros(1024, dtype=np.float32)
    vec[0] = 1.0
    uid = nid = None
    try:
        command.downgrade(cfg, BEFORE)
        assert "request_id" not in _columns(pg_conn, "user_newsletter_ctr_log")
        assert "role" not in _columns(pg_conn, "model_registry")
        with pg_conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" (user_email, user_password_hash, user_nickname, user_created_at) '
                "VALUES (%s, 'h', 'n', NOW()) RETURNING user_id",
                (f"mig-{suffix}@example.com",),
            )
            uid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO news_letter (news_letter_title, news_letter_sentence, news_letter_content, "
                "news_letter_embedding, news_letter_keywords, raw_news_count, news_letter_created_at) "
                "VALUES (%s, '요약', '내용', %s::vector, '[]', 1, NOW()) RETURNING news_letter_id",
                (f"mig-{suffix}", str(vec.tolist())),
            )
            nid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at) VALUES (%s, %s, NOW())",
                (uid, nid),
            )
            cur.execute(
                "INSERT INTO recommendation_impression_log "
                "(request_id, user_id, news_letter_id, position, score, source, model_version) "
                "VALUES (%s, %s, %s, 0, 0.5, 'realtime', 'heuristic-v1')",
                (old_request, uid, nid),
            )
            cur.execute(
                "INSERT INTO model_registry (model_name, model_version, model_text, is_active) "
                "VALUES (%s, 'v1', 'x', true), (%s, 'v2', 'x', false)",
                (model, model),
            )
    finally:
        command.upgrade(cfg, "head")

    try:
        with pg_conn.cursor() as cur:
            # 이전 스키마의 행: 클릭은 연결키 없이 'click', 칸은 새 컬럼이 NULL, 유일 제약에 걸리지 않는다
            cur.execute(
                "SELECT request_id, position, event, dwell_ms FROM user_newsletter_ctr_log WHERE user_id = %s",
                (uid,),
            )
            assert cur.fetchall() == [(None, None, "click", None)]
            cur.execute(
                "SELECT propensity, explored, det_rank, scores_shadow, features "
                "FROM recommendation_impression_log WHERE request_id = %s",
                (old_request,),
            )
            assert cur.fetchall() == [(None, False, None, None, None)]
            # 활성 모델은 'active', 어디서도 읽지 않던 비활성 모델은 'retired'(조용히 shadow가 되지 않는다)
            cur.execute(
                "SELECT model_version, role, is_active FROM model_registry WHERE model_name = %s ORDER BY 1",
                (model,),
            )
            assert cur.fetchall() == [("v1", "active", True), ("v2", "retired", False)]

            cur.execute(
                "SELECT conname FROM pg_constraint WHERE conname IN ("
                "'ck_user_newsletter_ctr_log_event', 'ck_model_registry_role', "
                "'ck_model_registry_active_matches_role', 'uq_recommendation_impression_log_request_position')"
            )
            assert len(cur.fetchall()) == 4
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_user_newsletter_ctr_log_request_id'"
            )
            (indexdef,) = cur.fetchone()
            assert "request_id" in indexdef and "IS NOT NULL" in indexdef  # 연결키가 있는 행만 담는 부분 인덱스

            # 새 로그에 행을 쓴 뒤에도 되돌릴 수 있다
            new_request = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO recommendation_request_log "
                "(request_id, user_id, source, model_version, policy_version, slate_size, shown_count, "
                " explore_positions, candidate_ids) "
                "VALUES (%s, %s, 'realtime', 'heuristic-v1', 'eps-uniform-v1', 1, 1, %s, %s)",
                (new_request, uid, [0], [nid]),
            )
            cur.execute(
                "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at, request_id, position, event) "
                "VALUES (%s, %s, NOW(), %s, 0, 'detail_view')",
                (uid, nid, new_request),
            )

        command.downgrade(cfg, BEFORE)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT to_regclass('recommendation_request_log')")
            assert cur.fetchone() == (None,)
            cur.execute("SELECT count(*) FROM user_newsletter_ctr_log WHERE user_id = %s", (uid,))
            assert cur.fetchone() == (2,)  # 클릭 행은 남고 연결키 컬럼만 사라진다
            cur.execute("SELECT count(*) FROM model_registry WHERE model_name = %s AND is_active", (model,))
            assert cur.fetchone() == (1,)
        assert not {"request_id", "position", "event", "dwell_ms"} & _columns(pg_conn, "user_newsletter_ctr_log")
        assert not {"det_rank", "scores_shadow", "features"} & _columns(pg_conn, "recommendation_impression_log")
    finally:
        command.upgrade(cfg, "head")
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM model_registry WHERE model_name = %s", (model,))
            if uid is not None:
                cur.execute("DELETE FROM recommendation_request_log WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM recommendation_impression_log WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM user_newsletter_ctr_log WHERE user_id = %s", (uid,))
                cur.execute('DELETE FROM "user" WHERE user_id = %s', (uid,))
            if nid is not None:
                cur.execute("DELETE FROM news_letter WHERE news_letter_id = %s", (nid,))


def test_models_match_the_migrated_schema_for_the_logging_v2_columns(database_url):
    """alembic autogenerate가 이 리비전이 더한 테이블·컬럼·유일 제약에서 차이를 보고하지 않는다.
    (부분 인덱스와 CHECK 제약은 위 테스트가 카탈로그에서 직접 본다.)"""
    import sys

    sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    import app.models  # noqa: F401 - 모든 테이블을 metadata에 등록

    ours = ("recommendation_request_log", "det_rank", "scores_shadow", "'features'", "dwell_ms",
            "'event'", "'role'", "uq_recommendation_impression_log_request_position")
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            diffs = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    finally:
        engine.dispose()
    # 한 항목은 연산 튜플이거나(테이블·컬럼 추가/삭제, 제약) 그런 튜플의 목록(컬럼 속성 변경)이다.
    # 인덱스 연산은 뺀다: 식이 들어간 인덱스(created_at DESC, 부분 인덱스)의 비교는 alembic 버전에 따라 다르다.
    flat = [op for d in diffs for op in (d if isinstance(d, list) else [d])]
    related = [
        op for op in flat
        if not str(op[0]).endswith("_index") and any(name in repr(op) for name in ours)
    ]
    assert related == []
    # 컬럼이 실제로 모델에 있는지(위 단언이 "둘 다 없음"으로 통과하지 않도록)
    tables = SQLModel.metadata.tables
    assert {"request_id", "position", "event", "dwell_ms"} <= set(tables["user_newsletter_ctr_log"].c.keys())
    assert {"det_rank", "scores_shadow", "features"} <= set(tables["recommendation_impression_log"].c.keys())
    assert "role" in tables["model_registry"].c and "candidate_ids" in tables["recommendation_request_log"].c
