"""4항 휴리스틱의 사전 가중치 점수와 쌍별 조건부 로지스틱 적합(E7)."""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from evaluation.recsys.ebnerd.heuristic_fit import (
    TERMS,
    fit_pairwise_logistic,
    fitted_scores,
    heuristic_terms,
    prior_scores,
)

REPO = Path(__file__).resolve().parents[2]
PREREG = yaml.safe_load((REPO / "evaluation/recsys/ebnerd/preregistration/cold-v1.2.yaml").read_text())
PRIOR = PREREG["heuristics"]["prior_weights"]


def _choice_data(w_true, n_groups=4000, size=21, seed=0):
    """다항 로짓 선택 모형: 요청마다 후보 size개 중 softmax(w·x)로 정답 1개."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal((n_groups * size, len(w_true)))
    util = (x @ np.asarray(w_true)).reshape(n_groups, size) + rng.gumbel(size=(n_groups, size))
    labels = np.zeros((n_groups, size), dtype=bool)
    labels[np.arange(n_groups), util.argmax(axis=1)] = True
    return x, labels.ravel(), np.arange(0, n_groups * size + 1, size)


def test_fit_recovers_the_weights_of_a_multinomial_logit_choice_model():
    w_true = [1.5, 0.7, -0.4, 0.2]
    x, labels, ptr = _choice_data(w_true)
    res = fit_pairwise_logistic(x, labels, ptr)
    assert res.converged and res.n_groups == 4000 and res.n_pairs == 4000 * 20
    np.testing.assert_allclose(res.weights, w_true, atol=0.08)
    again = fit_pairwise_logistic(x, labels, ptr)
    assert np.array_equal(res.weights, again.weights)  # 결정론


def test_each_request_carries_equal_weight_regardless_of_pool_size():
    """어떤 요청의 네거티브를 통째로 두 번 넣어도(쌍 수 2배) 목적함수와 적합 결과가 바뀌지 않는다."""
    x, labels, ptr = _choice_data([1.0, -0.5, 0.3, 0.0], n_groups=300, size=6, seed=1)
    base = fit_pairwise_logistic(x, labels, ptr)
    xs, ls, sizes = [], [], []
    for g in range(300):
        sl = slice(ptr[g], ptr[g + 1])
        xg, lg = x[sl], labels[sl]
        if g % 2 == 0:
            xg, lg = np.vstack([xg, xg[~lg]]), np.concatenate([lg, np.zeros((~lg).sum(), bool)])
        xs.append(xg), ls.append(lg), sizes.append(len(lg))
    dup = fit_pairwise_logistic(np.vstack(xs), np.concatenate(ls), np.concatenate([[0], np.cumsum(sizes)]))
    np.testing.assert_allclose(dup.weights, base.weights, atol=1e-8)
    assert dup.n_pairs > base.n_pairs and dup.n_groups == base.n_groups


def test_requests_without_a_positive_or_without_a_negative_are_ignored():
    x, labels, ptr = _choice_data([1.0, 0.0, 0.0, 0.0], n_groups=200, size=5, seed=2)
    base = fit_pairwise_logistic(x, labels, ptr)
    extra_x = np.vstack([x, np.full((5, 4), 9.0), np.full((3, 4), -9.0)])
    extra_l = np.concatenate([labels, np.zeros(5, bool), np.ones(3, bool)])   # 정답 없는 요청, 네거티브 없는 요청
    extra_ptr = np.concatenate([ptr, [ptr[-1] + 5, ptr[-1] + 8]])
    res = fit_pairwise_logistic(extra_x, extra_l, extra_ptr)
    assert res.n_groups == 200
    np.testing.assert_allclose(res.weights, base.weights, atol=1e-10)
    empty = fit_pairwise_logistic(np.zeros((4, 4)), np.zeros(4, bool), np.array([0, 4]))
    assert empty.n_groups == 0 and not empty.converged and np.all(empty.weights == 0)


def test_group_level_offsets_do_not_change_the_fit():
    """요청 안 모든 후보에 같은 값을 더해도(요청 수준 효과) 차이 벡터가 같으므로 결과가 같다."""
    x, labels, ptr = _choice_data([0.8, 0.4, 0.0, -0.3], n_groups=300, size=6, seed=3)
    shift = np.repeat(np.random.default_rng(0).normal(size=(300, 4)) * 5, 6, axis=0)
    a, b = fit_pairwise_logistic(x, labels, ptr), fit_pairwise_logistic(x + shift, labels, ptr)
    np.testing.assert_allclose(a.weights, b.weights, atol=1e-7)


def _feats(**cols):
    base = {"hist_cos": [0.6], "short_cos": [0.2], "hours_since_pub": [24.0], "pop_clicks_6h": [np.e ** 2 - 1],
            "hist_len": [10.0], "short_len": [3.0]}
    base.update(cols)
    return pd.DataFrame({k: np.asarray(v, dtype=np.float32) for k, v in base.items()})


def test_prior_scores_follow_the_serving_formula_and_weight_transfer():
    rec = 0.15 * np.exp(-24 / 48)
    both = prior_scores(_feats(), PRIOR, with_popularity=False)[0]
    assert both == pytest.approx(0.45 * 0.6 + 0.35 * 0.2 + rec, rel=1e-6)
    no_short = prior_scores(_feats(short_len=[0.0], short_cos=[0.0]), PRIOR, with_popularity=False)[0]
    assert no_short == pytest.approx(0.80 * 0.6 + rec, rel=1e-6)          # 단기 없음: 0.35가 장기로
    no_long = prior_scores(_feats(hist_len=[0.0], hist_cos=[0.0]), PRIOR, with_popularity=False)[0]
    assert no_long == pytest.approx(0.80 * 0.2 + rec, rel=1e-6)           # 장기 없음: 0.45가 단기로
    cold = prior_scores(_feats(hist_len=[0.0], hist_cos=[0.0], short_len=[0.0], short_cos=[0.0]), PRIOR,
                        with_popularity=False)[0]
    assert cold == pytest.approx(rec, rel=1e-6)
    # 인기 항: 0.05 x min(1, log1p(x)/5). log1p(e^2 - 1) = 2 -> 0.4, 아주 큰 값은 1에서 포화
    with_pop = prior_scores(_feats(), PRIOR, with_popularity=True)[0]
    assert with_pop - both == pytest.approx(0.05 * 0.4, rel=1e-4)
    huge = prior_scores(_feats(pop_clicks_6h=[1e9]), PRIOR, with_popularity=True)[0]
    assert huge - both == pytest.approx(0.05, rel=1e-6)


def test_registered_prior_weights_equal_the_serving_defaults():
    backend = str(REPO / "backend")
    if backend not in sys.path:
        sys.path.insert(0, backend)
    os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/testdb")
    scoring = pytest.importorskip("app.recsys.scoring")
    w = scoring.HeuristicWeights()
    assert PRIOR == {"long_term": w.long_term, "short_term": w.short_term, "recency": w.recency,
                     "popularity": w.popularity}
    assert PREREG["heuristics"]["recency_tau_hours"] == w.recency_tau_hours


def test_terms_and_fitted_scores_use_the_registered_four_terms():
    f = _feats(hist_cos=[0.6, 0.1], short_cos=[0.2, 0.3], hours_since_pub=[24.0, 0.0],
               pop_clicks_6h=[np.e ** 2 - 1, 0.0], hist_len=[1.0, 1.0], short_len=[1.0, 1.0])
    terms = heuristic_terms(f)
    assert TERMS == ("hist_cos", "short_cos", "recency", "log1p_pop_clicks_6h")
    np.testing.assert_allclose(terms, [[0.6, 0.2, np.exp(-0.5), 2.0], [0.1, 0.3, 1.0, 0.0]], rtol=1e-5)
    np.testing.assert_allclose(fitted_scores(f, [1.0, 2.0, 3.0, 4.0]), terms @ [1.0, 2.0, 3.0, 4.0])
