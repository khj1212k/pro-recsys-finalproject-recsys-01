import numpy as np
import pandas as pd
from datetime import datetime
from unittest.mock import MagicMock


def _synthetic_dataset(n_users=10, rows_per_user=6, n_features=4, seed=0):
    """CORRECTION #5: 기본 group_key='user_timestamp'는 '_timestamp' 컬럼을 요구하므로,
    유저별로 고유한 클릭 시각을 부여해 lgbm_dataset.py가 만드는 형태를 재현한다."""
    rng = np.random.RandomState(seed)
    base = datetime(2026, 1, 1)
    rows = []
    for uid in range(n_users):
        for j in range(rows_per_user):
            row = {
                "user_id": uid, "news_id": j, "label": 1 if j == 0 else 0,
                "_timestamp": base + pd.Timedelta(hours=uid),
            }
            for f in range(n_features):
                row[f"f{f}"] = rng.rand()
            rows.append(row)
    return pd.DataFrame(rows).sample(frac=1.0, random_state=seed).reset_index(drop=True)


def test_suggest_params_overlays_base_params_with_trial_values():
    from scripts.tune_hyperparams import suggest_params

    trial = MagicMock()
    trial.suggest_int.side_effect = lambda name, low, high: {"num_leaves": 63, "bagging_freq": 3}[name]
    trial.suggest_float.side_effect = lambda name, low, high, **kw: {
        "learning_rate": 0.08, "feature_fraction": 0.85, "bagging_fraction": 0.75
    }[name]

    base_params = {"objective": "lambdarank", "metric": "ndcg", "random_state": 42}
    params = suggest_params(trial, base_params)

    assert params["num_leaves"] == 63
    assert params["learning_rate"] == 0.08
    assert params["bagging_freq"] == 3
    # base_params의 다른 키는 그대로 보존되어야 함
    assert params["objective"] == "lambdarank"
    assert params["random_state"] == 42


def test_make_objective_runs_real_lgbm_training_and_returns_metric():
    from scripts.tune_hyperparams import make_objective
    import optuna

    train_df = _synthetic_dataset(n_users=10, rows_per_user=6, seed=1)
    valid_df = _synthetic_dataset(n_users=4, rows_per_user=6, seed=2)

    base_params = {
        "objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [5],
        "label_gain": [0, 1], "random_state": 42, "verbose": -1,
    }
    objective = make_objective(train_df, valid_df, base_params, metric_key="ndcg@5")

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=2)

    assert len(study.trials) == 2
    assert isinstance(study.best_value, float)
    assert 0.0 <= study.best_value <= 1.0
