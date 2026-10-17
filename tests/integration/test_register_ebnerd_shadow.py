"""scripts/register_ebnerd_shadow.py를 실제 model_registry에 대해 돈다 (ADR 0037). 모델 파일은 합성(22열)이다."""
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def tool():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("register_ebnerd_shadow", REPO / "scripts" / "register_ebnerd_shadow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def model_name(pg_conn):
    name = f"test-ebnerd-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM model_registry WHERE model_name = %s", (name,))


@pytest.fixture(scope="module")
def model_text():
    os.environ.setdefault("SECRET_KEY", "test-secret")
    os.environ.setdefault("ALGORITHM", "HS256")
    os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
    from tests.recsys.parity_sim import train_model_text

    return train_model_text()  # 트리 60개


def _files(tmp_path, model_text, features):
    report = {"meta": {"label": "[EB-NeRD]", "code_sha": "7d7c9f3", "evidence": {"grade": "preregistered-run"},
                       "preregistration": {"commit": "c9d1861"}, "environments": [{"lightgbm": "4.7.0"}]},
              "models": {}}
    paths = []
    for arm in ("poolneg", "poolneg_mask0"):  # 주모델을 마지막에: 첫 shadow 칸
        (tmp_path / f"{arm}_s0.txt").write_text(model_text, encoding="utf-8")
        summary = {"model": arm, "seed": 0, "best_iteration": 60, "features": features}
        (tmp_path / f"{arm}_s0.json").write_text(json.dumps(summary), encoding="utf-8")
        report["models"][arm] = [summary]
        paths.append(tmp_path / f"{arm}_s0.txt")
    (tmp_path / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return paths, tmp_path / "report.json"


def test_both_models_are_registered_as_shadows_in_order_and_the_serving_source_reads_them(
    tool, pg_conn, model_name, model_text, database_url, tmp_path, capsys
):
    from sqlalchemy import create_engine

    from app.recsys.lgbm_scorer import SqlModelSource
    from recsys_core import serving

    paths, report = _files(tmp_path, model_text, list(serving.FEATURE_NAMES))
    code = tool.main([*(a for p in paths for a in ("--model", str(p))), "--report", str(report),
                      "--name", model_name, "--database-url", database_url])

    assert code == 0, capsys.readouterr().err
    with pg_conn.cursor() as cur:
        cur.execute("SELECT model_version, role, is_active, feature_schema_hash, metrics FROM model_registry "
                    "WHERE model_name = %s ORDER BY model_id", (model_name,))
        rows = cur.fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("ebnerd-v1.2-poolneg-s0", "shadow", False), ("ebnerd-v1.2-poolneg_mask0-s0", "shadow", False)]
    assert {r[3] for r in rows} == {serving.SCHEMA_HASH}
    assert all("비상업" in r[4]["license"] and r[4]["offline"]["best_iteration"] == 60 for r in rows)

    engine = create_engine(database_url)
    try:
        # 서빙은 최근 등록 순으로 shadow 칸을 채운다: 주모델이 첫 칸
        assert SqlModelSource(engine).shadow_versions(model_name, 2) == [
            "ebnerd-v1.2-poolneg_mask0-s0", "ebnerd-v1.2-poolneg-s0"]
        assert SqlModelSource(engine).active_version(model_name) is None
    finally:
        engine.dispose()


def test_a_rejected_file_registers_nothing(tool, pg_conn, model_name, model_text, database_url, tmp_path):
    from recsys_core import serving

    paths, report = _files(tmp_path, model_text, list(serving.FEATURE_NAMES))
    paths[1].with_suffix(".json").write_text(json.dumps(
        {"model": "poolneg_mask0", "seed": 0, "best_iteration": 59, "features": list(serving.FEATURE_NAMES)}))

    assert tool.main([*(a for p in paths for a in ("--model", str(p))), "--report", str(report),
                      "--name", model_name, "--database-url", database_url]) == 1
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM model_registry WHERE model_name = %s", (model_name,))
        assert cur.fetchone()[0] == 0
