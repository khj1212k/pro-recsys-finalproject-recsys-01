"""model_registry 테이블(8b7f830013b7)과 LightGBMScorer의 실제 DB 경로."""
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest


def _model_text():
    import lightgbm as lgb

    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, 2))
    y = (X[:, 0] > 0).astype(int)
    return lgb.train(
        {"objective": "binary", "verbose": -1, "num_leaves": 4, "seed": 0},
        lgb.Dataset(X, y),
        num_boost_round=10,
    ).model_to_string()


@pytest.fixture
def engine(database_url):
    from sqlalchemy import create_engine

    eng = create_engine(database_url)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def model_name(pg_conn):
    name = f"test-ranker-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM model_registry WHERE model_name = %s", (name,))


def _insert(pg_conn, name, version, text, active, feature_names=None):
    import json

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO model_registry (model_name, model_version, model_text, feature_names, is_active) "
            "VALUES (%s, %s, %s, %s, %s)",
            (name, version, text, json.dumps(feature_names) if feature_names else None, active),
        )


def test_only_one_active_model_per_name(pg_conn, model_name):
    import psycopg2

    _insert(pg_conn, model_name, "v1", "x", True)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert(pg_conn, model_name, "v2", "x", True)
    _insert(pg_conn, model_name, "v2", "x", False)  # 비활성 버전은 여러 개 가능


def test_scorer_serves_the_active_registry_model_and_reloads_on_switch(engine, pg_conn, model_name):
    from app.recsys.lgbm_scorer import LightGBMScorer, SqlModelSource
    from app.recsys.scoring import HeuristicScorer
    from app.recsys.types import Item, UserState

    text = _model_text()
    _insert(pg_conn, model_name, "v1", text, True, ["f0", "f1"])
    _insert(pg_conn, model_name, "v2", text, False, ["f0", "f1"])

    def features(state, items, now):
        return np.array([[float(i), 0.0] for i in range(len(items))])

    features.feature_names = ["f0", "f1"]
    t = [0.0]
    scorer = LightGBMScorer(
        SqlModelSource(engine), HeuristicScorer(), features, model_name=model_name, clock=lambda: t[0]
    )
    now = datetime.now(timezone.utc)
    vec = np.ones(4, dtype=np.float32) / 2
    items = [Item(1, vec, now - timedelta(hours=1), 1), Item(2, vec, now, 1)]
    state = UserState(user_id=1, profile=vec)

    assert scorer.score(state, items, now).model_version == f"lgbm:{model_name}@v1"

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE model_registry SET is_active = false WHERE model_name = %s", (model_name,))
        cur.execute(
            "UPDATE model_registry SET is_active = true WHERE model_name = %s AND model_version = 'v2'",
            (model_name,),
        )
    t[0] = 61.0

    assert scorer.score(state, items, now).model_version == f"lgbm:{model_name}@v2"
