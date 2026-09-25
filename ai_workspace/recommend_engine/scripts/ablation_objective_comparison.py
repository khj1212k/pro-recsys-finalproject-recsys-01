# scripts/ablation_objective_comparison.py
# Ablation Study: objective='binary'(이전 구현) vs objective='lambdarank'(현재 구현)
# 비교 실험. 동일한 train/valid split에서 두 모델을 각각 학습해 held-out nDCG@k를
# 비교함으로써, "LGBMRanker를 실제 랭킹 손실로 전환한 것이 실제로 도움이 되는가"에
# 대한 정량적 근거를 남긴다.
import os
import sys
import json

import numpy as np
import pandas as pd
from sklearn.metrics import ndcg_score

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data.data_loader import DataLoader
from src.data.lgbm_dataset import LGBMDataset
from src.data.time_split import time_ordered_group_safe_split
from src.features.feature_engineer import FeatureEngineer
from src.models.lgbm_ranker import LGBMRanker
from src.utils.common import load_config, get_logger

logger = get_logger("AblationStudy")


def evaluate_ndcg_at_k(ranker: LGBMRanker, valid_df: pd.DataFrame, k: int = 5) -> float:
    """valid_df(user_id, news_id, label, features...)에 대해 유저별 nDCG@k를 계산해 평균낸다.

    sklearn.metrics.ndcg_score를 사용해 표준 정의를 따른다. positive label이 하나도
    없는 유저는 nDCG가 정의되지 않으므로 평균에서 제외한다.
    """
    scored = ranker.predict(valid_df)
    ndcgs = []
    for _, group in scored.groupby('user_id'):
        if group['label'].sum() == 0:
            continue
        y_true = group['label'].to_numpy().reshape(1, -1)
        y_score = group['score'].to_numpy().reshape(1, -1)
        ndcgs.append(ndcg_score(y_true, y_score, k=k))
    return float(np.mean(ndcgs)) if ndcgs else 0.0


def run_ablation(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    base_lgbm_params: dict,
    k: int = 5,
    num_boost_round: int = 200,
    early_stopping_rounds: int = 20,
    group_key: str = "user_timestamp",
) -> dict:
    """objective='binary'(이전) vs 'lambdarank'(현재)로 각각 학습해 held-out nDCG@k를 비교한다."""
    variants = {
        "binary_classification (이전)": {**base_lgbm_params, "objective": "binary", "metric": "auc"},
        "lambdarank (현재)": {
            **base_lgbm_params,
            "objective": "lambdarank",
            "metric": "ndcg",
            "ndcg_eval_at": [k],
            "label_gain": [0, 1],
        },
    }

    results = {}
    for name, params in variants.items():
        logger.info(f"🧪 학습 중: {name}")
        ranker = LGBMRanker(params=params, group_key=group_key)
        ranker.num_boost_round = num_boost_round
        ranker.early_stopping_rounds = early_stopping_rounds
        ranker.train(train_df.copy(), valid_df=valid_df.copy())
        score = evaluate_ndcg_at_k(ranker, valid_df, k=k)
        results[name] = score
        logger.info(f"   nDCG@{k} = {score:.4f}")

    return results


def main(output_path: str = None) -> dict:
    config = load_config()
    loader = DataLoader(config)
    fe = FeatureEngineer(loader)
    dataset = LGBMDataset(loader, fe)

    neg_ratio = config['lightgbm'].get('negative_sample_ratio', 5)
    full_df = dataset.create_train_dataset(neg_ratio=neg_ratio)

    group_key = config.get('ranking', {}).get('group_key', 'user_timestamp')
    val_ratio = config['data'].get('validation_ratio', 0.2)
    train_df, valid_df = time_ordered_group_safe_split(full_df, val_ratio, group_key=group_key)

    base_params = {
        k: v for k, v in config['lightgbm']['params'].items()
        if k not in ('objective', 'metric', 'ndcg_eval_at', 'label_gain')
    }
    results = run_ablation(train_df, valid_df, base_params, group_key=group_key)

    output_path = output_path or os.path.join(config['output']['results_dir'], "ablation_binary_vs_lambdarank.json")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    logger.info(f"💾 Ablation 결과 저장: {output_path}")
    return results


if __name__ == "__main__":
    main()
