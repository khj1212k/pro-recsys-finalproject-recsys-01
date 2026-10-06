"""scripts/register_model.py: 등록할 행을 만들고, 서빙 어댑터와 피처 목록이 다른 모델을 거절한다 (ADR 0033).
DB에 쓰는 부분은 tests/integration/test_register_model.py(CI)가 본다."""
import importlib.util
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest

from recsys_core import serving
from tests.recsys.parity_sim import train_model_text

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("register_model", REPO / "scripts" / "register_model.py")
register_model = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_model)


def _model_with(feature_names) -> str:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, len(feature_names) if feature_names else 22))
    y = X[:, 0] + rng.normal(0, 0.1, 300)
    data = lgb.Dataset(X, y, feature_name=list(feature_names) if feature_names else "auto")
    return lgb.train({"objective": "regression", "verbose": -1, "num_leaves": 4}, data, num_boost_round=5).model_to_string()


@pytest.fixture(scope="module")
def good_text():
    return train_model_text()


def test_a_model_trained_on_the_adapter_columns_becomes_a_shadow_row_with_the_schema_hash(good_text):
    row = register_model.build_row(good_text, "ebnerd-poolneg-s0", metrics={"ndcg@10": 0.27}, source="model.txt")

    assert (row["model_name"], row["model_version"], row["role"], row["is_active"]) == (
        "ranker", "ebnerd-poolneg-s0", "shadow", False)
    assert row["model_format"] == "lightgbm_text" and row["model_text"] == good_text
    assert row["feature_names"] == list(serving.FEATURE_NAMES)
    assert row["feature_schema_hash"] == serving.SCHEMA_HASH
    reg = row["metrics"]["registration"]
    assert row["metrics"]["ndcg@10"] == 0.27 and reg["source_file"] == "model.txt"
    assert len(reg["model_sha256"]) == 64 and reg["num_trees"] == 60 and reg["feature_schema_version"] == 2


def test_role_active_marks_the_row_active(good_text):
    row = register_model.build_row(good_text, "v2", name="ranker-b", role="active")

    assert (row["model_name"], row["role"], row["is_active"]) == ("ranker-b", "active", True)


@pytest.mark.parametrize(
    "names,expected",
    [
        (serving.FEATURE_NAMES[::-1], "순서가 다름"),
        (serving.FEATURE_NAMES[:-1], "모델에 없는 열: ['hist_len']"),
        (serving.FEATURE_NAMES[:5] + ("pop_ctr_shrunk_24h",) + serving.FEATURE_NAMES[5:], "어댑터에 없는 열"),
        (None, "모델에 없는 열"),  # 열 이름 없이 학습한 모델(Column_0 ...)
    ],
)
def test_a_model_whose_feature_list_differs_from_the_adapter_is_refused_with_the_reason(names, expected):
    with pytest.raises(register_model.ModelRejected, match="서빙 어댑터와 다릅니다") as exc:
        register_model.build_row(_model_with(names), "v1")

    assert expected in str(exc.value)


def test_swapping_two_columns_is_named_in_the_refusal():
    names = list(serving.FEATURE_NAMES)
    names[3], names[4] = names[4], names[3]

    problem = register_model.describe_feature_mismatch(names, serving.FEATURE_NAMES)

    assert "3번째가 모델 'cat_match_count', 어댑터 'hist_cos'" in problem
    assert register_model.describe_feature_mismatch(serving.FEATURE_NAMES, serving.FEATURE_NAMES) is None


@pytest.mark.parametrize("kwargs,match", [
    ({"role": "retired"}, "role"),
    ({"version": "v" * 65}, "version"),
    ({"version": ""}, "version"),
    ({"name": "n" * 65}, "name"),
])
def test_bad_role_name_or_version_is_refused(good_text, kwargs, match):
    with pytest.raises(register_model.ModelRejected, match=match):
        register_model.build_row(good_text, **{"version": "v1", **kwargs})


def test_text_that_is_not_a_lightgbm_model_is_refused():
    with pytest.raises(register_model.ModelRejected, match="읽을 수 없습니다"):
        register_model.build_row("this is not a model", "v1")


def test_dry_run_validates_without_a_database_and_prints_what_would_be_registered(good_text, tmp_path, capsys):
    path = tmp_path / "poolneg_seed0.txt"
    path.write_text(good_text, encoding="utf-8")
    metrics = tmp_path / "metrics.json"
    metrics.write_text(json.dumps({"p2_ndcg@10": 0.2686}), encoding="utf-8")

    code = register_model.main(["--model", str(path), "--version", "s0", "--metrics", str(metrics), "--note", "x",
                                "--dry-run", "--database-url", ""])

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out == {"dry_run": True, "name": "ranker", "version": "s0", "role": "shadow", "features": 22,
                   "feature_schema_hash": serving.SCHEMA_HASH, "model_sha256": out["model_sha256"]}


def test_cli_refuses_a_mismatched_model_and_an_unconfirmed_active_role(good_text, tmp_path, capsys):
    bad = tmp_path / "bad.txt"
    bad.write_text(_model_with(serving.FEATURE_NAMES[::-1]), encoding="utf-8")
    good = tmp_path / "good.txt"
    good.write_text(good_text, encoding="utf-8")

    assert register_model.main(["--model", str(bad), "--version", "v1", "--dry-run"]) == 1
    assert "서빙 어댑터와 다릅니다" in capsys.readouterr().err
    assert register_model.main(["--model", str(good), "--version", "v1", "--role", "active", "--dry-run"]) == 1
    assert "--yes-change-responses" in capsys.readouterr().err
    assert register_model.main(["--model", str(good), "--version", "v1", "--role", "active",
                                "--yes-change-responses", "--dry-run"]) == 0
    # DB 주소 없이 실제 등록을 시도하면 접속하지 않고 끝난다
    assert register_model.main(["--model", str(good), "--version", "v1", "--database-url", ""]) == 1
    assert "DATABASE_URL" in capsys.readouterr().err
