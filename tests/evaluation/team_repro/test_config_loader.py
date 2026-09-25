"""config_loader.py 테스트 (v2 요구사항 4, 6): 각 코드 버전의 실제 config.yaml을
읽어 objective 등 모델링 설정은 그대로 두고, seed/top_k 같은 하네스 값만
덮어쓰는지, 그리고 config_hash가 '파일 내용 + override'에만 반응하고 seed/top_k
에는 반응하지 않는지 검증한다. `ai_workspace/recommend_engine/config/config.yaml`은
git에 커밋된 파일이라(gitignored data/team_archive와 달리) 스킵 조건이 필요 없다."""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEAM_REPRO_DIR = REPO_ROOT / "evaluation" / "recsys" / "team_repro"
CURRENT_ENGINE_ROOT = REPO_ROOT / "ai_workspace" / "recommend_engine"

sys.path.insert(0, str(TEAM_REPRO_DIR))
import config_loader as CFG  # noqa: E402


def test_current_config_keeps_authored_objective_lambdarank():
    config, _hash, overrides = CFG.load_version_config(CURRENT_ENGINE_ROOT, seed=7, top_k=20, objective_override=None)
    assert config["lightgbm"]["params"]["objective"] == "lambdarank"
    assert overrides == []


def test_harness_only_overrides_time_and_output_fields():
    config, _hash, _ = CFG.load_version_config(CURRENT_ENGINE_ROOT, seed=7, top_k=13, objective_override=None)
    assert config["execution_env"] == "debug"
    assert config["output"]["results_dir"] == "/tmp"
    assert config["recommendation"]["top_k"] == 13
    assert config["lightgbm"]["params"]["random_state"] == 7
    # num_leaves/learning_rate 등은 파일에 적힌 그대로 유지돼야 함 (하네스가 덮어쓰지 않음)
    assert config["lightgbm"]["params"]["num_leaves"] == 31
    assert config["lightgbm"]["params"]["learning_rate"] == pytest.approx(0.05)


def test_config_hash_ignores_seed_and_top_k():
    _cfg_a, hash_a, _ = CFG.load_version_config(CURRENT_ENGINE_ROOT, seed=1, top_k=10, objective_override=None)
    _cfg_b, hash_b, _ = CFG.load_version_config(CURRENT_ENGINE_ROOT, seed=999, top_k=50, objective_override=None)
    assert hash_a == hash_b


def test_config_hash_changes_with_objective_override():
    _cfg_a, hash_a, overrides_a = CFG.load_version_config(CURRENT_ENGINE_ROOT, seed=1, top_k=10, objective_override=None)
    cfg_b, hash_b, overrides_b = CFG.load_version_config(CURRENT_ENGINE_ROOT, seed=1, top_k=10, objective_override="binary")
    assert hash_a != hash_b
    assert cfg_b["lightgbm"]["params"]["objective"] == "binary"
    assert cfg_b["lightgbm"]["params"]["metric"] == "auc"
    assert "ndcg_eval_at" not in cfg_b["lightgbm"]["params"]
    assert overrides_a == []
    assert len(overrides_b) == 1  # current 원본은 lambdarank이므로 binary로 override되면 기록에 남아야 함


def test_invalid_objective_override_raises():
    with pytest.raises(ValueError):
        CFG.load_version_config(CURRENT_ENGINE_ROOT, seed=1, top_k=10, objective_override="not_a_real_objective")
