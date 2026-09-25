"""v2 REQUIRED CHANGE #4: 각 코드 버전(team-final/fix-snapshot/current)의 실제
`config/config.yaml`을 읽어서 쓴다. 하네스는 시간/출력 경로처럼 평가 재현에 꼭
필요한 필드만 덮어쓰고, `lightgbm.params.objective` 같은 모델링 설정은 파일에
적힌 그대로 둔다.

v1의 결함(어드버서리얼 리뷰 MAJOR #8): pipeline.py가 `_default_lightgbm_params()`로
버전별 파라미터를 하드코딩했는데, fix-snapshot의 실제 config.yaml은 `lightgbm`이
아니라 `objective: lambdarank`(FIX #5가 이미 적용된 상태)인데도 하네스가
`objective: binary`로 돌렸다 - "fix-snapshot" 행이 사실은 "fix-snapshot 코드 +
binary로 덮어쓴 설정"이었다.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import List, Optional, Tuple

import yaml


def _deep_copy(obj):
    return json.loads(json.dumps(obj, default=str))


def load_version_config(
    engine_root: Path,
    seed: int,
    top_k: int,
    objective_override: Optional[str] = None,
) -> Tuple[dict, str, List[str]]:
    """반환: (하네스가 실제로 쓸 effective config, config_hash, 적용된 override 목록).

    config_hash는 '디스크에 적힌 원본 config.yaml + 요청된 objective_override'만의
    함수다 - seed/top_k처럼 실행마다 바뀌는 하네스 값은 해시에서 뺀다(그렇지 않으면
    캐시 키에 이미 포함된 seed가 config_hash에도 새어들어 같은 설정을 다른 해시로
    잘못 표시하게 된다).
    """
    cfg_path = Path(engine_root) / "config" / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        raw_config = yaml.safe_load(f)

    overrides: List[str] = []
    override_key = objective_override or "none"
    if objective_override and objective_override != "none":
        raw_objective = (raw_config.get("lightgbm") or {}).get("params", {}).get("objective")
        if raw_objective != objective_override:
            overrides.append(f"objective:{raw_objective}->{objective_override}")

    config_hash = hashlib.sha256(
        (json.dumps(raw_config, sort_keys=True, default=str) + "|" + override_key).encode("utf-8")
    ).hexdigest()[:16]

    config = _deep_copy(raw_config)

    # --- 하네스가 반드시 덮어써야 하는 필드 (평가 재현을 위한 시간/출력 경로 -
    #     모델링 설정이 아니므로 config_hash에는 포함하지 않는다) -------------
    config["execution_env"] = "debug"
    config.setdefault("output", {})
    config["output"]["results_dir"] = "/tmp"
    config["output"]["checkpoint_dir"] = "/tmp"

    config.setdefault("recommendation", {})
    config["recommendation"]["top_k"] = top_k
    config["recommendation"].setdefault("use_mmr", True)
    config["recommendation"].setdefault("mmr_pool_multiplier", 4)
    config["recommendation"]["method"] = config["recommendation"].get("method", "lgbm")

    config.setdefault("lightgbm", {})
    config["lightgbm"].setdefault("params", {})
    config["lightgbm"]["params"]["random_state"] = seed
    config["lightgbm"].setdefault("negative_sample_ratio", 5)
    config["lightgbm"].setdefault("num_boost_round", 1000)
    config["lightgbm"].setdefault("early_stopping_rounds", 50)

    config.setdefault("ranking", {})
    config["ranking"].setdefault("group_key", "user_timestamp")

    config.setdefault("data", {})
    config["data"].setdefault("max_history_days", 28)
    config["data"].setdefault("validation_ratio", 0.2)

    config.setdefault("time_decay", {"news_half_life_days": 7, "min_weight": 0.01})

    if objective_override and objective_override != "none":
        params = config["lightgbm"]["params"]
        if objective_override == "binary":
            params["objective"] = "binary"
            params["metric"] = "auc"
            params.pop("ndcg_eval_at", None)
            params.pop("label_gain", None)
        else:
            raise ValueError(f"지원하지 않는 objective_override: {objective_override}")

    return config, config_hash, overrides
