"""scripts/register_model.py를 실제 model_registry에 대해 돈다 (ADR 0033)."""
import importlib.util
import os
import uuid
from pathlib import Path

import psycopg2
import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def register_model():
    spec = importlib.util.spec_from_file_location("register_model", REPO / "scripts" / "register_model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tx_conn(database_url):
    """register()는 한 트랜잭션으로 쓴다: autocommit이 꺼진 연결이 필요하다."""
    conn = psycopg2.connect(database_url)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def model_name(pg_conn):
    name = f"test-register-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM model_registry WHERE model_name = %s", (name,))


@pytest.fixture(scope="module")
def model_text():
    # tests.recsys.parity_sim은 app.*를 임포트한다(app.security가 임포트 시점에 읽는 값을 채워 둔다).
    os.environ.setdefault("SECRET_KEY", "test-secret")
    os.environ.setdefault("ALGORITHM", "HS256")
    os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
    from tests.recsys.parity_sim import train_model_text

    return train_model_text()


def _rows(pg_conn, name):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT model_version, role, is_active, feature_names, feature_schema_hash, metrics "
            "FROM model_registry WHERE model_name = %s ORDER BY model_id",
            (name,),
        )
        return cur.fetchall()


def test_a_registered_model_is_a_shadow_row_that_the_serving_source_reads_back(
    register_model, tx_conn, pg_conn, model_name, model_text, database_url
):
    from sqlalchemy import create_engine

    from app.recsys.lgbm_scorer import SqlModelSource
    from recsys_core import serving

    result = register_model.register(tx_conn, register_model.build_row(model_text, "s0", name=model_name,
                                                                       metrics={"p2_ndcg@10": 0.27}))

    ((version, role, is_active, names, schema_hash, metrics),) = _rows(pg_conn, model_name)
    assert (version, role, is_active) == ("s0", "shadow", False) and result["retired_versions"] == []
    assert names == list(serving.FEATURE_NAMES) and schema_hash == serving.SCHEMA_HASH
    assert metrics["p2_ndcg@10"] == 0.27 and metrics["registration"]["num_trees"] == 60

    engine = create_engine(database_url)
    try:
        source = SqlModelSource(engine)
        assert source.shadow_versions(model_name, 2) == ["s0"] and source.active_version(model_name) is None
        loaded = source.load(model_name, "s0")
    finally:
        engine.dispose()
    assert loaded.model_text == model_text and loaded.feature_names == list(serving.FEATURE_NAMES)
    assert loaded.feature_schema_hash == serving.SCHEMA_HASH


def test_registering_an_active_model_retires_the_previous_one_in_the_same_transaction(
    register_model, tx_conn, pg_conn, model_name, model_text
):
    first = register_model.register(tx_conn, register_model.build_row(model_text, "a1", name=model_name, role="active"))
    second = register_model.register(tx_conn, register_model.build_row(model_text, "a2", name=model_name, role="active"))
    register_model.register(tx_conn, register_model.build_row(model_text, "s1", name=model_name))

    assert first["retired_versions"] == [] and second["retired_versions"] == ["a1"]
    assert [(r[0], r[1], r[2]) for r in _rows(pg_conn, model_name)] == [
        ("a1", "retired", False), ("a2", "active", True), ("s1", "shadow", False)
    ]


def test_a_duplicate_version_is_refused_and_leaves_the_active_model_in_place(
    register_model, tx_conn, pg_conn, model_name, model_text
):
    register_model.register(tx_conn, register_model.build_row(model_text, "a1", name=model_name, role="active"))

    with pytest.raises(register_model.ModelRejected, match="이미 등록"):
        register_model.register(tx_conn, register_model.build_row(model_text, "a1", name=model_name, role="active"))

    # 실패한 등록이 이전 활성 모델을 내려놓은 채로 끝나지 않는다(한 트랜잭션)
    assert [(r[0], r[1], r[2]) for r in _rows(pg_conn, model_name)] == [("a1", "active", True)]


def test_register_refuses_an_autocommit_connection(register_model, pg_conn, model_name, model_text):
    with pytest.raises(ValueError, match="autocommit"):
        register_model.register(pg_conn, register_model.build_row(model_text, "a1", name=model_name))
    assert _rows(pg_conn, model_name) == []


def test_the_cli_registers_from_a_file(register_model, pg_conn, model_name, model_text, database_url, tmp_path, capsys):
    path = tmp_path / "poolneg_seed0.txt"
    path.write_text(model_text, encoding="utf-8")

    code = register_model.main(["--model", str(path), "--version", "cli-1", "--name", model_name,
                                "--note", "integration", "--database-url", database_url])

    assert code == 0 and '"role": "shadow"' in capsys.readouterr().out
    ((version, role, _, _, _, metrics),) = _rows(pg_conn, model_name)
    assert (version, role) == ("cli-1", "shadow")
    assert metrics["note"] == "integration" and metrics["registration"]["source_file"] == "poolneg_seed0.txt"
