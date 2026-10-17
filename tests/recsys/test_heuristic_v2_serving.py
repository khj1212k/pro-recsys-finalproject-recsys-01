"""heuristic-v2가 서빙 경로에 붙은 모습(ADR 0014 사전 등록 2·3·4·5·8).

- 네 항이 같은 요청의 서빙 어댑터 열(ADR 0033)과 같고, 점수가 E7을 적합한 하네스의 식(heuristic_fit)과 같다 -
  그래야 서빙에 옮긴 가중치가 E7에서 잰 식의 가중치다.
- 6시간 클릭 수의 창과 popular 출처의 순서·창, 클릭 조회가 실패한 요청, 콜드 경로, 되돌리기 세트(prior),
  콜드스타트 임계(RECSYS_MIN_PERSONAL_EVENTS)의 요청 로그.
"""
from contextlib import contextmanager
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from app.recsys.config import RecsysConfig
from app.recsys.deadline import Deadline
from app.recsys.pipeline import ClickCounts, build_user_state, load_popularity, popular_ids
from app.recsys.scoring import (
    HEURISTIC_FEATURE_SCHEMA,
    PRIOR_HEURISTIC_FEATURE_SCHEMA,
    WEIGHTS_A,
    WEIGHTS_B,
    HeuristicScorer,
    decode_features,
)
from app.recsys.service import build_service
from app.recsys.types import SOURCE_COLD_ONBOARDING, SOURCE_COLD_POPULAR, SOURCE_REALTIME
from evaluation.recsys.ebnerd.heuristic_fit import fitted_scores, heuristic_terms
from recsys_core import serving
from tests.recsys.fakes import NOW, FakeNewsletter, FakeRepo, FakeUser, LogRecorder

DIM = 16
COL = {name: i for i, name in enumerate(serving.FEATURE_NAMES)}
HEAVY, LIGHT, STALE, NOBODY, ONBOARDED = 1, 2, 3, 4, 5
# 초 미만 자릿수가 있는 요청 시각: 요청 초 T = floor(now) + 1 규칙이 실제로 쓰인다
REQ = NOW + timedelta(microseconds=730_000)


def _world(seed=3):
    """뉴스레터 90개(70시간에 걸쳐 발행, 초 미만 시각), 클릭 이력이 다른 사용자 다섯, 군중 클릭."""
    rng = np.random.default_rng(seed)
    corpus = [
        FakeNewsletter(
            id=i,
            embedding=rng.standard_normal(DIM).astype(np.float32),
            created_at=REQ - timedelta(hours=float(rng.uniform(0.01, 70)), microseconds=int(rng.integers(0, 10**6))),
            raw_news_count=1 + i % 7,
            category_ids=(1 + i % 4,),
        )
        for i in range(1, 91)
    ]
    users = [FakeUser(HEAVY), FakeUser(LIGHT), FakeUser(STALE), FakeUser(NOBODY),
             FakeUser(ONBOARDED, onboarding_ids=[3, 4])]
    repo = FakeRepo(corpus, users + [FakeUser(100 + c) for c in range(30)])
    ids = [n.id for n in corpus]
    # HEAVY: 24시간 안에 30번(단기 상한 20을 넘는다) + 오래된 이력. LIGHT: 3번. STALE: 이틀 전보다 오래된 클릭만.
    for _ in range(30):
        repo.click(HEAVY, int(rng.choice(ids)), REQ - timedelta(hours=float(rng.uniform(0.001, 23.9))))
    for _ in range(15):
        repo.click(HEAVY, int(rng.choice(ids)), REQ - timedelta(days=float(rng.uniform(1.1, 20))))
    for _ in range(3):
        repo.click(LIGHT, int(rng.choice(ids)), REQ - timedelta(hours=float(rng.uniform(0.5, 30))))
    for _ in range(4):
        repo.click(STALE, int(rng.choice(ids)), REQ - timedelta(days=float(rng.uniform(2, 9))))
    # 군중: 6시간 창 안과 밖, 창 경계 바로 안팎
    for _ in range(400):
        repo.click(100 + int(rng.integers(0, 30)), int(rng.choice(ids)),
                   REQ - timedelta(hours=float(rng.uniform(0.0, 12)), microseconds=int(rng.integers(0, 10**6))))
    return repo, ids


def _service(repo, cfg=None, log=None, **kw):
    @contextmanager
    def factory():
        yield repo

    return build_service(cfg or RecsysConfig(), repo_factory=factory, now_fn=lambda: REQ, impression_writer=log, **kw)


# ------------------------------------------------------------------ 어댑터 열·하네스 식과 같다 (사전 등록 2·8)
@pytest.mark.parametrize("weights", [WEIGHTS_B, WEIGHTS_A], ids=["b", "a"])
@pytest.mark.parametrize("user", [HEAVY, LIGHT, STALE])
def test_the_four_terms_equal_the_adapter_columns_and_the_score_is_the_e7_harness_formula(user, weights):
    repo, ids = _world()
    state = build_user_state(repo, user, REQ, RecsysConfig())
    assert state.profile_source == "long_term"  # 장기 벡터가 쓰이는 사용자(사전 등록 2의 조건)
    items = list(repo.items(ids).values())
    state.popularity = load_popularity(repo, ids, REQ)
    state.clicks_6h = ClickCounts(repo, REQ).get(ids)

    adapter = serving.features(state, items, REQ).astype(np.float64)
    result = HeuristicScorer(weights).score(state, items, REQ)
    terms = result.features.astype(np.float64)

    np.testing.assert_allclose(terms[:, 0], adapter[:, COL["hist_cos"]], rtol=0, atol=1e-6)
    if state.short_term is None:  # 24시간 안의 클릭이 없다: 로그에는 NaN, 어댑터(하네스)는 0
        assert user == STALE and np.all(np.isnan(terms[:, 1])) and np.all(adapter[:, COL["short_cos"]] == 0)
    else:
        np.testing.assert_allclose(terms[:, 1], adapter[:, COL["short_cos"]], rtol=0, atol=1e-6)
    np.testing.assert_allclose(terms[:, 2], np.exp(-adapter[:, COL["hours_since_pub"]] / 48), rtol=0, atol=1e-6)
    np.testing.assert_allclose(terms[:, 3], np.log1p(adapter[:, COL["pop_clicks_6h"]]), rtol=0, atol=1e-6)
    assert (adapter[:, COL["pop_clicks_6h"]] > 0).sum() > 10  # 0만 비교하는 테스트가 아니다

    # 같은 피처 열로 하네스의 적합 식(E7)이 낸 점수와 서빙 점수가 같다
    frame = pd.DataFrame(adapter.astype(np.float32), columns=list(serving.FEATURE_NAMES))
    w = [weights.long_term, weights.short_term, weights.recency, weights.popularity]
    np.testing.assert_allclose(result.scores, fitted_scores(frame, w), rtol=0, atol=1e-5)
    np.testing.assert_allclose(np.nan_to_num(terms), heuristic_terms(frame), rtol=0, atol=1e-6)


def test_the_short_vector_is_capped_at_the_adapter_twenty_clicks():
    repo, _ = _world()
    state = build_user_state(repo, HEAVY, REQ, RecsysConfig())
    in_window = [c for c in repo.clicks if c[1] == HEAVY and c[3] >= serving.short_window_start(REQ)]
    assert len(in_window) == 30 and len(state.recent_clicks) == serving.SHORT_MAX_EVENTS == 20


# ------------------------------------------------------------------ 6시간 클릭 수와 popular 출처 (사전 등록 2·4)
def _small_repo():
    t = NOW  # 초 미만 자릿수 없음: T = NOW + 1초, 창 = [NOW - 1초 - 6시간, NOW - 1초)
    corpus = [
        FakeNewsletter(1, np.ones(DIM, np.float32), t - timedelta(hours=10)),
        FakeNewsletter(2, np.ones(DIM, np.float32), t - timedelta(hours=1)),
        FakeNewsletter(3, np.ones(DIM, np.float32), t - timedelta(hours=10)),
        FakeNewsletter(4, np.ones(DIM, np.float32), t - timedelta(hours=50)),
        FakeNewsletter(5, np.ones(DIM, np.float32), t - timedelta(hours=80)),  # 신선도 창(72시간) 밖
        FakeNewsletter(6, np.ones(DIM, np.float32), t - timedelta(hours=2), category_ids=()),  # 화면에 못 나간다
    ]
    repo = FakeRepo(corpus, [FakeUser(9)])
    start, end = t - timedelta(seconds=1, hours=6), t - timedelta(seconds=1)
    for _ in range(2):
        repo.click(9, 4, start)                       # 창의 시작: 들어간다
    repo.click(9, 1, start - timedelta(microseconds=1))  # 시작 바로 앞: 빠진다
    repo.click(9, 1, end)                             # 창의 끝(미포함): 빠진다
    repo.click(9, 1, end - timedelta(microseconds=1))  # 끝 바로 앞: 들어간다
    for _ in range(5):
        repo.click(9, 5, t - timedelta(hours=1))       # 창 밖의 뉴스레터는 클릭이 많아도 출처에 없다
        repo.click(9, 6, t - timedelta(hours=1))
    return repo, t


def test_six_hour_click_counts_use_the_adapter_window():
    repo, t = _small_repo()
    assert ClickCounts(repo, t).get([1, 2, 3, 4, 5]) == {1: 1, 2: 0, 3: 0, 4: 2, 5: 5}


def test_the_popular_source_orders_the_freshness_window_by_six_hour_clicks():
    repo, t = _small_repo()
    since = t - timedelta(hours=72)
    # 4(2건) -> 1(1건) -> 클릭 0건은 생성 시각 내림차순(2) -> 같은 시각이면 ID 내림차순(3)
    assert popular_ids(repo, since, t, 100) == [4, 1, 2, 3]
    assert popular_ids(repo, since, t, 2) == [4, 1]


def test_one_request_reads_each_items_clicks_once_and_a_failed_read_is_not_retried():
    repo, t = _small_repo()
    counts = ClickCounts(repo, t)
    counts.get([1, 2, 3])
    counts.get([2, 3, 4])
    assert repo.calls.count("item_click_counts") == 2 and counts.reads == 2  # 두 번째는 4만 읽었다

    repo.fail_on.add("item_click_counts")
    errors = []
    failing = ClickCounts(repo, t, on_error=lambda: errors.append(1))
    assert failing.get([1]) is None and failing.get([2]) is None
    assert errors == [1] and repo.calls.count("item_click_counts") == 3


# ------------------------------------------------------------------ 서비스 수준
def test_a_failed_click_read_drops_the_popularity_term_but_keeps_the_realtime_answer():
    repo, _ = _world()
    repo.fail_on.add("item_click_counts")
    log = LogRecorder()
    service = _service(repo, RecsysConfig(feature_fn=None), log=log)
    try:
        rec = service.recommend(LIGHT, fallback_repo=repo)
        service.log_impressions(LIGHT, rec, rec.news_letter_ids)
    finally:
        service.shutdown()

    assert rec.source == SOURCE_REALTIME and rec.fallback_reason is None
    assert rec.model_version == "heuristic-v2-b"
    assert service.counters.get("popularity.clicks_error") == 1  # 한 요청에 한 번만 세고 다시 읽지 않는다
    (request,) = log.requests
    assert request["feature_schema_version"] == HEURISTIC_FEATURE_SCHEMA
    assert all(np.isnan(decode_features(s["features"])[3]) for s in log.slots_of(rec.request_id))


def test_users_without_any_signal_get_the_non_personal_terms_of_the_active_heuristic():
    repo, ids = _world()
    service = _service(repo)
    try:
        det = service.recommender.rank(repo, NOBODY, REQ, Deadline(10.0))
        rec = service.recommend(NOBODY, fallback_repo=repo)
    finally:
        service.shutdown()

    metas = repo.window_meta(REQ - timedelta(hours=72))
    want = HeuristicScorer().cold_order(metas, ClickCounts(repo, REQ).get([m.news_letter_id for m in metas]), REQ)
    assert det.source == SOURCE_COLD_POPULAR and det.model_version == "heuristic-v2-b"
    assert det.eligible_ids.tolist() == [nid for nid, _ in want][: len(det.eligible_ids)]
    np.testing.assert_allclose(det.scores, [s for _, s in want][: len(det.eligible_ids)])
    assert (rec.source, rec.model_version, rec.profile_source) == (SOURCE_COLD_POPULAR, "heuristic-v2-b", "none")


def test_the_prior_set_rolls_back_to_heuristic_v1_and_popularity_v1():
    cfg = RecsysConfig(heuristic_set="prior", feature_fn=None)
    warm_repo, _ = _world()
    cold_repo, _ = _world()
    service = _service(warm_repo, cfg)
    try:
        warm = service.recommender.rank(warm_repo, LIGHT, REQ, Deadline(10.0))
        cold = service.recommender.rank(cold_repo, NOBODY, REQ, Deadline(10.0))
    finally:
        service.shutdown()

    assert (warm.model_version, cold.model_version) == ("heuristic-v1", "popularity-v1")
    assert warm.feature_schema_version == PRIOR_HEURISTIC_FEATURE_SCHEMA
    # 후보 출처 popular는 세트와 무관하게 6시간 클릭 순서라 개인화 경로는 클릭 수를 읽는다(사전 등록 4).
    # 되돌리기 세트의 콜드 경로(compute_scores)는 읽지 않는다.
    assert "item_click_counts" in warm_repo.calls and "item_click_counts" not in cold_repo.calls


@pytest.mark.parametrize("threshold, profile_source", [(0, "long_term"), (3, "long_term"), (4, "onboarding")])
def test_the_cold_start_threshold_is_logged_as_the_profile_source(threshold, profile_source):
    repo, ids = _world()
    for k in range(3):
        repo.click(ONBOARDED, ids[10 + k], REQ - timedelta(hours=2 + k))
    log = LogRecorder()
    service = _service(repo, RecsysConfig(min_personal_events=threshold, feature_fn=None), log=log)
    try:
        rec = service.recommend(ONBOARDED, fallback_repo=repo)
        service.log_impressions(ONBOARDED, rec, rec.news_letter_ids)
    finally:
        service.shutdown()

    (request,) = log.requests
    assert rec.profile_source == request["profile_source"] == profile_source
    assert rec.source == (SOURCE_REALTIME if profile_source == "long_term" else SOURCE_COLD_ONBOARDING)
    assert service.counters.get("profile.below_min_events") == (1 if profile_source == "onboarding" else 0)
