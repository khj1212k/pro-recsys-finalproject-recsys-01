import threading
import time
from datetime import timedelta

import lightgbm as lgb
import numpy as np
import pytest

from app.recsys.lgbm_scorer import LightGBMScorer, RegisteredModel, resolve_feature_fn
from app.recsys.metrics import RecsysCounters
from app.recsys.scoring import HeuristicScorer, ScorerStack, ScorerUnavailable
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
        self.shadows = []  # 최신 등록순
        self.version_calls = 0
        self.fail = False

    def publish(self, version, text, feature_names=None, activate=True):
        self.models[version] = RegisteredModel("ranker", version, text, feature_names)
        if activate:
            self.active = version

    def publish_shadow(self, version, text, feature_names=None):
        self.models[version] = RegisteredModel("ranker", version, text, feature_names)
        self.shadows.insert(0, version)

    def shadow_versions(self, name, limit):
        self.version_calls += 1
        if self.fail:
            raise RuntimeError("registry down")
        return self.shadows[:limit]

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


def test_the_longest_registry_name_and_version_fit_the_impression_log_column():
    """노출 로그의 model_version에는 "lgbm:<name>@<version>"이 들어간다. 레지스트리가 허용하는
    가장 긴 이름·버전으로 만든 값이 로그 컬럼에 들어가지 않으면, 학습 모델을 켜는 순간 모든
    노출 INSERT가 "value too long"으로 실패하고 로그가 조용히 끊긴다."""
    from app.models.recsys import ModelRegistry, RecommendationImpressionLog

    registry, log = ModelRegistry.__table__.c, RecommendationImpressionLog.__table__.c
    name = "n" * registry.model_name.type.length
    version = "v" * registry.model_version.type.length
    source = FakeSource()
    source.models[version] = RegisteredModel(name, version, _train_text(+1), ["cos_axis0", "zero"])
    source.active = version
    scorer = LightGBMScorer(
        source, fallback=HeuristicScorer(), feature_fn=feature0_is_axis0_cosine,
        model_name=name, clock=lambda: 0.0,
    )

    emitted = scorer.score(_state(), _items(), NOW).model_version

    assert emitted == f"lgbm:{name}@{version}"
    assert len(emitted) <= log.model_version.type.length


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


class BlockingSource(FakeSource):
    """레지스트리 조회가 멈춘 상황(앱이 바빠 커넥션을 못 빌리거나 DB가 느린 경우)."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def active_version(self, name):
        self.entered.set()
        assert self.release.wait(5)
        return super().active_version(name)


def test_background_reload_never_makes_a_request_wait_for_the_registry():
    source = BlockingSource()
    source.publish("v1", _train_text(+1))
    scorer = LightGBMScorer(
        source, fallback=HeuristicScorer(), feature_fn=feature0_is_axis0_cosine,
        clock=lambda: 0.0, reload_in_background=True,
    )

    started = time.perf_counter()
    while_blocked = scorer.score(_state(), _items(), NOW)
    elapsed = time.perf_counter() - started

    try:
        assert source.entered.wait(2)
        assert while_blocked.model_version == "heuristic-v1"
        assert elapsed < 0.5
    finally:
        source.release.set()

    deadline = time.monotonic() + 5
    while scorer._current is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert scorer.score(_state(), _items(), NOW).model_version == "lgbm:ranker@v1"
    assert source.version_calls == 1


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
        stack = service.recommender.stack
        assert isinstance(stack, ScorerStack)
        assert isinstance(stack.active, LightGBMScorer) and stack.active.role == "active"
        assert stack.active.counters is service.counters and stack.counters is service.counters
        # 피처 함수가 없으면 어떤 등록 모델도 점수를 낼 수 없다: shadow를 만들지 않는다
        assert stack.shadows == []
    finally:
        service.shutdown()


# ----------------------------------------------------------------------------- shadow 역할 (ADR 0025)


def _shadow(source, slot=0, feature_fn=feature0_is_axis0_cosine, counters=None):
    return LightGBMScorer(
        source, fallback=None, feature_fn=feature_fn, clock=lambda: 0.0, counters=counters,
        role="shadow", slot=slot,
    )


def test_a_shadow_scorer_never_substitutes_the_heuristic_for_a_missing_model():
    """shadow가 모델 없이 휴리스틱 점수를 자기 이름으로 내면 로그가 거짓이 된다."""
    source = FakeSource()
    source.publish("active-v1", _train_text(+1))  # 활성 모델이 있어도 shadow 행이 없으면 점수 없음

    with pytest.raises(ScorerUnavailable):
        _shadow(source).score(_state(), _items(), NOW)
    with pytest.raises(ScorerUnavailable):
        _shadow(FakeSource(), feature_fn=None).score(_state(), _items(), NOW)


def test_shadow_slots_read_the_newest_shadow_models_in_registration_order():
    source = FakeSource()
    source.publish_shadow("s-old", _train_text(+1), ["cos_axis0", "zero"])
    source.publish_shadow("s-new", _train_text(-1, seed=1), ["cos_axis0", "zero"])

    newest = _shadow(source, slot=0).score(_state(), _items(), NOW)
    second = _shadow(source, slot=1).score(_state(), _items(), NOW)

    assert newest.model_version == "lgbm:ranker@s-new"
    assert second.model_version == "lgbm:ranker@s-old"
    assert newest.scores[0] < newest.scores[1] and second.scores[0] > second.scores[1]
    with pytest.raises(ScorerUnavailable):  # 세 번째 shadow는 등록돼 있지 않다
        _shadow(source, slot=2).score(_state(), _items(), NOW)


def test_a_shadow_with_mismatched_features_or_a_failing_feature_function_gives_no_score():
    counters = RecsysCounters()
    source = FakeSource()
    source.publish_shadow("s1", _train_text(+1), ["other", "names"])
    with pytest.raises(ScorerUnavailable):
        _shadow(source, counters=counters).score(_state(), _items(), NOW)
    assert counters.get("scorer.feature_mismatch") == 1

    def broken(state, items, now):
        raise RuntimeError("feature store down")

    source = FakeSource()
    source.publish_shadow("s1", _train_text(+1))
    with pytest.raises(RuntimeError, match="feature store down"):
        _shadow(source, feature_fn=broken, counters=counters).score(_state(), _items(), NOW)
    assert counters.get("scorer.lgbm_error") == 1


def test_the_stack_logs_registered_shadow_models_next_to_the_active_heuristic():
    source = FakeSource()
    source.publish_shadow("s1", _train_text(+1), ["cos_axis0", "zero"])
    counters = RecsysCounters()
    stack = ScorerStack(
        HeuristicScorer(), [_shadow(source, 0, counters=counters), _shadow(source, 1, counters=counters)],
        counters=counters,
    )

    result = stack.score(_state(), _items(), NOW)

    assert result.model_version == "heuristic-v1"
    assert list(result.extra_scores) == ["lgbm:ranker@s1"]
    # 활성 점수는 프로필(축 1)과 같은 항목 2를, shadow 모델은 축 0인 항목 1을 선호한다
    assert result.scores[1] > result.scores[0]
    assert result.extra_scores["lgbm:ranker@s1"][0] > result.extra_scores["lgbm:ranker@s1"][1]
    assert counters.get("shadow.scored") == 1 and counters.get("shadow.unavailable") == 1


def test_runtime_stack_has_one_shadow_slot_per_shadow_max_when_a_feature_function_is_configured():
    from app.recsys.config import RecsysConfig
    from app.recsys.runtime import build_scorer_stack

    counters = RecsysCounters()
    stack = build_scorer_stack(RecsysConfig(shadow_max=3), FakeSource(), feature0_is_axis0_cosine, counters)

    assert [(s.role, s.slot, s.fallback) for s in stack.shadows] == [
        ("shadow", 0, None), ("shadow", 1, None), ("shadow", 2, None)
    ]
    assert stack.active.role == "active" and isinstance(stack.active.fallback, HeuristicScorer)
    assert stack.deadline_fraction == 0.5
    assert build_scorer_stack(RecsysConfig(shadow_max=0), FakeSource(), feature0_is_axis0_cosine, counters).shadows == []
