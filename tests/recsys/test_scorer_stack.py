"""ScorerStack: 활성 스코어러가 목록을 만들고 shadow는 점수만 남긴다 (ADR 0025).

shadow가 응답에 영향을 주지 않는다는 것을 여기서는 스코어 단계에서, 서비스 수준(목록이 같은지, 로그에
남는지)은 test_service_exploration_and_logs.py에서 본다.
"""
import logging
from datetime import timedelta

import numpy as np
import pytest

from app.recsys.deadline import Deadline
from app.recsys.metrics import RecsysCounters
from app.recsys.scoring import (
    FEATURE_SCHEMAS,
    HEURISTIC_FEATURE_SCHEMA,
    HeuristicScorer,
    HeuristicWeights,
    ScorerStack,
    ScorerUnavailable,
    decode_features,
    encode_features,
)
from app.recsys.types import Item, ScoreResult, UserState
from tests.recsys.fakes import NOW, axis_vec

DIM = 8


def _items(n=5):
    return [Item(i + 1, axis_vec(DIM, i % DIM), NOW - timedelta(hours=i), 1 + i) for i in range(n)]


def _state():
    return UserState(user_id=1, profile=axis_vec(DIM, 1))


class Fixed:
    """항상 같은 점수를 내는 스코어러."""

    def __init__(self, version, scores=None, raises=None, on_call=None):
        self.version, self.scores, self.raises, self.on_call = version, scores, raises, on_call
        self.calls = 0

    def score(self, state, items, now):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.raises:
            raise self.raises
        scores = np.arange(len(items), dtype=float) if self.scores is None else np.asarray(self.scores)
        return ScoreResult(scores=scores, model_version=self.version)


def test_active_scores_are_untouched_and_shadow_scores_are_keyed_by_model_version():
    active = HeuristicScorer()
    alone = active.score(_state(), _items(), NOW)

    stacked = ScorerStack(active, [Fixed("shadow-a"), Fixed("shadow-b", scores=[5, 4, 3, 2, 1])]).score(
        _state(), _items(), NOW
    )

    np.testing.assert_array_equal(stacked.scores, alone.scores)
    assert stacked.model_version == alone.model_version == "heuristic-v1"
    assert list(stacked.extra_scores) == ["shadow-a", "shadow-b"]
    assert stacked.extra_scores["shadow-b"].tolist() == [5, 4, 3, 2, 1]


def test_a_failing_shadow_is_swallowed_counted_and_does_not_stop_the_next_shadow(caplog):
    counters = RecsysCounters()
    stack = ScorerStack(
        HeuristicScorer(),
        [Fixed("broken", raises=RuntimeError("boom")), Fixed("unregistered", raises=ScorerUnavailable("no model")),
         Fixed("wrong-shape", scores=[1.0, 2.0]), Fixed("ok")],
        counters=counters,
    )

    with caplog.at_level(logging.ERROR, logger="app.recsys.scoring"):
        for _ in range(3):
            result = stack.score(_state(), _items(), NOW)

    assert list(result.extra_scores) == ["ok"]
    assert counters.get("shadow.error") == 6  # broken + wrong-shape, 세 번
    assert counters.get("shadow.unavailable") == 3  # 모델이 없는 것은 오류로 세지 않는다
    assert counters.get("shadow.scored") == 3
    # 요청마다 같은 예외가 나도 traceback은 shadow마다 한 번만 남는다
    assert len([r for r in caplog.records if r.exc_info]) == 2


def test_an_error_in_the_active_scorer_is_not_swallowed():
    stack = ScorerStack(Fixed("active", raises=RuntimeError("active down")), [Fixed("shadow")])

    with pytest.raises(RuntimeError, match="active down"):
        stack.score(_state(), _items(), NOW)


def test_shadows_are_skipped_when_less_than_the_deadline_fraction_of_the_budget_is_left():
    t = [0.0]
    counters = RecsysCounters()
    first, second = Fixed("s1", on_call=lambda: t.__setitem__(0, t[0] + 0.10)), Fixed("s2")
    stack = ScorerStack(HeuristicScorer(), [first, second], deadline_fraction=0.5, counters=counters)

    # 예산 300ms 중 100ms를 쓴 시점: 2/3가 남아 첫 shadow는 돈다. 그 shadow가 100ms를 써서
    # 1/3만 남으면 두 번째 shadow는 건너뛴다.
    deadline = Deadline(0.3, clock=lambda: t[0])
    t[0] = 0.10
    result = stack.score(_state(), _items(), NOW, deadline)

    assert list(result.extra_scores) == ["s1"]
    assert (first.calls, second.calls) == (1, 0)
    assert counters.get("shadow.skipped") == 1

    # 활성 점수를 낸 시점에 이미 절반 넘게 썼으면 shadow를 하나도 돌리지 않는다
    late = Deadline(0.3, clock=lambda: t[0])
    t[0] += 0.2
    assert ScorerStack(HeuristicScorer(), [first, second], counters=counters).score(
        _state(), _items(), NOW, late
    ).extra_scores == {}
    assert counters.get("shadow.skipped") == 3


def test_without_a_deadline_every_shadow_runs():
    shadows = [Fixed("s1"), Fixed("s2")]

    result = ScorerStack(HeuristicScorer(), shadows).score(_state(), _items(), NOW)

    assert list(result.extra_scores) == ["s1", "s2"]


def test_a_shadow_that_is_the_same_version_as_the_active_scorer_or_another_shadow_is_not_logged_twice():
    counters = RecsysCounters()
    stack = ScorerStack(
        Fixed("model-v1"), [Fixed("model-v1"), Fixed("model-v2"), Fixed("model-v2")], counters=counters
    )

    result = stack.score(_state(), _items(), NOW)

    assert list(result.extra_scores) == ["model-v2"]
    assert counters.get("shadow.duplicate") == 2


def test_the_heuristic_reports_the_four_terms_it_scored_with_and_they_reproduce_the_score():
    """칸 로그의 features는 가중치를 곱하기 전의 4항이다. 로그만으로 점수를 다시 계산할 수 있어야 한다."""
    w = HeuristicWeights()
    items = _items()
    long_only = UserState(user_id=1, profile=axis_vec(DIM, 1))
    both = UserState(user_id=1, profile=axis_vec(DIM, 1), short_term=axis_vec(DIM, 2))

    for state, w_long, w_short in ((long_only, w.long_term + w.short_term, 0.0), (both, w.long_term, w.short_term)):
        result = HeuristicScorer(w).score(state, items, NOW)
        assert result.feature_schema_version == HEURISTIC_FEATURE_SCHEMA
        assert result.features.shape == (len(items), len(FEATURE_SCHEMAS[HEURISTIC_FEATURE_SCHEMA]))
        cos_long, cos_short, recency, popularity = (result.features[:, j].astype(np.float64) for j in range(4))
        rebuilt = w_long * cos_long + w.recency * recency + w.popularity * popularity
        if w_short:
            rebuilt = rebuilt + w_short * cos_short
        else:
            assert np.all(np.isnan(cos_short))  # 없는 신호는 0이 아니라 NaN으로 남는다
        np.testing.assert_allclose(rebuilt, result.scores, atol=1e-6)


def test_feature_payload_round_trips_as_little_endian_float32():
    row = np.array([0.25, np.nan, 0.5, 1.0], dtype=np.float32)

    payload = encode_features(row)

    assert len(payload) == 16
    np.testing.assert_array_equal(decode_features(payload), row)
