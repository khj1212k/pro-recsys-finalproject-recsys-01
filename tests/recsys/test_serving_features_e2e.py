"""서빙 피처 어댑터가 요청 경로에 붙은 모습(ADR 0033): 등록된 LightGBM 모델이 shadow로 후보에 점수를 매기고,
그 점수와 어댑터 피처(스키마 2)가 칸 로그에 남는다. 저장소는 메모리 구현이다 - 같은 일을 PostgreSQL에서 보는
것은 tests/integration/test_feature_parity.py다.
"""
from contextlib import contextmanager
from datetime import timedelta

import lightgbm as lgb
import numpy as np
import pytest

from app.recsys.config import RecsysConfig
from app.recsys.lgbm_scorer import RegisteredModel
from app.recsys.metrics import RecsysCounters
from app.recsys.runtime import build_scorer_stack
from app.recsys.scoring import ADAPTER_FEATURE_SCHEMA, FEATURE_SCHEMAS, HEURISTIC_FEATURE_SCHEMA, decode_features
from app.recsys.service import build_service
from app.recsys.shadow import ShadowRunner
from app.recsys.types import SOURCE_REALTIME
from recsys_core import serving
from tests.recsys.fakes import NOW, FakeNewsletter, FakeRepo, FakeUser, LogRecorder
from tests.recsys.test_lgbm_scorer import FakeSource

DIM = 16
N_FEATURES = len(serving.FEATURE_NAMES)
USER, OTHER = 1, 2


def train_ranker_text(seed: int = 0) -> str:
    """어댑터의 22열을 받는 작은 LightGBM 모델. 결측(NaN) 열이 섞인 무작위 데이터로 학습한다 -
    배선을 보려는 것이지 품질을 보려는 것이 아니다."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(800, N_FEATURES)).astype(np.float32)
    X[:, [serving.FEATURE_NAMES.index("user_age"), serving.FEATURE_NAMES.index("user_gender")]] = np.nan
    X[rng.random(800) < 0.3, serving.FEATURE_NAMES.index("hours_since_last_event")] = np.nan
    cols = {name: X[:, i] for i, name in enumerate(serving.FEATURE_NAMES)}
    y = 2.0 * cols["hist_cos"] + cols["short_cos"] + 0.5 * cols["pop_clicks_6h"] - 0.3 * cols["hours_since_pub"]
    booster = lgb.train(
        {"objective": "regression", "verbose": -1, "num_leaves": 8, "seed": seed, "min_data_in_leaf": 5},
        lgb.Dataset(X, y + rng.normal(0, 0.1, 800), feature_name=list(serving.FEATURE_NAMES)),
        num_boost_round=30,
    )
    return booster.model_to_string()


MODEL_TEXT = train_ranker_text()


def _world():
    """뉴스레터 60개(카테고리 1~4), 클릭 이력이 있는 사용자와 다른 사용자, 노출 로그."""
    rng = np.random.default_rng(5)
    corpus = [
        FakeNewsletter(
            id=i,
            embedding=rng.standard_normal(DIM).astype(np.float32),
            created_at=NOW - timedelta(hours=float(rng.uniform(0.5, 70))),
            raw_news_count=1 + i % 4,
            category_ids=(1 + i % 4,),
        )
        for i in range(1, 61)
    ]
    repo = FakeRepo(corpus, [FakeUser(USER, category_ids=[2, 3]), FakeUser(OTHER)])
    for k, nid in enumerate((3, 8, 13, 21, 34, 55)):
        repo.click(USER, nid, NOW - timedelta(days=6 - k, minutes=7 * k))
    for k, nid in enumerate((5, 9, 9)):  # 최근 한 시간 안, 같은 뉴스레터 두 번 포함
        repo.click(USER, nid, NOW - timedelta(minutes=40 - 12 * k))
    for k in range(40):
        repo.click(OTHER, 10 + k % 25, NOW - timedelta(hours=float(rng.uniform(0.01, 50))))
    for k in range(300):
        repo.impress(OTHER, [int(rng.integers(1, 61))], NOW - timedelta(hours=float(rng.uniform(0.01, 30))))
    return repo


def _publish(source, version, role="shadow", names=serving.FEATURE_NAMES, schema_hash=serving.SCHEMA_HASH):
    source.models[version] = RegisteredModel("ranker", version, MODEL_TEXT, list(names), schema_hash)
    if role == "active":
        source.active = version
    else:
        source.shadows.insert(0, version)


def _service(repo, source, log, runner=None, feature_repo=True, feature_fn=serving.features, **cfg_kw):
    @contextmanager
    def factory():
        yield repo

    # 전용 스레드의 결과가 로그에 남는 것을 본다: 로그 쓰기의 대기 상한(기본 100ms)에 걸려 버려지지 않게 넉넉히.
    cfg = RecsysConfig(**{"model_reload_s": 0.0, "shadow_log_wait_ms": 10_000, **cfg_kw})
    counters = RecsysCounters()
    stack = build_scorer_stack(cfg, source, feature_fn, counters, runner=runner)
    for scorer in (stack.active, *stack.shadows):
        scorer.reload_in_background = False  # 테스트에서는 요청 안에서 바로 읽는다
    rng = np.random.default_rng(1)
    service = build_service(
        cfg, repo_factory=factory, scorer=stack, impression_writer=log, now_fn=lambda: NOW, counters=counters,
        rng_factory=lambda request_id: rng, feature_repo_factory=factory if feature_repo else None,
    )
    return service


def _booster():
    return lgb.Booster(model_str=MODEL_TEXT)


@pytest.mark.parametrize("off_path", [False, True])
def test_a_registered_shadow_model_scores_candidates_with_adapter_features_and_both_land_in_the_slot_log(off_path):
    repo, log, source = _world(), LogRecorder(), FakeSource()
    _publish(source, "v1")
    runner = ShadowRunner(budget_s=10.0) if off_path else None
    service = _service(repo, source, log, runner=runner)
    try:
        rec = service.recommend(USER, fallback_repo=repo)
        service.log_impressions(USER, rec, rec.news_letter_ids)
    finally:
        service.shutdown()
        if runner is not None:
            runner.shutdown()

    (request,) = log.requests
    version = "lgbm:ranker@v1"
    # 목록을 만든 것은 휴리스틱이다. 모델은 점수만 남긴다.
    assert rec.source == SOURCE_REALTIME and request["model_version"] == "heuristic-v1"
    assert request["feature_schema_version"] == ADAPTER_FEATURE_SCHEMA == 2
    assert request["features_as_of"] == NOW
    assert request["shadow_versions"] == [version]
    assert len(log.slots) == 20
    decoded = np.stack([decode_features(s["features"]) for s in log.slots])
    assert decoded.shape == (20, len(FEATURE_SCHEMAS[ADAPTER_FEATURE_SCHEMA])) and decoded.dtype == np.float32
    # 칸에 남은 shadow 점수 = 그 칸에 남은 피처로 같은 모델이 낸 점수: 로그만으로 모델의 채점이 다시 만들어진다
    logged = np.array([s["scores_shadow"][version] for s in log.slots])
    assert logged == pytest.approx(_booster().predict(decoded), abs=1e-9)
    assert len(set(np.round(logged, 6))) > 5  # 상수 점수가 아니다
    col = {n: i for i, n in enumerate(serving.FEATURE_NAMES)}
    assert (decoded[:, col["hist_len"]] == 9).all() and (decoded[:, col["short_len"]] == 3).all()
    assert (decoded[:, col["user_ncat"]] == 2).all()
    assert np.isnan(decoded[:, col["user_age"]]).all()
    assert decoded[:, col["pop_inviews_24h"]].sum() > 0
    assert service.counters.get("features.computed") == 1 and service.counters.get("shadow.scored") == 1
    # 인기도 창 집계는 후보에 대해 한 번 읽는다
    assert repo.calls.count("item_window_counts") == 1


def test_without_a_way_to_read_popularity_no_adapter_features_are_logged_and_the_model_gives_no_score():
    """인기도 입력을 읽을 저장소가 없으면 0으로 채워 틀린 피처를 남기지 않는다: 활성 휴리스틱의 4항(스키마 1)만 남는다."""
    repo, log, source = _world(), LogRecorder(), FakeSource()
    _publish(source, "v1")
    service = _service(repo, source, log, feature_repo=False)
    try:
        rec = service.recommend(USER, fallback_repo=repo)
        service.log_impressions(USER, rec, rec.news_letter_ids)
    finally:
        service.shutdown()

    (request,) = log.requests
    assert rec.source == SOURCE_REALTIME
    assert request["feature_schema_version"] == HEURISTIC_FEATURE_SCHEMA and request["shadow_versions"] is None
    assert all(len(s["features"]) == 4 * 4 and s["scores_shadow"] is None for s in log.slots)
    assert service.counters.get("features.inputs_missing") == 1 and service.counters.get("shadow.unavailable") >= 1
    assert service.counters.get("shadow.error") == 0


def test_a_state_that_lags_the_click_log_is_not_used_for_features():
    repo, log, source = _world(), LogRecorder(), FakeSource()
    _publish(source, "v1")
    # 장기 프로필을 직접 준 사용자: 상태가 클릭 로그에서 만들어진 것이 아니다. 방금 한 클릭보다 상태가 오래됐다.
    repo.users[3] = FakeUser(3, long_term=np.ones(DIM, dtype=np.float32))
    real_profile_state = repo.profile_state

    def stale(user_id):
        state, cats = real_profile_state(user_id)
        if user_id == 3:
            state = type(state)(state.hist.__class__(state.hist.hist_sum, state.hist.anchor_s - 86_400, 1, {0: 1}),
                                state.last_event_at - timedelta(days=1))
        return state, cats

    repo.profile_state = stale
    repo.click(3, 7, NOW - timedelta(minutes=3))
    service = _service(repo, source, log)
    try:
        rec = service.recommend(3, fallback_repo=repo)
        service.log_impressions(3, rec, rec.news_letter_ids)
    finally:
        service.shutdown()

    assert log.requests[0]["feature_schema_version"] == HEURISTIC_FEATURE_SCHEMA
    assert service.counters.get("features.inputs_missing") == 1


@pytest.mark.parametrize("offset_us, logged_schema", [(-1, ADAPTER_FEATURE_SCHEMA), (0, HEURISTIC_FEATURE_SCHEMA),
                                                     (250_000, HEURISTIC_FEATURE_SCHEMA)])
def test_a_click_landing_in_the_request_second_is_used_only_if_it_precedes_the_request(offset_us, logged_schema):
    """요청 시각과 상태 조회 사이에 같은 사용자의 클릭이 커밋된 경우. 저장소의 장기 상태에는 그 클릭이 들어 있고,
    최근 클릭 조회(요청 시각 미만)에는 없다. 요청과 같은 초 안이라 초 단위의 기준 시각으로는 보이지 않는다 -
    어댑터가 마이크로초 시각(ProfileState.last_event_at)으로 알아채고 그 요청의 어댑터 피처를 남기지 않는다."""
    repo, log, source = _world(), LogRecorder(), FakeSource()
    _publish(source, "v1")
    assert NOW.microsecond == 0
    repo.click(USER, 17, NOW + timedelta(microseconds=offset_us))
    service = _service(repo, source, log)
    try:
        rec = service.recommend(USER, fallback_repo=repo)
        service.log_impressions(USER, rec, rec.news_letter_ids)
    finally:
        service.shutdown()

    (request,) = log.requests
    assert rec.source == SOURCE_REALTIME  # 응답은 어느 쪽이든 나간다
    assert request["feature_schema_version"] == logged_schema
    if logged_schema == ADAPTER_FEATURE_SCHEMA:
        decoded = np.stack([decode_features(s["features"]) for s in log.slots])
        assert (decoded[:, serving.FEATURE_NAMES.index("hist_len")] == 10).all()  # 방금 한 클릭까지 10건
        assert service.counters.get("features.inputs_missing") == 0
    else:
        assert request["shadow_versions"] is None and all(s["scores_shadow"] is None for s in log.slots)
        assert service.counters.get("features.inputs_missing") == 1 and service.counters.get("features.error") == 0


def test_an_active_model_reads_popularity_on_the_request_path_and_its_features_are_computed_once():
    repo, log, source = _world(), LogRecorder(), FakeSource()
    _publish(source, "a1", role="active")
    _publish(source, "s1")
    calls = []

    def counting(state, items, now):
        calls.append(len(items))
        return serving.features(state, items, now)

    for attr in ("feature_names", "schema_version", "schema_hash"):
        setattr(counting, attr, getattr(serving.features, attr))
    service = _service(repo, source, log, feature_fn=counting)
    try:
        service.recommender.stack.active.maybe_reload()  # 모델을 먼저 올려 둔다(첫 요청부터 활성 모델이 채점)
        rec = service.recommend(USER, fallback_repo=repo)
        service.log_impressions(USER, rec, rec.news_letter_ids)
    finally:
        service.shutdown()

    (request,) = log.requests
    assert rec.model_version == "lgbm:ranker@a1" and request["model_version"] == "lgbm:ranker@a1"
    assert request["feature_schema_version"] == 2 and request["shadow_versions"] == ["lgbm:ranker@s1"]
    assert len(calls) == 1  # 활성 모델이 계산한 행렬을 shadow와 로그가 같이 쓴다
    assert repo.calls.count("item_window_counts") == 1
    decoded = np.stack([decode_features(s["features"]) for s in log.slots])
    scores = np.array([s["score"] for s in log.slots])
    assert scores == pytest.approx(_booster().predict(decoded), abs=1e-9)
    # 같은 모델 본문이라 shadow 점수도 같다(버전 이름만 다르다)
    assert np.array([s["scores_shadow"]["lgbm:ranker@s1"] for s in log.slots]) == pytest.approx(scores, abs=1e-12)


def test_a_failed_popularity_read_for_the_active_model_scores_that_request_with_the_heuristic():
    repo, log, source = _world(), LogRecorder(), FakeSource()
    _publish(source, "a1", role="active")
    service = _service(repo, source, log, feature_repo=False)
    try:
        service.recommender.stack.active.maybe_reload()
        repo.fail_on.add("item_window_counts")
        rec = service.recommend(USER, fallback_repo=repo)
    finally:
        service.shutdown()

    assert rec.source == SOURCE_REALTIME and rec.fallback_reason is None  # 조회 하나가 요청을 폴백으로 보내지 않는다
    assert rec.model_version == "heuristic-v1"
    assert service.counters.get("features.popularity_error") == 1 and service.counters.get("scorer.lgbm_error") == 1


@pytest.mark.parametrize(
    "names,schema_hash,counter",
    [
        (serving.FEATURE_NAMES[::-1], serving.SCHEMA_HASH, "scorer.feature_mismatch"),
        (serving.FEATURE_NAMES, "0" * 64, "scorer.schema_mismatch"),
    ],
)
def test_a_model_registered_against_a_different_feature_schema_is_not_served(names, schema_hash, counter):
    repo, log, source = _world(), LogRecorder(), FakeSource()
    _publish(source, "a1", role="active", names=names, schema_hash=schema_hash)
    _publish(source, "s1", names=names, schema_hash=schema_hash)
    service = _service(repo, source, log)
    try:
        service.recommender.stack.active.maybe_reload()
        rec = service.recommend(USER, fallback_repo=repo)
        service.log_impressions(USER, rec, rec.news_letter_ids)
    finally:
        service.shutdown()

    assert rec.model_version == "heuristic-v1"  # 활성 자리는 휴리스틱이 채점한다
    assert log.requests[0]["shadow_versions"] is None  # shadow는 점수를 남기지 않는다
    assert service.counters.get(counter) == 2
    # 어댑터 피처 자체는 남는다(모델과 무관하게 계산된다)
    assert log.requests[0]["feature_schema_version"] == 2


def test_a_model_registered_before_schema_hashes_existed_is_checked_by_feature_names_only():
    repo, log, source = _world(), LogRecorder(), FakeSource()
    _publish(source, "s1", schema_hash=None)
    service = _service(repo, source, log)
    try:
        rec = service.recommend(USER, fallback_repo=repo)
        service.log_impressions(USER, rec, rec.news_letter_ids)
    finally:
        service.shutdown()

    assert log.requests[0]["shadow_versions"] == ["lgbm:ranker@s1"]
