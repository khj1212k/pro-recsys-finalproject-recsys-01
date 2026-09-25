"""recsys_core(벡터화)와 팀 FeatureEngineer(행 단위 루프)의 겹치는 피처가 같은 값을 내는지.

팀 쪽 감쇠식(경과 일수 내림 + min_weight 하한)을 FeatureConfig(floor_days=True,
min_weight=0.01)로 맞추고, 비정수 일수/하한에 걸리는 오래된 클릭/미래 클릭/빈 히스토리를
일부러 섞은 합성 데이터로 비교한다.
"""
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from recsys_core import EventIndex, FeatureConfig, FeatureContext, ItemCatalog, Requests, compute_features  # noqa: E402

EPOCH = datetime(1970, 1, 1)


def _secs(dt):
    return int((dt - EPOCH).total_seconds())


def _build(seed=0, n_users=6, n_news=12, n_clicks=40):
    from src.data.data_loader import DataLoader, NewsItem, UserProfile
    from src.features.feature_engineer import FeatureEngineer

    rng = np.random.RandomState(seed)
    base = datetime(2026, 1, 1)
    emb = rng.randn(n_news, 1024).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    pub = [base + timedelta(minutes=int(rng.randint(0, 60 * 24 * 40))) for _ in range(n_news)]
    cats = rng.randint(0, 4, size=n_news)
    news_dict = {
        nid: NewsItem(news_id=nid, title="", content="", category_ids=[int(cats[nid])],
                      embedding=emb[nid], timestamp=pub[nid])
        for nid in range(n_news)
    }
    # 0번 유저는 클릭이 전혀 없는 콜드 유저.
    logs = pd.DataFrame({
        "user_id": rng.randint(1, n_users, size=n_clicks),
        "news_letter_id": rng.randint(0, n_news, size=n_clicks),
        "timestamp": [base + timedelta(minutes=int(m)) for m in rng.randint(0, 60 * 24 * 60, size=n_clicks)],
    })
    onboarding = {u: sorted(set(rng.randint(0, 4, size=rng.randint(0, 3)).tolist())) for u in range(n_users)}

    loader = DataLoader.__new__(DataLoader)
    loader.config = {"time_decay": {"news_half_life_days": 7, "min_weight": 0.01}}
    loader._news_dict = news_dict
    loader._user_profiles = None
    fe = FeatureEngineer.__new__(FeatureEngineer)
    fe.data_loader = loader
    fe.config = loader.config
    fe.news_dict = news_dict
    fe.logs_df = logs
    fe.user_profiles = {
        u: UserProfile(user_id=u, age_band_idx=0, gender_idx=0, onboarding_categories=onboarding[u])
        for u in range(n_users)
    }

    catalog = ItemCatalog(ids=np.arange(n_news), emb=emb, pub_time=[_secs(t) for t in pub],
                          category=cats, n_categories=4)
    static = np.zeros((n_users, 4), dtype=bool)
    for u, cs in onboarding.items():
        static[u, cs] = True
    ctx = FeatureContext(
        catalog=catalog,
        user_log=EventIndex(logs.user_id, [_secs(t) for t in logs.timestamp], logs.news_letter_id),
        static_categories=(np.arange(n_users), static),
        config=FeatureConfig(half_life_days=7, floor_days=True, min_weight=0.01),
    )
    return fe, ctx, base, rng


def test_overlapping_features_match_team_feature_engineer():
    fe, ctx, base, rng = _build()
    n_pairs = 60
    users = rng.randint(0, 6, size=n_pairs)
    news = rng.randint(0, 12, size=n_pairs)
    times = [base + timedelta(minutes=int(m)) for m in rng.randint(0, 60 * 24 * 70, size=n_pairs)]

    team = fe.create_features(user_ids=users.tolist(), news_ids=news.tolist(),
                              labels=[0] * n_pairs, timestamps=times)
    assert len(team) == n_pairs

    req = Requests(user=users, time=[_secs(t) for t in times],
                   cand_ptr=np.arange(n_pairs + 1), cand_item=news)
    ours = compute_features(ctx, req, groups=["recency", "history", "team_category"])

    np.testing.assert_allclose(ours["hours_since_pub"], team["hours_since_published"], rtol=1e-6, atol=1e-4)
    np.testing.assert_array_equal(ours["is_fresh_24h"], team["is_fresh_24h"])
    np.testing.assert_array_equal(ours["is_fresh_7d"], team["is_fresh_7d"])
    np.testing.assert_allclose(ours["hist_cos"], team["history_cosine_similarity"], atol=1e-5)
    np.testing.assert_array_equal(ours["cat_match_count"], team["category_match_count"])
    np.testing.assert_array_equal(ours["is_cat_match"], team["is_category_match"])
    np.testing.assert_array_equal(ours["user_ncat"], team["user_onboarding_cnt"])
    np.testing.assert_array_equal(ours["news_category"], team["news_category_repr"])
    # 비교가 공허하지 않도록 히스토리가 있는 행과 빈 행(콜드/첫 클릭 이전)이 모두 있어야 한다.
    assert (ours["hist_len"] > 0).sum() >= 10
    assert (ours["hist_len"] == 0).sum() >= 1
