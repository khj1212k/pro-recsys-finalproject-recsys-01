"""로그 v2(ADR 0025)를 실제 PostgreSQL+pgvector에서 검증한다: 요청 로그·칸 로그·클릭 로그의 조인,
칸마다 기록된 propensity, 연결키 없는 클릭, 피로 규칙 조회, 한 트랜잭션 쓰기, shadow 점수.

HTTP 경로(GET /newsletters/today, POST /logs/newsletter/click)는 실제 라우터·실제 표시 단계·운영 배선
(build_sql_service)으로 몬다. 인증만 시드한 사용자로 바꿔 끼운다.
"""
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import partial

import numpy as np
import pytest

# 시드 데이터(주제 4개 x 뉴스레터 12개)와 TestClient 팩토리는 요청 시점 추천 통합 테스트의 것을 쓴다.
from tests.integration.test_realtime_recsys_seeded_db import api_client, engine, seeded  # noqa: F401

REQUESTS_PER_USER = 40
TOP_K = 20


def _simulate(api_client, seeded, pg_conn):  # noqa: F811
    """사용자 5명 x 40요청 = 200요청. 요청마다 응답을 기록하고, 일부는 연결키와 함께, 일부는 기존
    프런트처럼 연결키 없이 클릭한다."""
    rng = np.random.default_rng(20261006)
    users = [seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[t][0]]) for t in range(4)]
    users.append(seeded.add_user())  # 개인 신호가 전혀 없는 사용자
    responses, linked, legacy = [], [], []
    for uid in users:
        client = api_client(uid)
        for _ in range(REQUESTS_PER_USER):
            resp = client.get("/newsletters/today")
            assert resp.status_code == 200
            shown = [item["news_letter_id"] for item in resp.json()]
            request_id = resp.headers["X-Request-Id"]
            responses.append({"uid": uid, "request_id": request_id, "shown": shown,
                              "source": resp.headers["X-Rec-Source"]})
            draw = rng.random()
            if draw < 0.3:
                position = int(rng.integers(0, len(shown)))
                body = {"news_letter_id": shown[position], "request_id": request_id, "position": position}
                assert client.post("/logs/newsletter/click", json=body).status_code == 200
                linked.append((request_id, shown[position], position))
            elif draw < 0.4:
                nid = shown[int(rng.integers(0, len(shown)))]
                assert client.post("/logs/newsletter/click", json={"news_letter_id": nid}).status_code == 200
                legacy.append((uid, nid))
    return users, responses, linked, legacy


def test_request_slot_and_click_logs_join_one_to_one_over_200_requests(api_client, seeded, pg_conn):  # noqa: F811
    from app.recsys.exploration import det_propensity, explore_propensity

    users, responses, linked, legacy = _simulate(api_client, seeded, pg_conn)
    assert len(responses) == 200 and linked and legacy

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT request_id::text, user_id, source, policy_version, cache_hit, slate_size, shown_count, "
            "       explore_positions, explore_pool_size, eligible_count, candidate_ids, fatigue_mode, "
            "       fatigued_count, profile_source, fallback_reason, model_version, latency_ms "
            "FROM recommendation_request_log WHERE user_id = ANY(%s)",
            (users,),
        )
        cols = [d[0] for d in cur.description]
        requests = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
        cur.execute(
            "SELECT request_id::text, position, news_letter_id, explored, propensity, det_rank, score, features "
            "FROM recommendation_impression_log WHERE user_id = ANY(%s) ORDER BY request_id, position",
            (users,),
        )
        slots = defaultdict(list)
        for row in cur.fetchall():
            slots[row[0]].append(row[1:])

    # --- 요청 로그: 응답 하나에 정확히 한 행
    assert set(requests) == {r["request_id"] for r in responses}
    assert len(requests) == 200

    # --- 칸 로그: 요청마다 화면에 나간 칸 그대로
    explore_counts = set()
    for resp in responses:
        request = requests[resp["request_id"]]
        rows = slots[resp["request_id"]]
        assert request["user_id"] == resp["uid"] and request["source"] == resp["source"]
        assert [r[0] for r in rows] == list(range(len(resp["shown"])))
        assert [r[1] for r in rows] == resp["shown"]
        assert request["shown_count"] == request["slate_size"] == len(rows) == TOP_K
        assert request["policy_version"] == "eps-uniform-v1" and request["fallback_reason"] is None
        assert request["fatigue_mode"] == "log" and request["latency_ms"] is not None
        assert set(resp["shown"]) <= set(request["candidate_ids"])
        assert request["eligible_count"] == len(request["candidate_ids"])

        # 칸마다 기록된 propensity가 그 요청에 기록된 값들로 계산한 닫힌 식과 같다
        m, slate = len(request["explore_positions"]), request["slate_size"]
        explore_counts.add(m)
        assert request["explore_pool_size"] == request["eligible_count"] - (slate - m)
        assert [r[0] for r in rows if r[2]] == request["explore_positions"]
        det_ranks = [r[4] for r in rows if not r[2]]
        assert det_ranks == list(range(slate - m))
        for position, _, explored, propensity, det_rank, score, features in rows:
            if explored:
                assert det_rank is None
                assert propensity == pytest.approx(explore_propensity(slate, m, request["explore_pool_size"]))
            else:
                assert propensity == pytest.approx(det_propensity(det_rank, position, slate, m))
            assert score is not None
            if request["model_version"] == "heuristic-v1":
                assert len(bytes(features)) == 4 * 4  # 휴리스틱 4항, float32
    # 신호 없는 사용자의 첫 요청은 4칸, 프로필이 있는 사용자는 2칸
    assert explore_counts == {2, 4}

    # --- 캐시: 클릭이 없으면 결정론 목록은 캐시에서 오지만 탐색 칸은 요청마다 다시 뽑힌다
    by_user = defaultdict(list)
    for resp in responses:
        by_user[resp["uid"]].append(requests[resp["request_id"]])
    for uid, rows in by_user.items():
        assert rows[0]["cache_hit"] is False and rows[0]["fatigued_count"] == 0
        hits = [r for r in rows if r["cache_hit"]]
        assert len(hits) >= 10
        assert len({tuple(r["explore_positions"]) for r in hits}) > 1
        # 피로 규칙(log 모드): 같은 뉴스레터가 3번 넘게 노출되면 세기 시작한다
        assert max(r["fatigued_count"] for r in rows) >= 1

    # --- 조인: 클릭 -> 칸 (request_id, news_letter_id), 칸 -> 요청 (request_id)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(s.impression_id), count(DISTINCT s.impression_id), "
            "       count(*) FILTER (WHERE s.position = c.position) "
            "FROM user_newsletter_ctr_log c "
            "LEFT JOIN recommendation_impression_log s "
            "  ON s.request_id = c.request_id AND s.news_letter_id = c.news_letter_id "
            "WHERE c.user_id = ANY(%s) AND c.request_id IS NOT NULL AND c.event = 'click'",
            (users,),
        )
        assert cur.fetchone() == (len(linked),) * 4  # 클릭마다 칸이 정확히 하나, 위치도 같다
        cur.execute(
            "SELECT count(*), count(r.request_id) FROM recommendation_impression_log s "
            "LEFT JOIN recommendation_request_log r ON r.request_id = s.request_id "
            "WHERE s.user_id = ANY(%s)",
            (users,),
        )
        assert cur.fetchone() == (200 * TOP_K, 200 * TOP_K)
        cur.execute(
            "SELECT count(*) FROM recommendation_request_log r WHERE r.user_id = ANY(%s) AND r.shown_count <> "
            "(SELECT count(*) FROM recommendation_impression_log s WHERE s.request_id = r.request_id)",
            (users,),
        )
        assert cur.fetchone() == (0,)
        cur.execute(
            "SELECT c.request_id::text, c.news_letter_id, c.position FROM user_newsletter_ctr_log c "
            "WHERE c.user_id = ANY(%s) AND c.request_id IS NOT NULL",
            (users,),
        )
        assert sorted(cur.fetchall()) == sorted(linked)

        # --- 연결키 없는 클릭(기존 프런트)도 그대로 저장된다
        cur.execute(
            "SELECT user_id, news_letter_id FROM user_newsletter_ctr_log "
            "WHERE user_id = ANY(%s) AND request_id IS NULL AND position IS NULL "
            "  AND dwell_ms IS NULL AND event = 'click'",
            (users,),
        )
        assert sorted(cur.fetchall()) == sorted(legacy)


def test_a_click_without_request_id_still_stores_and_the_event_column_is_constrained(
    api_client, seeded, pg_conn  # noqa: F811
):
    import psycopg2

    uid = seeded.add_user()
    nid = seeded.by_topic[0][0]

    resp = api_client(uid).post("/logs/newsletter/click", json={"news_letter_id": nid})
    raw_log_id = seeded.click(uid, seeded.by_topic[0][1])  # 컬럼을 모르는 기존 쓰기 경로(시드·벤치·배치)

    assert resp.status_code == 200 and resp.json()["status"] == "success"
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT log_id, news_letter_id, request_id, position, event, dwell_ms "
            "FROM user_newsletter_ctr_log WHERE user_id = %s ORDER BY log_id",
            (uid,),
        )
        assert cur.fetchall() == [
            (resp.json()["log_id"], nid, None, None, "click", None),
            (raw_log_id, seeded.by_topic[0][1], None, None, "click", None),
        ]
    with pytest.raises(psycopg2.errors.CheckViolation), pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_newsletter_ctr_log (user_id, news_letter_id, created_at, event) "
            "VALUES (%s, %s, NOW(), 'purchase')",
            (uid, nid),
        )


def test_detail_view_rows_are_not_read_as_clicks_by_the_request_path(api_client, engine, seeded, pg_conn):  # noqa: F811
    from sqlalchemy.orm import Session

    from app.recsys.sql_repository import SqlRecsysRepository

    uid = seeded.add_user()
    viewed, clicked = seeded.by_topic[1][0], seeded.by_topic[2][0]
    request_id = str(uuid.uuid4())
    client = api_client(uid)
    body = {"news_letter_id": viewed, "request_id": request_id, "position": 4, "event": "detail_view", "dwell_ms": 12_000}
    assert client.post("/logs/newsletter/click", json=body).status_code == 200

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with Session(engine) as s:
        repo = SqlRecsysRepository(s)
        assert repo.last_click_id(uid) is None
        assert repo.clicked_among(uid, [viewed, clicked]) == set()
        assert repo.short_term_vector(uid, since, 20) is None

    click_log_id = client.post("/logs/newsletter/click", json={"news_letter_id": clicked}).json()["log_id"]
    with Session(engine) as s:
        repo = SqlRecsysRepository(s)
        assert repo.last_click_id(uid) == click_log_id
        assert repo.clicked_among(uid, [viewed, clicked]) == {clicked}
        np.testing.assert_allclose(repo.short_term_vector(uid, since, 20), seeded.vec_of[clicked], atol=1e-6)
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT request_id::text, position, event, dwell_ms FROM user_newsletter_ctr_log "
            "WHERE user_id = %s AND event = 'detail_view'",
            (uid,),
        )
        assert cur.fetchall() == [(request_id, 4, "detail_view", 12_000)]


def _slot(request_id, uid, nid, position, **extra):
    return {"request_id": request_id, "user_id": uid, "news_letter_id": nid, "position": position,
            "score": 0.5, "source": "realtime", "model_version": "heuristic-v1", **extra}


def test_fatigued_among_counts_recent_impressions_of_one_user(engine, seeded):  # noqa: F811
    from sqlalchemy.orm import Session

    from app.recsys.sql_repository import SqlImpressionWriter, SqlRecsysRepository

    me, other = seeded.add_user(), seeded.add_user()
    thrice, twice, old = seeded.by_topic[0][:3]
    now = datetime.now(timezone.utc)
    writer = SqlImpressionWriter(engine)
    for _ in range(3):
        writer([_slot(str(uuid.uuid4()), me, thrice, 0)])
        writer([_slot(str(uuid.uuid4()), other, twice, 0)])
    for _ in range(2):
        writer([_slot(str(uuid.uuid4()), me, twice, 0)])
    # 48시간 밖의 노출 3번
    writer([_slot(str(uuid.uuid4()), me, old, 0, created_at=now - timedelta(hours=60 + i)) for i in range(1)])
    writer([_slot(str(uuid.uuid4()), me, old, 0, created_at=now - timedelta(hours=70))])
    writer([_slot(str(uuid.uuid4()), me, old, 0, created_at=now - timedelta(hours=80))])

    with Session(engine) as s:
        repo = SqlRecsysRepository(s)
        since = now - timedelta(hours=48)
        assert repo.fatigued_among(me, [thrice, twice, old], since, 3) == {thrice}
        assert repo.fatigued_among(me, [thrice, twice, old], since, 2) == {thrice, twice}
        assert repo.fatigued_among(me, [thrice, twice, old], now - timedelta(hours=100), 3) == {thrice, old}
        assert repo.fatigued_among(other, [thrice, twice, old], since, 3) == {twice}
        assert repo.fatigued_among(me, [twice], since, 3) == set()
        assert repo.fatigued_among(me, [], since, 3) == set()


def test_request_row_and_slot_rows_are_written_together_or_not_at_all(engine, seeded, pg_conn):  # noqa: F811
    from sqlalchemy import exc

    from app.recsys.scoring import decode_features, encode_features
    from app.recsys.sql_repository import SqlImpressionWriter

    uid = seeded.add_user()
    a, b = seeded.by_topic[0][:2]
    writer = SqlImpressionWriter(engine)

    def request_row(request_id):
        return {
            "request_id": request_id, "user_id": uid, "source": "realtime", "model_version": "heuristic-v1",
            "policy_version": "eps-uniform-v1", "profile_source": "long_term", "cache_hit": True,
            "fallback_reason": None, "candidate_count": 3, "eligible_count": 2, "explore_pool_size": 1,
            "slate_size": 2, "shown_count": 2, "explore_positions": [1], "candidate_ids": [a, b],
            "feature_schema_version": 1, "shadow_versions": ["lgbm:ranker@v1"], "fatigue_mode": "log",
            "fatigued_count": 0, "latency_ms": 12,
        }

    def counts(request_id):
        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM recommendation_request_log WHERE request_id = %s", (request_id,))
            n_requests = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM recommendation_impression_log WHERE request_id = %s", (request_id,))
            return n_requests, cur.fetchone()[0]

    features = np.array([0.25, np.nan, 0.5, 1.0], dtype=np.float32)
    extra = {"explored": False, "propensity": 1.0, "det_rank": 0,
             "scores_shadow": {"lgbm:ranker@v1": 0.75}, "features": encode_features(features)}

    # 한 응답의 같은 위치에 칸이 둘이면 유일 제약에 걸리고, 요청 행도 남지 않는다
    broken = str(uuid.uuid4())
    with pytest.raises(exc.IntegrityError):
        writer([_slot(broken, uid, a, 0, **extra), _slot(broken, uid, b, 0, **extra)], request_row(broken))
    assert counts(broken) == (0, 0)

    good = str(uuid.uuid4())
    writer(
        [_slot(good, uid, a, 0, **extra),
         _slot(good, uid, b, 1, **{**extra, "explored": True, "propensity": 0.5, "det_rank": None})],
        request_row(good),
    )
    assert counts(good) == (1, 2)
    # 같은 request_id로 한 번 더 쓰면 요청 로그의 기본 키에 걸리고 칸도 늘지 않는다
    with pytest.raises(exc.IntegrityError):
        writer([_slot(good, uid, a, 5, **extra)], request_row(good))
    assert counts(good) == (1, 2)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT explore_positions, candidate_ids, shadow_versions, cache_hit, policy_version, latency_ms "
            "FROM recommendation_request_log WHERE request_id = %s",
            (good,),
        )
        assert cur.fetchone() == ([1], [a, b], ["lgbm:ranker@v1"], True, "eps-uniform-v1", 12)
        cur.execute(
            "SELECT position, explored, propensity, det_rank, scores_shadow, features "
            "FROM recommendation_impression_log WHERE request_id = %s ORDER BY position",
            (good,),
        )
        first, second = cur.fetchall()
    assert first[:5] == (0, False, 1.0, 0, {"lgbm:ranker@v1": 0.75})
    assert second[:5] == (1, True, 0.5, None, {"lgbm:ranker@v1": 0.75})
    np.testing.assert_array_equal(decode_features(bytes(first[5])), features)

    # 빈 응답: 칸 없이 요청 행만
    empty = str(uuid.uuid4())
    writer([], {**request_row(empty), "source": "empty", "slate_size": 0, "shown_count": 0})
    assert counts(empty) == (1, 0)


def _two_feature_model_text():
    import lightgbm as lgb

    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, 2))
    y = (X[:, 0] > 0).astype(int)
    return lgb.train(
        {"objective": "binary", "verbose": -1, "num_leaves": 4, "seed": 0},
        lgb.Dataset(X, y),
        num_boost_round=10,
    ).model_to_string()


def test_a_registered_shadow_model_is_scored_and_logged_without_changing_the_response(engine, seeded, pg_conn):  # noqa: F811
    import json

    from app.recsys.config import RecsysConfig
    from app.recsys.lgbm_scorer import LightGBMScorer, SqlModelSource
    from app.recsys.scoring import HeuristicScorer, ScorerStack, item_ages_hours
    from app.recsys.service import build_service
    from app.recsys.sql_repository import SqlImpressionWriter, SqlRecsysRepository, sql_repo_scope
    from sqlalchemy.orm import Session

    def features(state, items, now):
        return np.column_stack([item_ages_hours(items, now), [it.raw_news_count for it in items]])

    features.feature_names = ["age_hours", "raw_news_count"]
    model_name = f"test-shadow-{uuid.uuid4().hex[:8]}"
    uid = seeded.add_user(long_term=seeded.vec_of[seeded.by_topic[0][0]])
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_registry (model_name, model_version, model_text, feature_names, role) "
            "VALUES (%s, 'v1', %s, %s, 'shadow')",
            (model_name, _two_feature_model_text(), json.dumps(features.feature_names)),
        )

    def service(scorer, seed):
        cfg = RecsysConfig(time_budget_ms=5000)
        rng = np.random.default_rng(seed)
        return build_service(
            cfg,
            repo_factory=partial(sql_repo_scope, engine, cfg.time_budget_ms),
            scorer=scorer,
            impression_writer=SqlImpressionWriter(engine),
            rng_factory=lambda request_id: rng,
        )

    def recommend(svc):
        with Session(engine) as s:
            return svc.recommend(uid, fallback_repo=SqlRecsysRepository(s))

    shadow = LightGBMScorer(
        SqlModelSource(engine), fallback=None, feature_fn=features, model_name=model_name, role="shadow", slot=0
    )
    plain, stacked = service(HeuristicScorer(), 5), service(ScorerStack(HeuristicScorer(), [shadow]), 5)
    try:
        expected = recommend(plain)
        rec = recommend(stacked)
        stacked.log_impressions(uid, rec, rec.news_letter_ids)

        version = f"lgbm:{model_name}@v1"
        assert rec.news_letter_ids == expected.news_letter_ids  # 같은 시드의 탐색, 같은 활성 점수: 응답이 같다
        assert rec.model_version == "heuristic-v1" and rec.shadow_versions == [version]
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT shadow_versions FROM recommendation_request_log WHERE request_id = %s", (rec.request_id,)
            )
            assert cur.fetchone() == ([version],)
            cur.execute(
                "SELECT explored, scores_shadow FROM recommendation_impression_log "
                "WHERE request_id = %s ORDER BY position",
                (rec.request_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == TOP_K and sum(r[0] for r in rows) == 2
        for _, scores_shadow in rows:  # 탐색 칸 포함 모든 칸
            assert list(scores_shadow) == [version] and 0.0 <= scores_shadow[version] <= 1.0
    finally:
        plain.shutdown()
        stacked.shutdown()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM model_registry WHERE model_name = %s", (model_name,))
