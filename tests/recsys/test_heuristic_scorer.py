from datetime import timedelta

import numpy as np
import pytest

from app.recsys.scoring import HeuristicScorer, HeuristicWeights
from app.recsys.types import Item, UserState
from tests.recsys.fakes import NOW, axis_vec

DIM = 8


def _item(i, axis, age_h=1.0, count=1):
    return Item(i, axis_vec(DIM, axis), NOW - timedelta(hours=age_h), count)


def test_short_term_click_signal_moves_similar_items_up():
    items = [_item(1, 0), _item(2, 1)]
    scorer = HeuristicScorer()
    base = UserState(user_id=1, profile=axis_vec(DIM, 0))
    clicked_b = UserState(user_id=1, profile=axis_vec(DIM, 0), short_term=axis_vec(DIM, 1))

    before = scorer.score(base, items, NOW).scores
    after = scorer.score(clicked_b, items, NOW).scores

    assert before[0] > before[1]
    assert (after[1] - after[0]) > (before[1] - before[0])


def test_missing_short_term_gives_its_weight_to_the_profile():
    w = HeuristicWeights(long_term=0.5, short_term=0.3, recency=0.15, popularity=0.05)
    items = [_item(1, 0, age_h=0.0, count=0)]
    only_profile = UserState(user_id=1, profile=axis_vec(DIM, 0))

    score = HeuristicScorer(w).score(only_profile, items, NOW).scores[0]

    # cos=1 에 (0.5+0.3), 신선도 exp(0)=1 에 0.15, 인기 log1p(0)=0
    assert score == pytest.approx(0.8 + 0.15, abs=1e-6)


def test_recency_and_popularity_break_ties_between_equally_similar_items():
    items = [_item(1, 0, age_h=60), _item(2, 0, age_h=1), _item(3, 0, age_h=60, count=40)]
    state = UserState(user_id=1, profile=axis_vec(DIM, 0))

    scores = HeuristicScorer().score(state, items, NOW).scores

    assert scores[1] > scores[0]
    assert scores[2] > scores[0]


def test_version_is_reported():
    state = UserState(user_id=1, profile=axis_vec(DIM, 0))
    result = HeuristicScorer().score(state, [_item(1, 0)], NOW)
    assert result.model_version == "heuristic-v1"
    assert isinstance(result.scores, np.ndarray)
