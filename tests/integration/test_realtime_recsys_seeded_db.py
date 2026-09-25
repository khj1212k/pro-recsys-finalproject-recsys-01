"""요청 시점 추천(app.recsys)을 작게 시드한 실제 PostgreSQL+pgvector에서 검증한다.

다른 통합 테스트가 남긴 뉴스레터가 있어도 깨지지 않도록, 결과 중 이 테스트가
시드한 항목만 골라 상대적인 성질(주제 비율 변화 등)만 검사한다.
"""
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial

import numpy as np
import pytest
from sqlalchemy import text

DIM = 1024
TOPICS = 4
PER_TOPIC = 12


def _topic_vec(rng, axis):
    v = np.zeros(DIM, dtype=np.float32)
    v[axis] = 1.0
    v += rng.normal(0, 0.02, DIM).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def engine(database_url):
    from sqlalchemy import create_engine

    from app.database import register_pgvector_on_connect

    eng = create_engine(database_url)
    register_pgvector_on_connect(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def seeded(pg_conn):
    """TOPICS개 주제 x PER_TOPIC개 뉴스레터(모두 신선, 주제별 카테고리) + 헬퍼."""
    rng = np.random.default_rng(7)
    suffix = uuid.uuid4().hex[:8]
    created = {"news": [], "users": [], "cats": []}
    topic_of = {}
    vec_of = {}
    now = datetime.now(timezone.utc)
    with pg_conn.cursor() as cur:
        cat_ids = []
        for t in range(TOPICS):
            code = 900000 + int(uuid.uuid4().int % 90000)
            cur.execute(
                "INSERT INTO category (category_name, category_code) VALUES (%s, %s) RETURNING category_id",
                (f"topic{t}-{suffix}", code),
            )
            cat_ids.append(cur.fetchone()[0])
        created["cats"] = cat_ids
        for t in range(TOPICS):
            for j in range(PER_TOPIC):
                v = _topic_vec(rng, t)
                cur.execute(
                    "INSERT INTO news_letter (news_letter_title, news_letter_sentence, news_letter_content, "
                    "news_letter_embedding, news_letter_keywords, raw_news_count, news_letter_created_at) "
                    "VALUES (%s, %s, %s, %s::vector, %s, %s, %s) RETURNING news_letter_id",
                    (f"t{t}-{j}-{suffix}", "요약", "내용", str(v.tolist()), "[]", 1 + j % 3,
                     now - timedelta(hours=1 + j)),
                )
                nid = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO news_letter_categories (news_letter_id, category_id) VALUES (%s, %s)",
                    (nid, cat_ids[t]),
                )
                created["news"].append(nid)
                topic_of[nid] = t
                vec_of[nid] = v

    def add_user(long_term=None, onboarding=(), categories=()):
        with pg_conn.cursor() as cur:
            cur.execute(
                'INSERT INTO "user" (user_email, user_password_hash, user_nickname, user_created_at, user_embedding) '
                "VALUES (%s, 'h', 'n', NOW(), %s::vector) RETURNING user_id",
                (f"rt-{uuid.uuid4().hex[:10]}@example.com",
                 None if long_term is None else str(np.asarray(long_term).tolist())),
            )
            uid = cur.fetchone()[0]
            for nid in onboarding:
                cur.execute(
                    "INSERT INTO user_preferred_newsletter (news_letter_id, user_id) VALUES (%s, %s)",
                    (nid, uid),
                )
            for cid in categories:
                cur.execute(
                    "INSERT INTO user_preferred_categories (category_id, user_id) VALUES (%s, %s)",
                    (cid, uid),
                )
        created["users"].append(uid)
        return uid

    def click(uid, nid, at=None):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at) "
                "VALUES (%s, %s, %s) RETURNING log_id",
                (uid, nid, at or datetime.now(timezone.utc)),
            )
            return cur.fetchone()[0]

    ctx = type("Seeded", (), {})()
    ctx.topic_of, ctx.vec_of, ctx.cat_ids = topic_of, vec_of, created["cats"]
    ctx.add_user, ctx.click, ctx.now = add_user, click, now
    ctx.by_topic = {t: [n for n, tt in topic_of.items() if tt == t] for t in range(TOPICS)}
    try:
        yield ctx
    finally:
        with pg_conn.cursor() as cur:
            uids, nids = created["users"], created["news"]
            if uids:
                cur.execute("DELETE FROM recommendation_impression_log WHERE user_id = ANY(%s)", (uids,))
                cur.execute("DELETE FROM news_letter_today_batch WHERE user_id = ANY(%s)", (uids,))
                cur.execute("DELETE FROM user_newsletter_ctr_log WHERE user_id = ANY(%s)", (uids,))
                cur.execute("DELETE FROM user_preferred_newsletter WHERE user_id = ANY(%s)", (uids,))
                cur.execute("DELETE FROM user_preferred_categories WHERE user_id = ANY(%s)", (uids,))
                cur.execute('DELETE FROM "user" WHERE user_id = ANY(%s)', (uids,))
            if nids:
                cur.execute("DELETE FROM news_letter_categories WHERE news_letter_id = ANY(%s)", (nids,))
                cur.execute("DELETE FROM news_letter WHERE news_letter_id = ANY(%s)", (nids,))
            cur.execute("DELETE FROM category WHERE category_id = ANY(%s)", (created["cats"],))


def _service(engine, **cfg_overrides):
    from app.recsys.config import RecsysConfig
    from app.recsys.service import build_service
    from app.recsys.sql_repository import SqlImpressionWriter, sql_repo_scope

    # CI 러너의 첫 요청(임베딩 캐시 미적중) 지연에 흔들리지 않도록 기본 예산을 넉넉히 둔다.
    # 예산 자체의 동작은 fault injection 테스트가 따로 검증한다.
    cfg = RecsysConfig(**{"time_budget_ms": 5000, **cfg_overrides})
    return build_service(
        cfg,
        repo_factory=partial(sql_repo_scope, engine, cfg.time_budget_ms),
        impression_writer=SqlImpressionWriter(engine),
    )


@contextmanager
def _request_repo(engine):
    from sqlalchemy.orm import Session

    from app.recsys.sql_repository import SqlRecsysRepository

    with Session(engine) as s:
        yield SqlRecsysRepository(s)


def _recommend(service, engine, uid):
    with _request_repo(engine) as repo:
        return service.recommend(uid, fallback_repo=repo)


def test_a_click_moves_the_next_response_toward_the_clicked_topic(engine, seeded):
    long_term = seeded.vec_of[seeded.by_topic[0][0]] + 0.6 * seeded.vec_of[seeded.by_topic[2][0]]
    uid = seeded.add_user(long_term=long_term / np.linalg.norm(long_term))
    service = _service(engine)

    def topic1_share(rec):
        mine = [n for n in rec.news_letter_ids if n in seeded.topic_of][:10]
        return sum(seeded.topic_of[n] == 1 for n in mine) / max(1, len(mine))

    before = _recommend(service, engine, uid)
    clicked = seeded.by_topic[1][0]
    seeded.click(uid, clicked)
    after = _recommend(service, engine, uid)

    print(f"\n[recsys click-shift] topic1 share@10 before={topic1_share(before):.2f} after={topic1_share(after):.2f}")
    assert before.source == after.source == "realtime"
    assert after.cache_hit is False
    assert clicked not in after.news_letter_ids
    assert topic1_share(after) > topic1_share(before)
    service.shutdown()


def test_a_click_older_than_24h_does_not_count_as_short_term(engine, seeded):
    uid = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])
    seeded.click(uid, seeded.by_topic[1][0], at=datetime.now(timezone.utc) - timedelta(hours=30))

    from app.recsys.sql_repository import SqlRecsysRepository
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        vec = SqlRecsysRepository(s).short_term_vector(
            uid, datetime.now(timezone.utc) - timedelta(hours=24), 20
        )
    assert vec is None


def test_brand_new_users_never_get_an_empty_list(engine, seeded):
    service = _service(engine)
    nobody = seeded.add_user()
    onboarded = seeded.add_user(onboarding=seeded.by_topic[3][:2])
    categorized = seeded.add_user(categories=[seeded.cat_ids[2]])

    rec_nobody = _recommend(service, engine, nobody)
    rec_onboarded = _recommend(service, engine, onboarded)
    rec_categorized = _recommend(service, engine, categorized)

    assert rec_nobody.source == "cold_start_popular" and rec_nobody.news_letter_ids
    assert rec_onboarded.source == "cold_start_onboarding"
    mine = [n for n in rec_onboarded.news_letter_ids if n in seeded.topic_of][:5]
    assert sum(seeded.topic_of[n] == 3 for n in mine) >= 3
    assert rec_categorized.source == "cold_start_category"
    service.shutdown()


def test_db_fault_triggers_batch_fallback_within_budget_and_frees_the_connection(engine, seeded, pg_conn):
    from app.recsys.config import RecsysConfig
    from app.recsys.service import build_service
    from app.recsys.sql_repository import sql_repo_scope

    uid = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])
    batch_ids = seeded.by_topic[2][:5]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO news_letter_today_batch (user_id, news_letter_ids, created_at) VALUES (%s, %s, NOW())",
            (uid, json.dumps(batch_ids)),
        )

    budget_ms = 200

    @contextmanager
    def stalled_repo():
        # 실시간 경로의 첫 쿼리가 DB에서 멈춘 상황. 서버 쪽 statement_timeout이 쿼리를
        # 끊어야 요청이 폴백으로 응답한 뒤 남은 작업 스레드가 커넥션을 오래 붙잡지 않는다.
        # (여기서는 요청 쪽 예산 초과가 항상 먼저 나도록 예산의 2배로 둔다.)
        with sql_repo_scope(engine, 2 * budget_ms) as repo:
            repo.session.execute(text("SELECT pg_sleep(5)"))
            yield repo

    service = build_service(RecsysConfig(time_budget_ms=budget_ms), repo_factory=stalled_repo)
    started = time.perf_counter()
    rec = _recommend(service, engine, uid)
    elapsed = time.perf_counter() - started

    assert rec.source == "batch"
    assert rec.fallback_reason == "timeout"
    assert rec.news_letter_ids == batch_ids
    assert elapsed < 1.0
    assert service.counters.get("fallback.timeout") == 1

    deadline = time.monotonic() + 3.0
    while engine.pool.checkedout() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert engine.pool.checkedout() == 0
    service.shutdown()


def test_knn_binds_the_query_as_a_numpy_array(engine, seeded):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from app.recsys.sql_repository import SqlRecsysRepository

    seen = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if "<=>" in statement:
            seen.append(parameters)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        target = seeded.by_topic[2][0]
        with Session(engine) as s:
            ids = SqlRecsysRepository(s).knn_ids(
                seeded.vec_of[target], datetime.now(timezone.utc) - timedelta(hours=72), 5
            )
    finally:
        event.remove(engine, "before_cursor_execute", spy)

    assert ids[0] == target
    assert all(seeded.topic_of.get(n) == 2 for n in ids if n in seeded.topic_of)
    assert len(seen) == 1 and isinstance(seen[0]["q"], np.ndarray)


def test_python_list_vector_param_fails_which_is_why_numpy_is_required(engine, seeded):
    from sqlalchemy import exc
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        with pytest.raises(exc.ProgrammingError, match="operator does not exist"):
            s.execute(
                text("SELECT news_letter_id FROM news_letter ORDER BY news_letter_embedding <=> :q LIMIT 1"),
                {"q": seeded.vec_of[seeded.by_topic[0][0]].tolist()},
            )


def test_impressions_are_written_to_the_log_table(engine, seeded, pg_conn):
    uid = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])
    service = _service(engine)
    rec = _recommend(service, engine, uid)

    service.log_impressions(uid, rec, rec.news_letter_ids[:3])

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT request_id::text, news_letter_id, position, source, model_version "
            "FROM recommendation_impression_log WHERE user_id = %s ORDER BY position",
            (uid,),
        )
        rows = cur.fetchall()
    assert [r[1] for r in rows] == rec.news_letter_ids[:3]
    assert [r[2] for r in rows] == [0, 1, 2]
    assert {r[0] for r in rows} == {rec.request_id}
    assert {(r[3], r[4]) for r in rows} == {("realtime", "heuristic-v1")}
    service.shutdown()


def test_sql_path_latency_p50_p95(engine, seeded):
    """SQL 경로 지연(결과 캐시 끔). 절대값은 러너 사양에 좌우되므로 느슨한 상한만 두고
    수치는 출력한다 - CI에서는 별도 스텝이 -s로 이 테스트를 돌려 로그에 남긴다."""
    uid = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])
    seeded.click(uid, seeded.by_topic[1][0])
    service = _service(engine, cache_ttl_s=0)

    def run(n):
        samples = []
        for _ in range(n):
            t0 = time.perf_counter()
            rec = _recommend(service, engine, uid)
            samples.append((time.perf_counter() - t0) * 1000)
            assert rec.source == "realtime"
        return samples

    cold = run(1)  # 첫 요청: 뉴스레터 임베딩 캐시 미적중
    warm = sorted(run(40))
    p50, p95 = warm[len(warm) // 2], warm[int(len(warm) * 0.95) - 1]
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else float("nan")
    print(
        f"\n[recsys sql-path] candidates<={service.cfg.candidate_cap} "
        f"cold_first={cold[0]:.1f}ms warm_p50={p50:.1f}ms warm_p95={p95:.1f}ms "
        f"n={len(warm)} loadavg1={load:.2f}"
    )
    assert p95 < 1000
    service.shutdown()


def test_request_sessions_holding_every_app_connection_do_not_starve_the_realtime_path(
    database_url, seeded
):
    """API 요청은 인증 조회 때부터 앱 풀 커넥션 하나를 쥔 채 추천을 기다린다. 실시간 경로가
    같은 풀에서 커넥션을 또 빌리면, 동시 요청 수가 풀 크기에 닿는 순간 요청들이 커넥션을
    쥐고 작업 스레드는 커넥션을 기다리는 순환 대기가 생긴다(ADR 0015 벤치마크에서 16개
    동시 클라이언트가 QueuePool 30초 타임아웃으로 죽음)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.database import register_pgvector_on_connect
    from app.recsys.config import RecsysConfig
    from app.recsys.runtime import build_sql_service
    from app.recsys.sql_repository import SqlRecsysRepository

    # 예산은 부하가 큰 러너에서도 정상 경로가 넉넉히 끝나도록 크게 두고, 앱 풀 대기
    # 한도(pool_timeout)는 그보다 길게 둬서 풀을 공유하면 반드시 예산 초과로 폴백하게 한다.
    app_engine = create_engine(database_url, pool_size=2, max_overflow=0, pool_timeout=10)
    register_pgvector_on_connect(app_engine)
    uid = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])
    service = build_sql_service(RecsysConfig(time_budget_ms=5000), app_engine, database_url)
    try:
        with Session(app_engine) as held, Session(app_engine) as request_session:
            held.execute(text("SELECT 1"))
            request_session.execute(text("SELECT 1"))
            assert app_engine.pool.checkedout() == 2
            rec = service.recommend(uid, fallback_repo=SqlRecsysRepository(request_session))
        assert rec.source == "realtime", rec.fallback_reason
    finally:
        service.shutdown()
        app_engine.dispose()
