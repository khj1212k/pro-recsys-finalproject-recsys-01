"""scripts/register_ebnerd_shadow.py: M4에서 학습한 모델을 shadow로만, 판정 리포트의 그 모델일 때만, 서빙 어댑터와
열이 같을 때만 등록 행으로 만든다 (ADR 0037). 모델 파일은 합성이다 - EB-NeRD 모델은 저장소에 없다.
DB에 쓰는 부분은 tests/integration/test_register_ebnerd_shadow.py(CI)가 본다."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest

from recsys_core import serving
from tests.recsys.parity_sim import train_model_text

REPO = Path(__file__).resolve().parents[2]
FEATURES = list(serving.FEATURE_NAMES)


def _load():
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("register_ebnerd_shadow", REPO / "scripts" / "register_ebnerd_shadow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load()


@pytest.fixture(scope="module")
def model_text():
    return train_model_text()  # 22열, 트리 60개


def _report(best=60, features=FEATURES):
    entry = lambda arm, seed: {"model": arm, "seed": seed, "best_iteration": best, "best_es_score": 0.77,  # noqa: E731
                               "features": list(features)}
    return {
        "meta": {"label": "[EB-NeRD]", "evidence": {"grade": "preregistered-run"}, "code_sha": "7d7c9f3",
                 "preregistration": {"commit": "c9d1861", "sha256": "c17399c0"},
                 "environments": [{"lightgbm": "4.7.0", "python": "3.13.16", "system": "Linux", "machine": "x86_64"}]},
        "models": {arm: [entry(arm, s) for s in (0, 1, 2)]
                   for arm in ("poolneg", "poolneg_mask0", "poolneg_rank")},
    }


def _checkpoint(tmp_path, model_text, arm="poolneg_mask0", seed=0, best=60, features=FEATURES):
    d = tmp_path / "models"
    d.mkdir(exist_ok=True)
    (d / f"{arm}_s{seed}.txt").write_text(model_text, encoding="utf-8")
    (d / f"{arm}_s{seed}.json").write_text(json.dumps(
        {"model": arm, "seed": seed, "best_iteration": best, "features": list(features)}), encoding="utf-8")
    return d / f"{arm}_s{seed}.txt"


def test_the_m4_models_named_by_the_adr_were_trained_on_the_adapter_columns():
    """ADR 0037의 확인: 커밋된 판정 리포트에 적힌 두 arm(seed 0~2)의 피처 목록이 서빙 어댑터의 22열과 이름·순서가 같다."""
    report = json.loads(tool.REPORT.read_text(encoding="utf-8"))
    for arm in tool.REGISTRABLE:
        for seed in (0, 1, 2):
            entry = tool.report_entry(report, arm, seed)
            assert entry["features"] == FEATURES and len(FEATURES) == 22
            assert entry["best_iteration"] > 0


def test_a_checked_model_becomes_a_shadow_row_with_provenance_licence_and_definition_differences(tmp_path, model_text):
    path = _checkpoint(tmp_path, model_text)

    row = tool.prepare(path, _report(), "ab" * 32, "ranker")

    assert (row["model_name"], row["model_version"], row["role"], row["is_active"]) == (
        "ranker", "ebnerd-v1.2-poolneg_mask0-s0", "shadow", False)
    assert row["feature_names"] == FEATURES and row["feature_schema_hash"] == serving.SCHEMA_HASH
    m = row["metrics"]
    assert m["offline"]["report_sha256"] == "ab" * 32 and m["offline"]["best_iteration"] == 60
    assert m["offline"]["preregistration_commit"] == "c9d1861" and m["offline"]["trained_with"]["lightgbm"] == "4.7.0"
    assert "비상업" in m["license"] and m["adr"].startswith("docs/adr/0037")
    diffs = m["training_feature_definition_differs"]
    assert diffs["short_max_events"] == {"training": None, "serving": 20}
    assert diffs["item_lag_s"] == {"training": 0, "serving": 2}
    assert diffs["session"]["training"] == "EB-NeRD session_id"
    assert set(diffs) == {"short_max_events", "item_lag_s", "session"}  # 나머지 정의는 같다
    assert m["registration"]["source_file"] == "poolneg_mask0_s0.txt"


@pytest.mark.parametrize("arm, seed", [("poolneg_rank", 0), ("poolneg_mask0", 1)])
def test_only_the_arms_and_seed_the_adr_names_are_registered(tmp_path, model_text, arm, seed):
    path = _checkpoint(tmp_path, model_text, arm=arm, seed=seed)
    with pytest.raises(tool.ModelRejected, match="허용"):
        tool.prepare(path, _report(), "x", "ranker")


@pytest.mark.parametrize(
    "report_kw, ckpt_kw, match",
    [
        ({"best": 61}, {}, "best_iteration"),                       # 리포트와 실행기 요약이 다르다
        ({"best": 59}, {"best": 59}, "트리 60개"),                   # 본문의 트리 수가 best_iteration과 다르다
        ({"features": FEATURES[::-1]}, {"features": FEATURES[::-1]}, "판정 리포트의 그 모델"),
    ],
)
def test_a_model_that_is_not_the_one_the_report_judged_is_rejected(tmp_path, model_text, report_kw, ckpt_kw, match):
    path = _checkpoint(tmp_path, model_text, **ckpt_kw)
    with pytest.raises(tool.ModelRejected, match=match):
        tool.prepare(path, _report(**report_kw), "x", "ranker")


def test_a_model_whose_columns_differ_from_the_adapter_stops_at_the_check(tmp_path):
    """열이 23개(축소 CTR을 더한 arm처럼)면 리포트·요약이 서로 맞아도 어댑터 검사에서 멈춘다."""
    names = FEATURES + ["pop_ctr_shrunk_24h"]
    rng = np.random.default_rng(0)
    X = rng.normal(size=(300, len(names)))
    text = lgb.train({"objective": "regression", "verbose": -1, "num_leaves": 4},
                     lgb.Dataset(X, X[:, 0], feature_name=names), num_boost_round=5).model_to_string()
    path = _checkpoint(tmp_path, text, best=5, features=names)

    with pytest.raises(tool.ModelRejected, match="서빙 어댑터와 다릅니다"):
        tool.prepare(path, _report(best=5, features=names), "x", "ranker")


def test_the_runner_summary_must_travel_with_the_model_and_match_its_name(tmp_path, model_text):
    path = _checkpoint(tmp_path, model_text)
    path.with_suffix(".json").write_text(json.dumps({"model": "poolneg", "seed": 0, "best_iteration": 60,
                                                     "features": FEATURES}), encoding="utf-8")
    with pytest.raises(tool.ModelRejected, match="파일 이름"):
        tool.prepare(path, _report(), "x", "ranker")
    path.with_suffix(".json").unlink()
    with pytest.raises(tool.ModelRejected, match="실행기 요약이 없습니다"):
        tool.prepare(path, _report(), "x", "ranker")
    renamed = path.with_name("mask0.txt")
    path.rename(renamed)
    with pytest.raises(tool.ModelRejected, match="<arm>_s<seed>"):
        tool.prepare(renamed, _report(), "x", "ranker")


def test_a_model_file_inside_the_work_tree_must_be_git_ignored(tmp_path, model_text, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("/data/\n", encoding="utf-8")
    monkeypatch.setattr(tool, "REPO", repo)

    (repo / "data").mkdir()
    (repo / "work").mkdir()
    ignored = _checkpoint(repo / "data", model_text)
    loose = _checkpoint(repo / "work", model_text)

    tool.check_location(ignored)  # gitignore된 자리: 통과
    tool.check_location(tmp_path / "outside.txt")  # 저장소 밖: 통과
    with pytest.raises(tool.ModelRejected, match="무시되지 않는"):
        tool.check_location(loose)
    subprocess.run(["git", "-C", str(repo), "add", "-f", str(ignored)], check=True)
    with pytest.raises(tool.ModelRejected, match="추적하는"):
        tool.check_location(ignored)


def test_dry_run_checks_every_file_before_anything_is_registered(tmp_path, model_text, capsys):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report()), encoding="utf-8")
    good = _checkpoint(tmp_path, model_text, arm="poolneg")
    main_model = _checkpoint(tmp_path, model_text)

    assert tool.main(["--model", str(good), "--model", str(main_model), "--report", str(report), "--dry-run"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [x["version"] for x in lines] == ["ebnerd-v1.2-poolneg-s0", "ebnerd-v1.2-poolneg_mask0-s0"]
    assert all(x["dry_run"] and x["role"] == "shadow" for x in lines)

    bad = _checkpoint(tmp_path, model_text, arm="poolneg_rank")
    # 둘째 파일이 거절되면 DB 주소가 없어도(접속 전에) 실패한다 - 첫째도 등록되지 않는다
    assert tool.main(["--model", str(good), "--model", str(bad), "--report", str(report)]) == 1
    assert "등록하지 않음" in capsys.readouterr().err
