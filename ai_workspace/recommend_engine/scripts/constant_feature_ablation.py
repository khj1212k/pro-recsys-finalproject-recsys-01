# scripts/constant_feature_ablation.py
# 항상 0이던 피처 user_age_band/user_gender(커밋 d1bc2d2에서 제거)가 LightGBM 예측에 영향을
# 주었는지 확인하는 실험. 같은 데이터에 상수 열 2개를 넣은 모델과 뺀 모델을 학습해 held-out
# 예측을 비교한다. 결과가 같다면, 제거 전에 잰 팀 결과를 제거 후 코드로 재현해도 이 변경
# 때문에 수치가 달라지지 않는다.
#
# 데이터는 합성이다. 실제 로그를 쓰지 않는다. 피처 8개는 모델 입력 열 계약
# (tests/recommend_engine/test_feature_columns_contract.py)과 같은 개수와 비슷한 형태(연속값, 0/1, 개수)로 만든다.
# 한 그룹은 정답 1개와 오답 5개다(config.yaml negative_sample_ratio=5).
# LightGBM 파라미터는 config.yaml의 lightgbm.params를 그대로 쓰고, 시드만 실행마다 바꾼다.
# 학습은 두 방식으로 한다.
# - early_stopping: LGBMRanker.train과 같다(num_boost_round 1000, 검증셋 early stopping 50).
#   이 합성 데이터에서는 몇 번째 트리에서 멈추므로 feature_fraction·bagging 난수가 많이 쓰이지 않는다.
# - fixed_rounds: early stopping 없이 트리 300개. 열 샘플링·배깅이 수백 번 일어나므로, 상수 열이
#   난수 흐름을 바꾸는지를 더 엄하게 본다.
#
# 실행:
#   python scripts/constant_feature_ablation.py --out ../../reports/recsys/constant_feature_ablation_v1.json
import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone

import lightgbm as lgb
import numpy as np
import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

N_FEATURES = 8
ITEMS_PER_GROUP = 6  # 정답 1 + 오답 5
CONSTANT_COLUMNS = ("user_age_band", "user_gender")
POSITIONS = ("front", "middle", "back")
DEFAULT_SEEDS = (42, 7, 2026)
FIXED_ROUNDS = 300
MODES = ("early_stopping", "fixed_rounds")


def load_training_config() -> dict:
    with open(os.path.join(PROJECT_ROOT, "config", "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)["lightgbm"]
    return {
        "params": dict(cfg["params"]),
        "num_boost_round": int(cfg["num_boost_round"]),
        "early_stopping_rounds": int(cfg["early_stopping_rounds"]),
    }


def make_groups(rng: np.random.RandomState, n_groups: int):
    """그룹마다 정답 1개. 정답은 잠재 점수(피처 일부의 선형 결합 + 잡음)가 가장 높은 항목이다."""
    n = n_groups * ITEMS_PER_GROUP
    X = np.column_stack([
        rng.exponential(48.0, n),              # 게시 후 경과 시간 비슷한 값
        rng.binomial(1, 0.3, n),               # 24시간 이내 여부 비슷한 값
        rng.binomial(1, 0.7, n),               # 7일 이내 여부 비슷한 값
        rng.uniform(-0.2, 0.9, n),             # 히스토리 코사인 유사도 비슷한 값
        rng.poisson(1.0, n),                   # 카테고리 일치 개수 비슷한 값
        rng.binomial(1, 0.4, n),               # 카테고리 일치 여부 비슷한 값
        rng.randint(0, 12, n),                 # 카테고리 id 비슷한 값
        rng.randint(1, 6, n),                  # 온보딩 카테고리 수 비슷한 값
    ]).astype(np.float64)
    latent = 2.0 * X[:, 3] + 0.8 * X[:, 5] - 0.01 * X[:, 0] + 0.5 * X[:, 1] + rng.normal(0, 0.5, n)
    y = np.zeros(n, dtype=np.int32)
    for g in range(n_groups):
        s = slice(g * ITEMS_PER_GROUP, (g + 1) * ITEMS_PER_GROUP)
        y[s.start + int(np.argmax(latent[s]))] = 1
    return X, y, [ITEMS_PER_GROUP] * n_groups


def with_constant_columns(X: np.ndarray, position: str) -> np.ndarray:
    zeros = np.zeros((X.shape[0], len(CONSTANT_COLUMNS)))
    at = {"front": 0, "middle": X.shape[1] // 2, "back": X.shape[1]}[position]
    return np.hstack([X[:, :at], zeros, X[:, at:]])


def train(mode: str, params: dict, cfg: dict, X_tr, y_tr, g_tr, X_va, y_va, g_va) -> lgb.Booster:
    dtrain = lgb.Dataset(X_tr, y_tr, group=g_tr)
    if mode == "fixed_rounds":
        return lgb.train(params, dtrain, num_boost_round=FIXED_ROUNDS)
    dvalid = lgb.Dataset(X_va, y_va, group=g_va, reference=dtrain)
    return lgb.train(
        params, dtrain, num_boost_round=cfg["num_boost_round"], valid_sets=[dvalid],
        callbacks=[lgb.early_stopping(cfg["early_stopping_rounds"], verbose=False)],
    )


def trees_used(model: lgb.Booster) -> int:
    return int(model.best_iteration) if model.best_iteration > 0 else int(model.num_trees())


def predict(model: lgb.Booster, X) -> np.ndarray:
    return model.predict(X, num_iteration=model.best_iteration if model.best_iteration > 0 else None)


def run(seeds, n_train_groups: int, n_valid_groups: int, n_test_groups: int, num_threads: int) -> dict:
    cfg = load_training_config()
    runs = []
    for seed in seeds:
        rng = np.random.RandomState(seed)
        X_tr, y_tr, g_tr = make_groups(rng, n_train_groups)
        X_va, y_va, g_va = make_groups(rng, n_valid_groups)
        X_te, _y_te, _g_te = make_groups(rng, n_test_groups)
        params = dict(cfg["params"], random_state=seed, num_threads=num_threads)

        for mode in MODES:
            base = train(mode, params, cfg, X_tr, y_tr, g_tr, X_va, y_va, g_va)
            base_pred = predict(base, X_te)
            for position in POSITIONS:
                aug = train(mode, params, cfg, with_constant_columns(X_tr, position), y_tr, g_tr,
                            with_constant_columns(X_va, position), y_va, g_va)
                aug_pred = predict(aug, with_constant_columns(X_te, position))
                at = {"front": 0, "middle": N_FEATURES // 2, "back": N_FEATURES}[position]
                split_importance = aug.feature_importance("split")
                runs.append({
                    "mode": mode,
                    "seed": seed,
                    "position": position,
                    "trees_without": trees_used(base),
                    "trees_with": trees_used(aug),
                    "max_abs_diff": float(np.max(np.abs(base_pred - aug_pred))),
                    "array_equal": bool(np.array_equal(base_pred, aug_pred)),
                    "constant_columns_split_count": int(split_importance[at:at + len(CONSTANT_COLUMNS)].sum()),
                    "n_test_rows": int(X_te.shape[0]),
                })

    return {
        "experiment": "constant_feature_ablation_v1",
        "run_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": {
            "python": platform.python_version(),
            "lightgbm": lgb.__version__,
            "numpy": np.__version__,
            "num_threads": num_threads,
        },
        "data": {
            "source": "synthetic (numpy RandomState(seed)), no real logs",
            "n_features": N_FEATURES,
            "constant_columns": list(CONSTANT_COLUMNS),
            "items_per_group": ITEMS_PER_GROUP,
            "groups": {"train": n_train_groups, "valid": n_valid_groups, "test": n_test_groups},
        },
        "training": {
            "params_from": "ai_workspace/recommend_engine/config/config.yaml lightgbm.params (random_state overridden per seed)",
            "params": cfg["params"],
            "early_stopping": {"num_boost_round": cfg["num_boost_round"],
                               "early_stopping_rounds": cfg["early_stopping_rounds"]},
            "fixed_rounds": {"num_boost_round": FIXED_ROUNDS},
        },
        "modes": list(MODES),
        "seeds": list(seeds),
        "positions": list(POSITIONS),
        "runs": runs,
        "summary": {
            "n_runs": len(runs),
            "all_array_equal": all(r["array_equal"] for r in runs),
            "max_abs_diff_over_runs": max(r["max_abs_diff"] for r in runs),
            "all_tree_counts_equal": all(r["trees_with"] == r["trees_without"] for r in runs),
            "trees_range_by_mode": {
                m: [min(r["trees_without"] for r in runs if r["mode"] == m),
                    max(r["trees_without"] for r in runs if r["mode"] == m)]
                for m in MODES
            },
            "constant_columns_ever_split": any(r["constant_columns_split_count"] > 0 for r in runs),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="상수 피처 2개 제거 전후 LightGBM 예측 비교(합성 데이터)")
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--train-groups", type=int, default=400)
    parser.add_argument("--valid-groups", type=int, default=100)
    parser.add_argument("--test-groups", type=int, default=100)
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--out", help="결과 JSON 경로 (없으면 표준 출력만)")
    args = parser.parse_args(argv)

    result = run(args.seeds, args.train_groups, args.valid_groups, args.test_groups, args.num_threads)
    for r in result["runs"]:
        print(f"{r['mode']:<14} seed={r['seed']:>4} position={r['position']:<6} trees {r['trees_without']:>3}/"
              f"{r['trees_with']:>3} max|diff|={r['max_abs_diff']:.1e} equal={r['array_equal']} "
              f"const_splits={r['constant_columns_split_count']}")
    print(json.dumps(result["summary"], ensure_ascii=False))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return 0 if result["summary"]["all_array_equal"] else 1


if __name__ == "__main__":
    sys.exit(main())
