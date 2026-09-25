from datetime import timedelta

import lightgbm as lgb
import numpy as np
import pytest

from app.recsys.lgbm_scorer import LightGBMScorer, RegisteredModel, resolve_feature_fn
from app.recsys.metrics import RecsysCounters
from app.recsys.scoring import HeuristicScorer
from app.recsys.types import Item, UserState
from tests.recsys.fakes import NOW, axis_vec

DIM = 8


def _train_text(sign: float, seed: int = 0) -> str:
    """피처 0이 클수록(sign=+1) 또는 작을수록(sign=-1) 점수가 높은 2-피처 모델."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(400, 2))
    y = (sign * X[:, 0] > 0).astype(int)
    booster = lgb.train(
        {"objective": "binary", "verbose": -1, "num_leaves": 4, "seed": seed},
        lgb.Dataset(X, y),
        num_boost_round=20,
    )
    return booster.model_to_string()


class FakeSource:
    def __init__(self):
        self.models = {}
        self.active = None
        self.version_calls = 0
        self.fail = False

    def publish(self, version, text, feature_names=None, activate=True):
        self.models[version] = RegisteredModel("ranker", version, text, feature_names)
        if activate:
            self.active = version

    def active_version(self, name):
        self.version_calls += 1
        if self.fail:
            raise RuntimeError("registry down")
        return self.active

    def load(self, name, version):
        return self.models.get(version)


def feature0_is_axis0_cosine(state, items, now):
    return np.array([[float(it.embedding[0]), 0.0] for it in items])


feature0_is_axis0_cosine.feature_names = ["cos_axis0", "zero"]


def _items():
    return [Item(1, axis_vec(DIM, 0), NOW - timedelta(hours=1), 1), Item(2, axis_vec(DIM, 1), NOW, 1)]


def _state():
    return UserState(user_id=1, profile=axis_vec(DIM, 1))


def _scorer(source, feature_fn=feature0_is_axis0_cosine, clock=None, counters=None):
    return LightGBMScorer(
        source,
        fallback=HeuristicScorer(),
        feature_fn=feature_fn,
        reload_interval_s=60,
        clock=clock or (lambda: 0.0),
        counters=counters,
    )


def test_without_an_active_model_the_heuristic_is_used():
    result = _scorer(FakeSource()).score(_state(), _items(), NOW)
    assert result.model_version == "heuristic-v1"


def test_active_model_scores_with_the_injected_feature_function():
    source = FakeSource()
    source.publish("v1", _train_text(+1), ["cos_axis0", "zero"])

    result = _scorer(source).score(_state(), _items(), NOW)

    assert result.model_version == "lgbm:ranker@v1"
    # 휴리스틱이면 프로필(축 1)과 같은 항목 2가 이기지만, 모델은 피처 0(축 0)을 선호한다
    assert result.scores[0] > result.scores[1]


def test_model_is_hot_reloaded_only_after_the_interval():
    source = FakeSource()
    source.publish("v1", _train_text(+1))
    t = [0.0]
    scorer = _scorer(source, clock=lambda: t[0])
    scorer.score(_state(), _items(), NOW)

    source.publish("v2", _train_text(-1, seed=1))
    t[0] = 59.0
    assert scorer.score(_state(), _items(), NOW).model_version == "lgbm:ranker@v1"
    t[0] = 60.0
    reloaded = scorer.score(_state(), _items(), NOW)

    assert reloaded.model_version == "lgbm:ranker@v2"
    assert reloaded.scores[0] < reloaded.scores[1]
    assert source.version_calls == 2


def test_deactivating_the_model_returns_to_the_heuristic_after_reload():
    source = FakeSource()
    source.publish("v1", _train_text(+1))
    t = [0.0]
    scorer = _scorer(source, clock=lambda: t[0])
    scorer.score(_state(), _items(), NOW)

    source.active = None
    t[0] = 61.0

    assert scorer.score(_state(), _items(), NOW).model_version == "heuristic-v1"


def test_registry_failure_keeps_serving_the_current_model():
    source = FakeSource()
    source.publish("v1", _train_text(+1))
    t = [0.0]
    counters = RecsysCounters()
    scorer = _scorer(source, clock=lambda: t[0], counters=counters)
    scorer.score(_state(), _items(), NOW)

    source.fail = True
    t[0] = 61.0

    assert scorer.score(_state(), _items(), NOW).model_version == "lgbm:ranker@v1"
    assert counters.get("scorer.reload_error") == 1


@pytest.mark.parametrize(
    "bad_fn",
    [
        lambda s, items, now: (_ for _ in ()).throw(RuntimeError("feature bug")),
        lambda s, items, now: np.zeros((len(items), 5)),
    ],
    ids=["raises", "wrong-width"],
)
def test_feature_errors_fall_back_to_the_heuristic_and_are_counted(bad_fn):
    source = FakeSource()
    source.publish("v1", _train_text(+1))
    counters = RecsysCounters()

    result = _scorer(source, feature_fn=bad_fn, counters=counters).score(_state(), _items(), NOW)

    assert result.model_version == "heuristic-v1"
    assert counters.get("scorer.lgbm_error") == 1


def test_feature_name_mismatch_with_registry_disables_the_model():
    source = FakeSource()
    source.publish("v1", _train_text(+1), feature_names=["something_else", "zero"])
    counters = RecsysCounters()

    result = _scorer(source, counters=counters).score(_state(), _items(), NOW)

    assert result.model_version == "heuristic-v1"
    assert counters.get("scorer.feature_mismatch") == 1


def test_without_a_feature_function_the_heuristic_is_used_even_if_a_model_exists():
    source = FakeSource()
    source.publish("v1", _train_text(+1))

    assert _scorer(source, feature_fn=None).score(_state(), _items(), NOW).model_version == "heuristic-v1"


def test_feature_function_is_resolved_from_a_dotted_path():
    from app.recsys import scoring

    assert resolve_feature_fn("app.recsys.scoring:item_ages_hours") is scoring.item_ages_hours
    assert resolve_feature_fn(None) is None
    assert resolve_feature_fn("no_such_module_xyz:fn") is None


def test_registry_is_not_polled_when_no_feature_function_is_injected():
    source = FakeSource()
    source.publish("v1", _train_text(+1))

    _scorer(source, feature_fn=None).score(_state(), _items(), NOW)

    assert source.version_calls == 0


def test_default_runtime_wires_the_lgbm_adapter_with_env_config(monkeypatch):
    from app.recsys import runtime
    from app.recsys.lgbm_scorer import LightGBMScorer

    monkeypatch.setenv("RECSYS_MODE", "batch")
    monkeypatch.setenv("RECSYS_TIME_BUDGET_MS", "250")
    service = runtime._build_default_service()
    try:
        assert service.cfg.mode == "batch"
        assert service.cfg.time_budget_ms == 250
        assert isinstance(service.recommender.scorer, LightGBMScorer)
        assert service.recommender.scorer.counters is service.counters
    finally:
        service.shutdown()
