"""baselines.py 테스트: 작은 합성 데이터로 각 베이스라인의 랭킹 로직을 손으로 검증한다
(팀 아카이브에 의존하지 않음 - 순수 로직 테스트)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "evaluation" / "recsys" / "team_repro"))

import baselines as BL  # noqa: E402


def test_random_baseline_returns_top_k_permutation_of_candidates():
    out = BL.random_baseline(user_ids=[1, 2], candidate_ids=[10, 20, 30, 40], top_k=2, seed=0)
    assert set(out.keys()) == {1, 2}
    for uid, recs in out.items():
        assert len(recs) == 2
        assert set(recs) <= {10, 20, 30, 40}
        assert len(set(recs)) == 2


def test_random_baseline_is_deterministic_given_seed():
    a = BL.random_baseline([1, 2, 3], [1, 2, 3, 4, 5], top_k=3, seed=7)
    b = BL.random_baseline([1, 2, 3], [1, 2, 3, 4, 5], top_k=3, seed=7)
    assert a == b


def test_popularity_baseline_ranks_by_train_click_count_desc():
    counts = {10: 5, 20: 1, 30: 9}
    out = BL.popularity_baseline(user_ids=[1, 2], candidate_ids=[10, 20, 30], top_k=3, train_click_counts=counts)
    assert out[1] == [30, 10, 20]
    assert out[1] == out[2]  # 유저 무관 동일 랭킹


def test_popularity_baseline_missing_items_treated_as_zero_count():
    counts = {10: 1}
    out = BL.popularity_baseline(user_ids=[1], candidate_ids=[10, 20], top_k=2, train_click_counts=counts)
    assert out[1][0] == 10  # count=1 > count=0(20)


def test_recency_baseline_prefers_most_recently_published():
    now = datetime(2026, 2, 1)
    created = {10: now - timedelta(days=5), 20: now - timedelta(days=1), 30: now - timedelta(days=10)}
    out = BL.recency_baseline(user_ids=[1], candidate_ids=[10, 20, 30], top_k=3, created_at_by_id=created, cutoff=now)
    assert out[1] == [20, 10, 30]


def test_recency_baseline_excludes_future_items_to_the_end():
    now = datetime(2026, 2, 1)
    created = {10: now - timedelta(days=1), 20: now + timedelta(days=1)}  # 20은 미래(존재하지 않아야 정상)
    out = BL.recency_baseline(user_ids=[1], candidate_ids=[10, 20], top_k=2, created_at_by_id=created, cutoff=now)
    assert out[1][0] == 10  # 미래 아이템이 뒤로 밀림


def test_category_match_baseline_ranks_by_overlap_count():
    cat_map = {10: [1, 2], 20: [1], 30: [3]}
    prefs = {1: {1, 2}}
    out = BL.category_match_baseline(
        user_ids=[1], candidate_ids=[10, 20, 30], top_k=3, user_pref_categories=prefs, category_map=cat_map
    )
    assert out[1][0] == 10  # 2개 매치
    assert out[1][1] == 20  # 1개 매치
    assert out[1][2] == 30  # 0개 매치


def test_category_match_baseline_user_with_no_preferences_falls_back_to_tiebreak_only():
    cat_map = {10: [1], 20: [2]}
    out = BL.category_match_baseline(
        user_ids=[1], candidate_ids=[10, 20], top_k=2, user_pref_categories={}, category_map=cat_map
    )
    assert set(out[1]) == {10, 20}


def test_history_embedding_uses_only_logs_before_cutoff():
    now = datetime(2026, 2, 1)
    logs = pd.DataFrame(
        {
            "user_id": [1, 1],
            "news_letter_id": [10, 20],
            "timestamp": [now - timedelta(days=1), now + timedelta(days=1)],  # 20은 cutoff 이후
        }
    )
    emb_by_id = {10: np.array([1.0, 0.0]), 20: np.array([0.0, 1.0])}
    result = BL.history_embedding(1, now, logs, emb_by_id, half_life_days=7, min_weight=0.01)
    assert result is not None
    np.testing.assert_allclose(result, [1.0, 0.0])


def test_history_embedding_returns_none_when_no_prior_logs():
    now = datetime(2026, 2, 1)
    logs = pd.DataFrame({"user_id": [], "news_letter_id": [], "timestamp": []})
    result = BL.history_embedding(1, now, logs, {}, half_life_days=7, min_weight=0.01)
    assert result is None


def test_cosine_history_baseline_ranks_candidates_by_similarity_to_history():
    now = datetime(2026, 2, 1)
    logs = pd.DataFrame({"user_id": [1], "news_letter_id": [10], "timestamp": [now - timedelta(days=1)]})
    emb_by_id = {
        10: np.array([1.0, 0.0]),
        20: np.array([1.0, 0.01]),  # 10과 매우 유사
        30: np.array([0.0, 1.0]),  # 10과 직교
    }
    out = BL.cosine_history_baseline(
        user_ids=[1], candidate_ids=[20, 30], top_k=2, logs_df=logs, emb_by_id=emb_by_id, cutoff=now
    )
    assert out[1][0] == 20  # 히스토리(10)와 더 유사한 20이 먼저


def test_onboarding_newsletter_similarity_baseline_uses_selected_newsletters():
    prefs = pd.DataFrame({"user_id": [1], "news_letter_id": [10]})
    emb_by_id = {10: np.array([1.0, 0.0]), 20: np.array([0.99, 0.01]), 30: np.array([0.0, 1.0])}
    out = BL.onboarding_newsletter_similarity_baseline(
        user_ids=[1], candidate_ids=[20, 30], top_k=2, preferred_newsletters=prefs, emb_by_id=emb_by_id
    )
    assert out[1][0] == 20
