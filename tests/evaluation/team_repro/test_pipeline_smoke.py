"""요구된 '작은 서브샘플로 하나의 variant를 end-to-end로 돌리는' 스모크 테스트.

실제 아카이브를 5명 유저 x 20건 뉴스레터로 축소한 뒤, current 코드 버전으로
train -> inference(MMR) -> evaluate 전체 흐름을 실제 recommend_engine 클래스
(FeatureEngineer/LGBMDataset/LGBMRanker/MMRReranker/Evaluator, 전부 수정 없음)로
직접 실행한다. pipeline.py의 run_one()과 동일한 흐름이지만 서브프로세스 없이
인프로세스로 돌려서 빠르게(수 초) 검증한다.
"""
import dataclasses
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEAM_REPRO_DIR = REPO_ROOT / "evaluation" / "recsys" / "team_repro"
ENGINE_ROOT = REPO_ROOT / "ai_workspace" / "recommend_engine"

sys.path.insert(0, str(TEAM_REPRO_DIR))
import file_loader as FL  # noqa: E402
import metrics as M  # noqa: E402

pytestmark = pytest.mark.skipif(
    not FL.DATA_ROOT.exists(), reason="data/team_archive가 없는 환경 (gitignored 아카이브 데이터)"
)


def _install_torch_stub():
    if "torch" not in sys.modules:
        torch_stub = types.ModuleType("torch")
        torch_stub.Tensor = type("Tensor", (), {})
        sys.modules["torch"] = torch_stub


@pytest.fixture(scope="module")
def small_bundle():
    bundle = FL.load_archive_bundle()
    small_users = bundle.users.head(5).reset_index(drop=True)
    small_news = bundle.newsletters.head(20).reset_index(drop=True)
    keep_uids = set(small_users["user_id"])
    keep_nids = set(small_news["news_letter_id"])

    small_logs = bundle.ctr_logs[
        bundle.ctr_logs["user_id"].isin(keep_uids) & bundle.ctr_logs["news_letter_id"].isin(keep_nids)
    ].reset_index(drop=True)
    small_cats = bundle.categories[bundle.categories["news_letter_id"].isin(keep_nids)].reset_index(drop=True)
    small_pref_cats = bundle.preferred_categories[bundle.preferred_categories["user_id"].isin(keep_uids)]
    small_pref_nl = bundle.preferred_newsletters[bundle.preferred_newsletters["user_id"].isin(keep_uids)]

    return dataclasses.replace(
        bundle,
        newsletters=small_news,
        users=small_users,
        ctr_logs=small_logs,
        categories=small_cats,
        preferred_categories=small_pref_cats,
        preferred_newsletters=small_pref_nl,
    )


def test_end_to_end_tiny_subsample_current_version(small_bundle):
    if str(ENGINE_ROOT) not in sys.path:
        sys.path.insert(0, str(ENGINE_ROOT))
    _install_torch_stub()

    from src.data.data_loader import DataLoader, NewsItem  # noqa: E402
    from src.features.feature_engineer import FeatureEngineer  # noqa: E402
    from src.data.lgbm_dataset import LGBMDataset  # noqa: E402
    from src.models.lgbm_ranker import LGBMRanker  # noqa: E402
    from src.core.reranker import create_reranker_from_config  # noqa: E402
    from src.core.evaluator import Evaluator  # noqa: E402

    bundle = small_bundle
    # 클릭이 하나도 없으면 테스트가 무의미해지므로 사전 확인
    n_clicks = int((bundle.ctr_logs["is_clicked"] == 1).sum())
    assert n_clicks > 0, "테스트 서브샘플에 클릭이 없습니다 - head() 크기를 늘려야 합니다."

    pinned_now = bundle.dataset_end_time.to_pydatetime()
    config = {
        "data": {"max_history_days": 28, "validation_ratio": 0.3},
        "time_decay": {"news_half_life_days": 7, "min_weight": 0.01},
        "ranking": {"group_key": "user_timestamp"},
        "lightgbm": {
            "params": {
                "objective": "lambdarank",
                "metric": "ndcg",
                "ndcg_eval_at": [5],
                "label_gain": [0, 1],
                "verbose": -1,
                "random_state": 0,
                "min_data_in_leaf": 1,
                "min_data_in_bin": 1,
            },
            "num_boost_round": 5,
            "early_stopping_rounds": 5,
            "negative_sample_ratio": 2,
        },
        "recommendation": {
            "method": "lgbm",
            "top_k": 3,
            "use_mmr": True,
            "mmr_pool_multiplier": 2,
            "mmr_lambda": {"few_categories": 0.8, "medium_categories": 0.7, "many_categories": 0.6, "default": 0.7},
        },
        "output": {"results_dir": "/tmp", "checkpoint_dir": "/tmp"},
        "execution_env": "debug",
    }

    loader = FL.build_file_data_loader(
        DataLoader, NewsItem, config, bundle, pinned_now, label_mode=FL.LabelMode.CLICKS_ONLY
    )

    fe = FeatureEngineer(loader)
    dataset = LGBMDataset(loader, fe, seed=0)
    full_df = dataset.create_train_dataset(neg_ratio=config["lightgbm"]["negative_sample_ratio"])
    assert not full_df.empty

    from src.data.time_split import time_ordered_group_safe_split

    train_df, valid_df = time_ordered_group_safe_split(full_df, config["data"]["validation_ratio"])
    assert len(train_df) > 0

    ranker = LGBMRanker(params=config["lightgbm"]["params"], group_key=config["ranking"]["group_key"])
    valid_arg = valid_df if len(valid_df) > 0 else None
    ranker.train(train_df, valid_df=valid_arg)
    assert ranker.model is not None

    inference_df = dataset.create_inference_dataset(target_user_ids=None, eval_timestamp=pinned_now)
    assert len(inference_df) == len(bundle.users) * len(bundle.newsletters)
    scored_df = ranker.predict(inference_df)
    assert "score" in scored_df.columns

    reranker = create_reranker_from_config(config)
    news_dict = loader.load_embedded_news()
    pref_cats = loader.load_user_preferred_categories()
    user_cat_counts = pref_cats.groupby("user_id")["category_id"].count().to_dict()

    recommendations = {}
    for uid, group in scored_df.groupby("user_id"):
        group = group.sort_values("score", ascending=False)
        nids = [n for n in group["news_id"] if n in news_dict]
        filtered = group[group["news_id"].isin(nids)]
        embeddings = np.array([news_dict[n].embedding for n in nids])
        selected = reranker.rerank_for_user(
            scores=filtered["score"].values,
            embeddings=embeddings,
            top_k=config["recommendation"]["top_k"],
            num_preferred_categories=user_cat_counts.get(uid, 0),
        )
        recommendations[int(uid)] = [nids[i] for i, _ in selected]

    assert len(recommendations) == len(bundle.users)
    for uid, recs in recommendations.items():
        assert len(recs) <= config["recommendation"]["top_k"]
        assert len(recs) == len(set(recs))  # MMR 결과에 중복이 없어야 함

    clicks = bundle.ctr_logs[bundle.ctr_logs["is_clicked"] == 1]
    ground_truth = clicks.groupby("user_id")["news_letter_id"].apply(set).to_dict()
    category_map = FL.category_ids_by_newsletter(bundle)
    evaluator = Evaluator(k_values=[5])
    per_user = M.per_user_metrics(evaluator, recommendations, ground_truth, category_map)
    agg = M.aggregate(per_user)
    # 유저가 하나라도 평가됐다면 mrr은 항상 [0,1] 구간
    if agg:
        assert 0.0 <= agg["mrr"] <= 1.0
