# scripts/tune_hyperparams.py
# LightGBM 하이퍼파라미터를 Optuna로 탐색해 근거 있는 값을 찾는다.
# config.yaml에 고정값으로만 존재하던 num_leaves/learning_rate 등을
# 실제 탐색 과정을 거쳐 결정했다는 근거를 남기기 위한 스크립트.
import os
import sys
import json
import argparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data.data_loader import DataLoader
from src.data.lgbm_dataset import LGBMDataset
from src.data.time_split import time_ordered_group_safe_split
from src.features.feature_engineer import FeatureEngineer
from src.models.lgbm_ranker import LGBMRanker
from src.utils.common import load_config, get_logger

logger = get_logger("HyperparamTuner")


def suggest_params(trial, base_params: dict) -> dict:
    """Optuna trial로부터 탐색할 하이퍼파라미터를 샘플링해 base_params 위에 덮어씌운다."""
    params = dict(base_params)
    params.update({
        "num_leaves": trial.suggest_int("num_leaves", 15, 127),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
        "feature_fraction": trial.suggest_float("feature_fraction", 0.6, 1.0),
        "bagging_fraction": trial.suggest_float("bagging_fraction", 0.6, 1.0),
        "bagging_freq": trial.suggest_int("bagging_freq", 1, 10),
    })
    return params


def make_objective(train_df, valid_df, base_params: dict, group_key: str = "user_timestamp", metric_key: str = "ndcg@5"):
    """train_df/valid_df를 클로저로 캡처한 Optuna objective 함수를 생성한다.

    DB/DataLoader와 분리된 순수 함수 형태라 합성 데이터로도 검증 가능하다.
    """
    def objective(trial):
        params = suggest_params(trial, base_params)
        ranker = LGBMRanker(params=params, group_key=group_key)
        ranker.num_boost_round = 200
        ranker.early_stopping_rounds = 20
        ranker.train(train_df, valid_df=valid_df)

        best_score = ranker.model.best_score.get("valid_0", {})
        return best_score.get(metric_key, 0.0)

    return objective


def tune(n_trials: int = 10, output_path: str = None) -> dict:
    import optuna

    config = load_config()
    loader = DataLoader(config)
    fe = FeatureEngineer(loader)
    dataset = LGBMDataset(loader, fe)

    neg_ratio = config['lightgbm'].get('negative_sample_ratio', 5)
    full_df = dataset.create_train_dataset(neg_ratio=neg_ratio)

    group_key = config.get('ranking', {}).get('group_key', 'user_timestamp')
    val_ratio = config['data'].get('validation_ratio', 0.2)
    train_df, valid_df = time_ordered_group_safe_split(full_df, val_ratio, group_key=group_key)

    base_params = config['lightgbm']['params']
    objective = make_objective(train_df, valid_df, base_params, group_key=group_key)

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials)

    logger.info(f"🏆 Best params: {study.best_params} (score={study.best_value:.4f})")

    output_path = output_path or os.path.join(config['output']['results_dir'], "best_hyperparams.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump({
            "best_params": study.best_params,
            "best_score": study.best_value,
            "n_trials": n_trials,
        }, f, indent=2)

    logger.info(f"💾 결과 저장: {output_path}")
    return study.best_params


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--n-trials", type=int, default=10)
    args = parser.parse_args()
    tune(n_trials=args.n_trials)
