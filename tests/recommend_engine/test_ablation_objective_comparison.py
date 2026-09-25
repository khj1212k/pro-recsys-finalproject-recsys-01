import numpy as np
import pandas as pd
from datetime import datetime


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


def test_evaluate_ndcg_at_k_returns_value_in_valid_range():
    from scripts.ablation_objective_comparison import evaluate_ndcg_at_k
    from src.models.lgbm_ranker import LGBMRanker

    train_df = _synthetic_dataset(n_users=10, rows_per_user=6, seed=1)
    valid_df = _synthetic_dataset(n_users=5, rows_per_user=6, seed=2)

    ranker = LGBMRanker(params={"objective": "lambdarank", "metric": "ndcg", "verbose": -1})
    ranker.num_boost_round = 10
    ranker.early_stopping_rounds = 5
    ranker.train(train_df, valid_df=valid_df)

    score = evaluate_ndcg_at_k(ranker, valid_df, k=5)

    assert 0.0 <= score <= 1.0


def test_run_ablation_compares_binary_and_lambdarank_variants():
    from scripts.ablation_objective_comparison import run_ablation

    train_df = _synthetic_dataset(n_users=15, rows_per_user=6, seed=3)
    valid_df = _synthetic_dataset(n_users=6, rows_per_user=6, seed=4)

    base_params = {"num_leaves": 15, "learning_rate": 0.1, "verbose": -1, "random_state": 42}
    results = run_ablation(train_df, valid_df, base_params, k=5, num_boost_round=10, early_stopping_rounds=5)

    assert set(results.keys()) == {"binary_classification (이전)", "lambdarank (현재)"}
    for score in results.values():
        assert 0.0 <= score <= 1.0
