"""활성 휴리스틱 heuristic-v2 (ADR 0014 사전 등록 1·2·3·5)와 되돌리기용 heuristic-v1."""
import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from app.recsys.scoring import (
    FEATURE_SCHEMAS,
    HEURISTIC_FEATURE_SCHEMA,
    HEURISTIC_SETS,
    PRIOR_HEURISTIC_FEATURE_SCHEMA,
    WEIGHTS_A,
    WEIGHTS_B,
    HeuristicScorer,
    HeuristicWeights,
    PriorHeuristicScorer,
    PriorHeuristicWeights,
    hours_since_pub,
    make_heuristic,
    popularity_order,
)
from app.recsys.types import Item, NewsletterMeta, UserState
from tests.recsys.fakes import NOW, axis_vec

DIM = 8
REPORT = Path(__file__).resolve().parents[2] / "reports" / "recsys" / "ebnerd_v1_2_cold.json"


def _item(i, axis, age_h=1.0, count=1):
    return Item(i, axis_vec(DIM, axis), NOW - timedelta(hours=age_h), count)


# ------------------------------------------------------------------ 가중치 (사전 등록 1)
@pytest.mark.parametrize("name, weights", [("b", WEIGHTS_B), ("a", WEIGHTS_A)])
def test_serving_weights_are_the_mean_of_the_three_seed_fits_in_the_m4_report(name, weights):
    fits = json.loads(REPORT.read_text(encoding="utf-8"))["e7"]["weights"][name]
    assert sorted(fits) == ["0", "1", "2"]
    for fit in fits.values():
        assert fit["terms"] == ["hist_cos", "short_cos", "recency", "log1p_pop_clicks_6h"] and fit["converged"]
    mean = np.mean([fits[s]["weights"] for s in ("0", "1", "2")], axis=0)
    got = [weights.long_term, weights.short_term, weights.recency, weights.popularity]
    np.testing.assert_allclose(got, mean, rtol=0, atol=1e-12)
    assert weights.recency_tau_hours == 48.0


def test_the_default_set_is_b_and_the_version_names_the_set():
    assert HeuristicWeights() == WEIGHTS_B == HEURISTIC_SETS["b"]
    assert HeuristicScorer().version == "heuristic-v2-b"
    assert HeuristicScorer(set_name="a").version == "heuristic-v2-a"
    assert HeuristicScorer(WEIGHTS_A).version == "heuristic-v2-a"  # 같은 값이면 세트 이름을 찾는다
    assert HeuristicScorer(HeuristicWeights(long_term=1.0)).version == "heuristic-v2-custom"
    assert make_heuristic("prior").version == "heuristic-v1"
    with pytest.raises(ValueError):
        HeuristicScorer(set_name="c")


# ------------------------------------------------------------------ 점수 식 (사전 등록 2)
def test_the_score_is_the_weighted_sum_of_the_four_terms_without_moving_weights():
    w = WEIGHTS_B
    items = [_item(1, 0, age_h=2.0), _item(2, 1, age_h=30.0), _item(3, 2, age_h=0.0)]
    state = UserState(user_id=1, profile=axis_vec(DIM, 0), short_term=axis_vec(DIM, 1), clicks_6h={1: 3, 3: 0})

    result = HeuristicScorer().score(state, items, NOW)

    h = hours_since_pub([it.created_at for it in items], NOW).astype(np.float64)
    cos_long = np.array([1.0, 0.0, 0.0])
    cos_short = np.array([0.0, 1.0, 0.0])
    clicks = np.array([3.0, 0.0, 0.0])
    want = w.long_term * cos_long + w.short_term * cos_short + w.recency * np.exp(-h / 48) + w.popularity * np.log1p(clicks)
    np.testing.assert_allclose(result.scores, want, atol=1e-6)
    assert result.feature_schema_version == HEURISTIC_FEATURE_SCHEMA == 3
    assert FEATURE_SCHEMAS[HEURISTIC_FEATURE_SCHEMA] == ("cos_long", "cos_short", "recency", "log1p_clicks_6h")
    np.testing.assert_allclose(result.features, np.column_stack([cos_long, cos_short, np.exp(-h / 48), np.log1p(clicks)]),
                               atol=1e-6)


def test_a_missing_signal_contributes_zero_and_is_logged_as_nan():
    """하네스는 히스토리·단기 이벤트가 없으면 hist_cos = short_cos = 0이고, 적합한 식에는 가중치를 넘기는 규칙이 없다."""
    w = WEIGHTS_B
    items = [_item(1, 0, age_h=0.0)]
    only_profile = UserState(user_id=1, profile=axis_vec(DIM, 0), clicks_6h={})

    result = HeuristicScorer().score(only_profile, items, NOW)

    # T = floor(now) + 1이라 방금 만든 뉴스레터의 경과 시간은 1초다(어댑터와 같은 규칙)
    recency = np.exp(-(1 / 3600) / 48)
    assert result.scores[0] == pytest.approx(w.long_term * 1.0 + w.recency * recency, abs=1e-6)
    cos_long, cos_short, rec, log_clicks = result.features[0]
    assert np.isnan(cos_short) and cos_long == pytest.approx(1.0) and log_clicks == 0.0


def test_unread_click_counts_drop_the_popularity_term_for_every_candidate():
    items = [_item(1, 0, age_h=5), _item(2, 0, age_h=5)]
    read = UserState(user_id=1, profile=axis_vec(DIM, 0), clicks_6h={1: 10})
    unread = UserState(user_id=1, profile=axis_vec(DIM, 0), clicks_6h=None)

    with_counts = HeuristicScorer().score(read, items, NOW)
    without = HeuristicScorer().score(unread, items, NOW)

    assert with_counts.scores[0] > with_counts.scores[1]
    assert without.scores[0] == pytest.approx(without.scores[1])  # 인기 항이 모든 후보에서 0
    assert np.all(np.isnan(without.features[:, 3]))


def test_the_popularity_term_is_log1p_of_raw_clicks_and_ignores_article_counts():
    items = [_item(1, 0, age_h=5, count=1), _item(2, 0, age_h=5, count=10_000)]
    state = UserState(user_id=1, profile=axis_vec(DIM, 0), clicks_6h={1: 1000, 2: 0})

    scores = HeuristicScorer().score(state, items, NOW).scores

    assert scores[0] - scores[1] == pytest.approx(WEIGHTS_B.popularity * np.log1p(1000), abs=1e-6)  # 상한 없음


def test_an_onboarding_profile_takes_the_long_term_slot():
    items = [_item(1, 0), _item(2, 1)]
    onboarded = UserState(user_id=1, profile=axis_vec(DIM, 1), profile_source="onboarding", clicks_6h={})

    scores = HeuristicScorer().score(onboarded, items, NOW).scores

    assert scores[1] - scores[0] == pytest.approx(WEIGHTS_B.long_term, abs=1e-5)


def test_hours_since_pub_follows_the_adapter_request_second():
    created = NOW - timedelta(hours=3, microseconds=250_000)
    now = NOW.replace(microsecond=900_000)
    # T = floor(now) + 1, 생성 시각은 초로 내림: (NOW + 1 s) - (NOW - 3 h - 1 s) = 3 h + 2 s
    assert hours_since_pub([created], now)[0] == pytest.approx(3 + 2 / 3600, abs=1e-6)
    assert hours_since_pub([NOW + timedelta(hours=1)], NOW)[0] == 0.0  # 미래는 0


def test_empty_candidates_score_to_empty_arrays():
    result = HeuristicScorer().score(UserState(user_id=1, profile=axis_vec(DIM, 0)), [], NOW)
    assert result.scores.shape == (0,) and result.features.shape == (0, 4)


# ------------------------------------------------------------------ 콜드 경로·popular 출처 (사전 등록 3·4)
def _meta(nid, age_h, raw=1):
    return NewsletterMeta(nid, NOW - timedelta(hours=age_h), raw)


def test_the_cold_order_uses_the_two_non_personal_terms_and_breaks_ties_by_recency_then_id():
    metas = [_meta(1, 10), _meta(2, 1), _meta(3, 10), _meta(4, 10, raw=500)]
    order = HeuristicScorer().cold_order(metas, {1: 5}, NOW)

    w = WEIGHTS_B
    h = hours_since_pub([m.news_letter_created_at for m in metas], NOW).astype(np.float64)
    want = w.recency * np.exp(-h / 48) + w.popularity * np.log1p(np.array([5, 0, 0, 0]))
    assert [nid for nid, _ in order] == [1, 2, 4, 3]  # 4와 3은 동점(기사 수는 쓰지 않는다): 생성 시각이 같아 ID 내림차순
    np.testing.assert_allclose([s for _, s in order], want[[0, 1, 3, 2]], atol=1e-9)


def test_the_popular_source_orders_by_six_hour_clicks_then_recency_then_id():
    metas = [_meta(1, 10), _meta(2, 1), _meta(3, 10), _meta(4, 50)]
    assert popularity_order(metas, {4: 2, 1: 1}) == [4, 1, 2, 3]
    assert popularity_order(metas, None) == [2, 3, 1, 4]  # 읽지 못하면 전부 0: 생성 시각 -> ID


# ------------------------------------------------------------------ 되돌리기 세트 (사전 등록 5)
def test_the_prior_set_is_heuristic_v1_unchanged():
    w = PriorHeuristicWeights()
    assert (w.long_term, w.short_term, w.recency, w.popularity) == (0.45, 0.35, 0.15, 0.05)
    items = [_item(1, 0, age_h=0.0, count=0)]
    only_profile = UserState(user_id=1, profile=axis_vec(DIM, 0))

    result = PriorHeuristicScorer().score(only_profile, items, NOW)

    # 단기 신호가 없으면 단기 가중치를 장기로 넘긴다: cos=1 에 (0.45+0.35), 신선도 exp(0)=1 에 0.15, 기사 수 log1p(0)=0
    assert result.scores[0] == pytest.approx(0.8 + 0.15, abs=1e-6)
    assert result.model_version == "heuristic-v1" and result.feature_schema_version == PRIOR_HEURISTIC_FEATURE_SCHEMA
    metas = [_meta(1, 1, raw=1), _meta(2, 1, raw=50)]
    assert [nid for nid, _ in PriorHeuristicScorer().cold_order(metas, None, NOW)] == [2, 1]  # compute_scores 순서
    assert PriorHeuristicScorer.cold_model_version == "popularity-v1"


def test_short_term_click_signal_moves_similar_items_up():
    items = [_item(1, 0), _item(2, 1)]
    scorer = HeuristicScorer()
    base = UserState(user_id=1, profile=axis_vec(DIM, 0), clicks_6h={})
    clicked_b = UserState(user_id=1, profile=axis_vec(DIM, 0), short_term=axis_vec(DIM, 1), clicks_6h={})

    before = scorer.score(base, items, NOW).scores
    after = scorer.score(clicked_b, items, NOW).scores

    assert before[0] > before[1]
    assert (after[1] - after[0]) > (before[1] - before[0])
