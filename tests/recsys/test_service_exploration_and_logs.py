"""서비스 수준: 캐시 뒤 탐색, 요청·칸 로그 행, shadow 점수, 노출 피로 규칙 (ADR 0025).

메커니즘과 식 자체는 test_exploration_slots.py가 본다. 여기서는 그것이 요청 경로에 맞게 붙었는지 -
캐시가 탐색을 얼리지 않는지, 로그에 남는 값이 화면과 일치하는지 - 를 메모리 저장소로 확인한다.
"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from app.recsys.config import RecsysConfig
from app.recsys.deadline import Deadline
from app.recsys.exploration import det_propensity, plan_slate
from app.recsys.scoring import HeuristicScorer, HeuristicWeights, ScorerStack, decode_features
from app.recsys.service import build_service, rng_for_request
from app.recsys.types import SOURCE_COLD_POPULAR, SOURCE_POPULAR, SOURCE_REALTIME, ScoreResult
from tests.recsys.fakes import NOW, FakeRepo, FakeUser, LogRecorder, axis_vec, two_topic_corpus

DIM = 16
WARM, NOBODY = 1, 2


def _repo():
    return FakeRepo(
        two_topic_corpus(dim=DIM, per_topic=15),
        [FakeUser(WARM, long_term=axis_vec(DIM, 0)), FakeUser(NOBODY)],
    )


def _seeded(seed=0):
    """요청마다 같은 Generator에서 이어 뽑는다: 시드 하나로 여러 요청의 뽑기가 재현된다."""
    rng = np.random.default_rng(seed)
    return lambda request_id: rng


def _service(repo, cfg=None, log=None, **kw):
    @contextmanager
    def factory():
        yield repo

    return build_service(
        cfg or RecsysConfig(), repo_factory=factory, now_fn=lambda: NOW, impression_writer=log, **kw
    )


def _det(service, repo, user_id):
    return service.recommender.rank(repo, user_id, NOW, Deadline(10.0))


# ----------------------------------------------------------------------------- 캐시 뒤 탐색


def test_cache_hits_reuse_the_deterministic_list_but_draw_exploration_per_request():
    repo = _repo()
    service = _service(repo, rng_factory=_seeded(1))

    recs = [service.recommend(WARM, fallback_repo=repo) for _ in range(40)]

    assert repo.calls.count("knn_ids") == 1  # 결정론 목록은 한 번만 계산됐다
    assert [r.cache_hit for r in recs] == [False] + [True] * 39
    det = _det(service, repo, WARM)
    for rec in recs:
        kept = [nid for nid, slot in zip(rec.news_letter_ids, rec.slots) if not slot.explored]
        explored = [nid for nid, slot in zip(rec.news_letter_ids, rec.slots) if slot.explored]
        assert kept == det.ranked_ids[:18]  # 결정론 부분은 요청과 무관하다
        assert len(explored) == 2 and not set(explored) & set(det.ranked_ids[:18])
        assert len(set(rec.news_letter_ids)) == 20
    # 탐색 칸은 캐시에 얼어 있지 않다: 위치도 아이템도 요청마다 달라진다
    assert len({rec.explore_positions for rec in recs}) > 20
    assert len({nid for rec in recs for nid, s in zip(rec.news_letter_ids, rec.slots) if s.explored}) > 6
    assert service.counters.get("explore.requests") == 40
    assert service.counters.get("explore.slots") == 80


def test_users_with_a_profile_get_two_slots_and_users_without_any_signal_get_four():
    repo = _repo()
    service = _service(repo, rng_factory=_seeded(2))

    warm = service.recommend(WARM, fallback_repo=repo)
    cold = service.recommend(NOBODY, fallback_repo=repo)

    assert (warm.source, warm.profile_source, len(warm.explore_positions)) == (SOURCE_REALTIME, "long_term", 2)
    assert (cold.source, cold.profile_source, len(cold.explore_positions)) == (SOURCE_COLD_POPULAR, "none", 4)
    assert cold.policy_version == warm.policy_version == "eps-uniform-v1"
    # 신호 없는 사용자의 결정론 부분은 인기 목록의 앞 16개다
    det = _det(service, repo, NOBODY)
    assert [n for n, s in zip(cold.news_letter_ids, cold.slots) if not s.explored] == det.ranked_ids[:16]


@pytest.mark.parametrize("cfg", [RecsysConfig(explore_enabled=False), RecsysConfig(explore_slots=0, explore_slots_cold=0)])
def test_exploration_can_be_switched_off_and_the_list_is_then_the_deterministic_one(cfg):
    repo = _repo()
    service = _service(repo, cfg)

    for user in (WARM, NOBODY):
        rec = service.recommend(user, fallback_repo=repo)
        assert rec.news_letter_ids == _det(service, repo, user).ranked_ids
        assert rec.policy_version == "deterministic" and rec.explore_positions == ()
        assert [s.propensity for s in rec.slots] == [1.0] * 20
        assert [s.det_rank for s in rec.slots] == list(range(20))
    assert service.counters.get("explore.requests") == 0


def test_the_default_draw_is_seeded_by_the_request_id_so_a_logged_request_can_be_replayed():
    repo = _repo()
    service = _service(repo)

    first = service.recommend(WARM, fallback_repo=repo)
    second = service.recommend(WARM, fallback_repo=repo)

    det = _det(service, repo, WARM)
    for rec in (first, second):
        replayed = plan_slate(det.ranked_ids, det.eligible_ids.tolist(), 20, 2, rng_for_request(rec.request_id))
        assert replayed.ids == rec.news_letter_ids
        assert replayed.explore_positions == rec.explore_positions
    assert first.request_id != second.request_id


def test_exploration_marginals_hold_over_many_service_requests():
    """서비스를 거친 3천 요청의 칸 정보로 본 주변 확률. 위치별 탐색 비율 m/S, 탐색 칸의 propensity,
    그리고 결정론 칸의 1/propensity 합의 기대값 = (결정론 칸 수) x (한 아이템이 놓일 수 있는 위치 수)."""
    repo = _repo()
    service = _service(repo, rng_factory=_seeded(3))
    n = 3_000

    at_position = Counter()
    det_weight_sums = []
    for _ in range(n):
        rec = service.recommend(WARM, fallback_repo=repo)
        pool = rec.explore_pool_size
        det_weight = 0.0
        for pos, slot in enumerate(rec.slots):
            if slot.explored:
                at_position[pos] += 1
                assert slot.propensity == pytest.approx((2 / 20) / pool)
            else:
                assert slot.propensity == pytest.approx(det_propensity(slot.det_rank, pos, 20, 2))
                det_weight += 1.0 / slot.propensity
        det_weight_sums.append(det_weight)

    assert sum(at_position.values()) == 2 * n
    sd = np.sqrt(n * 0.1 * 0.9)
    assert all(abs(at_position[pos] - 0.1 * n) < 5 * sd for pos in range(20))
    # 결정론 아이템마다 놓일 수 있는 위치는 m+1 = 3곳: Σ_칸 1/propensity의 기대값은 18 x 3
    sums = np.asarray(det_weight_sums)
    assert abs(sums.mean() - 18 * 3) < 4 * sums.std(ddof=1) / np.sqrt(n)


# ----------------------------------------------------------------------------- 로그 행


def test_request_and_slot_rows_describe_the_slate_that_was_shown():
    repo = _repo()
    log = LogRecorder()
    service = _service(repo, log=log, rng_factory=_seeded(4))
    rec = service.recommend(WARM, fallback_repo=repo)

    service.log_impressions(WARM, rec, rec.news_letter_ids)

    (request,) = log.requests
    slots = log.slots_of(rec.request_id)
    det = _det(service, repo, WARM)
    assert request["request_id"] == rec.request_id and request["user_id"] == WARM
    assert (request["source"], request["model_version"]) == (SOURCE_REALTIME, "heuristic-v1")
    assert (request["policy_version"], request["profile_source"]) == ("eps-uniform-v1", "long_term")
    assert (request["cache_hit"], request["fallback_reason"]) == (False, None)
    assert request["candidate_ids"] == det.eligible_ids.tolist()
    assert all(type(nid) is int for nid in request["candidate_ids"])  # DB 드라이버는 numpy 정수를 못 받는다
    assert request["eligible_count"] == len(det.eligible_ids) == 30
    assert request["candidate_count"] == 30
    assert request["explore_pool_size"] == 30 - 18
    assert request["slate_size"] == request["shown_count"] == 20
    assert request["feature_schema_version"] == 1 and request["shadow_versions"] is None
    assert request["latency_ms"] is not None and request["latency_ms"] >= 0

    assert [s["position"] for s in slots] == list(range(20))
    assert [s["news_letter_id"] for s in slots] == rec.news_letter_ids
    explored = [s for s in slots if s["explored"]]
    assert [s["position"] for s in explored] == request["explore_positions"] and len(explored) == 2
    for s in slots:
        if s["explored"]:
            assert s["det_rank"] is None
            assert s["propensity"] == pytest.approx((2 / 20) / 12)
        else:
            assert s["news_letter_id"] == det.ranked_ids[s["det_rank"]]
            assert s["propensity"] == pytest.approx(det_propensity(s["det_rank"], s["position"], 20, 2))
    # 위치마다 "이 칸에 무엇이든 놓일 확률"은 1이다: 탐색 풀 전체 + 그 위치에 올 수 있는 결정론 순위들
    for pos in range(20):
        total = 12 * (2 / 20) / 12 + sum(det_propensity(r, pos, 20, 2) for r in range(18))
        assert total == pytest.approx(1.0)

    # 탐색으로 들어온 아이템에도 활성 점수와 피처가 남고, 피처로 점수를 다시 계산할 수 있다
    w = HeuristicWeights()
    for s in slots:
        cos_long, cos_short, recency, popularity = decode_features(s["features"]).astype(float)
        assert np.isnan(cos_short)  # 이 사용자는 단기 벡터가 없다
        rebuilt = (w.long_term + w.short_term) * cos_long + w.recency * recency + w.popularity * popularity
        assert s["score"] == pytest.approx(rebuilt, abs=1e-6)
    assert service.counters.get("requests.logged") == 1
    assert service.counters.get("impressions.logged") == 20


def test_propensity_is_withheld_when_the_shown_list_differs_from_the_planned_slate():
    """표시 단계가 항목 하나를 빼면 뒤 칸들의 위치가 당겨진다. 기록해 둔 확률은 계획한 위치의 것이므로
    그 요청의 propensity는 남기지 않는다(틀린 값을 남기느니 없는 편이 낫다)."""
    repo = _repo()
    log = LogRecorder()
    service = _service(repo, log=log, rng_factory=_seeded(5))
    rec = service.recommend(WARM, fallback_repo=repo)
    shown = rec.news_letter_ids[:7] + rec.news_letter_ids[8:]

    service.log_impressions(WARM, rec, shown)

    (request,) = log.requests
    slots = log.slots_of(rec.request_id)
    assert (request["slate_size"], request["shown_count"]) == (20, 19)
    assert [s["position"] for s in slots] == list(range(19))
    assert all(s["propensity"] is None for s in slots)
    assert sum(s["explored"] for s in slots) in (1, 2)  # 무엇이 탐색 칸이었는지는 남는다
    assert service.counters.get("impressions.slate_mismatch") == 1


def test_a_fallback_response_logs_a_request_row_with_its_reason_and_no_propensity():
    repo = _repo()
    repo.fail_on.add("knn_ids")
    log = LogRecorder()
    service = _service(repo, log=log)
    rec = service.recommend(WARM, fallback_repo=repo)

    service.log_impressions(WARM, rec, rec.news_letter_ids[:20])

    (request,) = log.requests
    assert (request["source"], request["fallback_reason"], request["policy_version"]) == (SOURCE_POPULAR, "error", "none")
    assert request["candidate_ids"] is None and request["explore_positions"] is None
    assert request["slate_size"] == request["shown_count"] == 20
    slots = log.slots_of(rec.request_id)
    assert len(slots) == 20
    assert all(s["propensity"] is None and s["explored"] is False and s["det_rank"] is None for s in slots)
    assert service.counters.get("impressions.slate_mismatch") == 0


def test_a_candidate_set_with_a_repeated_id_is_answered_by_the_fallback_and_logs_no_propensity():
    """후보에 같은 ID가 두 번 들어오면(후보 생성기의 버그) propensity 식의 전제가 깨진다. 틀린 값을 로그에
    남기는 대신 그 요청은 폴백으로 응답한다: 사용자는 목록을 받고, 로그에는 추정에 쓰지 않는 행만 남는다."""
    repo = _repo()
    log = LogRecorder()
    service = _service(repo, log=log, rng_factory=_seeded(13))
    rank = service.recommender.rank

    def rank_with_a_repeated_candidate(*args):
        det = rank(*args)
        return replace(det, eligible_ids=np.append(det.eligible_ids, det.eligible_ids[-1]))

    service.recommender.rank = rank_with_a_repeated_candidate
    rec = service.recommend(WARM, fallback_repo=repo)
    service.log_impressions(WARM, rec, rec.news_letter_ids[:20])

    assert (rec.source, rec.fallback_reason, rec.policy_version) == (SOURCE_POPULAR, "error", "none")
    assert len(log.slots) == 20 and all(s["propensity"] is None for s in log.slots)
    assert service.counters.get("fallback.error") == 1
    assert service.counters.get("explore.requests") == 0


def test_an_empty_response_still_leaves_one_request_row():
    repo = FakeRepo([], [FakeUser(WARM, long_term=axis_vec(DIM, 0))])
    log = LogRecorder()
    service = _service(repo, log=log)
    rec = service.recommend(WARM, fallback_repo=repo)

    service.log_impressions(WARM, rec, [])

    assert rec.source == "empty"
    (request,) = log.requests
    assert (request["source"], request["shown_count"], request["fallback_reason"]) == ("empty", 0, "empty")
    assert log.slots == []
    assert service.counters.get("requests.logged") == 1


def test_logged_values_are_plain_python_types_the_database_driver_accepts():
    """칸 행과 요청 행에 numpy 스칼라가 섞이면 psycopg2가 바인딩하지 못해 로그 쓰기가 통째로 실패한다."""
    repo = _repo()
    log = LogRecorder()
    service = _service(repo, log=log, scorer=ScorerStack(HeuristicScorer(), [ReverseOfActive()]),
                       rng_factory=_seeded(11))
    rec = service.recommend(WARM, fallback_repo=repo)
    service.log_impressions(WARM, rec, rec.news_letter_ids)

    plain = (int, float, str, bool, bytes, type(None))
    for row in log.slots + log.requests:
        for key, value in row.items():
            if isinstance(value, dict):
                assert all(type(v) in plain for v in value.values()), key
            elif isinstance(value, list):
                assert all(type(v) in plain for v in value), key
            else:
                assert type(value) in plain, (key, type(value))


def test_a_slot_without_shadow_scores_is_stored_as_sql_null_not_json_null():
    """JSON 컬럼의 기본 동작은 파이썬 None을 JSON null('null'::jsonb)로 넣는 것이다. 그러면 shadow가 없는 칸을
    "scores_shadow IS NULL"로 고를 수 없다. 칸 로그의 컬럼은 None을 SQL NULL로 바인딩해야 한다."""
    from sqlalchemy.dialects import postgresql

    from app.models.recsys import RecommendationImpressionLog

    bind = RecommendationImpressionLog.__table__.c.scores_shadow.type.bind_processor(postgresql.dialect())

    assert bind(None) is None
    assert bind({"lgbm:ranker@v1": 0.5}) == '{"lgbm:ranker@v1": 0.5}'


def test_a_cache_hit_is_recorded_on_the_request_row_with_its_own_exploration():
    repo = _repo()
    log = LogRecorder()
    service = _service(repo, log=log, rng_factory=_seeded(6))

    for _ in range(2):
        rec = service.recommend(WARM, fallback_repo=repo)
        service.log_impressions(WARM, rec, rec.news_letter_ids)

    first, second = log.requests
    assert (first["cache_hit"], second["cache_hit"]) == (False, True)
    assert first["candidate_ids"] == second["candidate_ids"]
    assert first["explore_positions"] != second["explore_positions"]
    assert first["request_id"] != second["request_id"]


# ----------------------------------------------------------------------------- shadow 스코어러


class ReverseOfActive:
    """활성 점수의 부호를 뒤집은 shadow. 응답에 섞이면 목록이 뒤집히므로 섞였는지 바로 드러난다."""

    version = "shadow-reverse"

    def __init__(self, fail=False, nan_for=None):
        self.fail, self.nan_for = fail, nan_for
        self.calls = 0

    def score(self, state, items, now):
        self.calls += 1
        if self.fail:
            raise RuntimeError("shadow model exploded")
        scores = -HeuristicScorer().score(state, items, now).scores
        if self.nan_for is not None:
            scores[[i for i, it in enumerate(items) if it.news_letter_id == self.nan_for]] = np.nan
        return ScoreResult(scores=scores, model_version=self.version)


def test_shadow_scores_are_logged_for_every_slot_and_never_change_the_response():
    repo = _repo()
    shadow = ReverseOfActive()
    log = LogRecorder()
    plain = _service(_repo(), rng_factory=_seeded(7))
    stacked = _service(repo, log=log, scorer=ScorerStack(HeuristicScorer(), [shadow]), rng_factory=_seeded(7))

    expected = plain.recommend(WARM, fallback_repo=_repo())
    rec = stacked.recommend(WARM, fallback_repo=repo)
    stacked.log_impressions(WARM, rec, rec.news_letter_ids)
    again = stacked.recommend(WARM, fallback_repo=repo)  # 캐시 적중: shadow를 다시 돌리지 않는다
    stacked.log_impressions(WARM, again, again.news_letter_ids)

    assert rec.news_letter_ids == expected.news_letter_ids and rec.scores == expected.scores
    assert rec.model_version == "heuristic-v1"
    assert shadow.calls == 1
    first, second = log.requests
    assert first["shadow_versions"] == second["shadow_versions"] == ["shadow-reverse"]
    for request in (first, second):
        slots = log.slots_of(request["request_id"])
        assert len(slots) == 20
        for s in slots:  # 탐색 칸 포함 모든 칸에 같은 후보의 shadow 점수가 있다
            assert s["scores_shadow"] == {"shadow-reverse": pytest.approx(-s["score"])}


def test_a_failing_shadow_leaves_the_response_and_the_log_intact():
    repo = _repo()
    log = LogRecorder()
    plain = _service(_repo(), rng_factory=_seeded(8))
    stacked = _service(
        repo, log=log, scorer=ScorerStack(HeuristicScorer(), [ReverseOfActive(fail=True)]), rng_factory=_seeded(8)
    )

    expected = plain.recommend(WARM, fallback_repo=_repo())
    rec = stacked.recommend(WARM, fallback_repo=repo)
    stacked.log_impressions(WARM, rec, rec.news_letter_ids)

    assert rec.source == SOURCE_REALTIME and rec.fallback_reason is None
    assert rec.news_letter_ids == expected.news_letter_ids
    assert log.requests[0]["shadow_versions"] is None
    assert all(s["scores_shadow"] is None for s in log.slots)
    assert stacked.recommender.stack.counters.get("shadow.error") == 1


def test_a_non_finite_shadow_score_is_logged_as_null_so_the_row_stays_valid_json():
    repo = _repo()
    log = LogRecorder()
    probe = _service(_repo(), rng_factory=_seeded(9)).recommend(WARM, fallback_repo=_repo())
    broken_item = probe.news_letter_ids[0]
    service = _service(
        repo, log=log, scorer=ScorerStack(HeuristicScorer(), [ReverseOfActive(nan_for=broken_item)]),
        rng_factory=_seeded(9),
    )

    rec = service.recommend(WARM, fallback_repo=repo)
    service.log_impressions(WARM, rec, rec.news_letter_ids)

    by_item = {s["news_letter_id"]: s["scores_shadow"]["shadow-reverse"] for s in log.slots}
    assert by_item[broken_item] is None
    assert all(v is not None for nid, v in by_item.items() if nid != broken_item)


# ----------------------------------------------------------------------------- 노출 피로 규칙


def _impress_three_times(repo, user, ids):
    for hours_ago in (30, 20, 10):
        repo.impress(user, ids, NOW - timedelta(hours=hours_ago))


def test_fatigue_log_mode_counts_fatigued_candidates_without_removing_them():
    repo = _repo()
    service = _service(repo, RecsysConfig(explore_enabled=False))
    top = service.recommend(WARM, fallback_repo=repo).news_letter_ids[:3]
    _impress_three_times(repo, WARM, top)
    repo.impress(WARM, [top[0]], NOW - timedelta(hours=60))  # 48시간 밖의 노출은 세지 않는다
    service.cache.clear()

    rec = service.recommend(WARM, fallback_repo=repo)

    assert rec.news_letter_ids[:3] == top  # 세기만 하고 빼지 않는다
    assert (rec.fatigue_mode, rec.fatigued_count) == ("log", 3)
    assert service.counters.get("fatigue.items") == 3
    assert service.counters.get("fatigue.requests_with_fatigued") == 1


def test_two_impressions_or_other_users_impressions_do_not_fatigue_an_item():
    repo = _repo()
    service = _service(repo, RecsysConfig(explore_enabled=False))
    top = service.recommend(WARM, fallback_repo=repo).news_letter_ids[:2]
    repo.impress(WARM, top, NOW - timedelta(hours=5))
    repo.impress(WARM, top, NOW - timedelta(hours=3))
    _impress_three_times(repo, NOBODY, top)
    service.cache.clear()

    assert service.recommend(WARM, fallback_repo=repo).fatigued_count == 0


def test_fatigue_enforce_mode_removes_fatigued_items_from_the_list_and_from_the_exploration_pool():
    repo = _repo()
    log = LogRecorder()
    service = _service(repo, RecsysConfig(fatigue_mode="enforce"), log=log, rng_factory=_seeded(10))
    before = _det(service, repo, WARM)
    fatigued = before.ranked_ids[:2] + before.eligible_ids.tolist()[-2:]
    _impress_three_times(repo, WARM, fatigued)

    seen = set()
    for _ in range(60):
        rec = service.recommend(WARM, fallback_repo=repo)
        seen.update(rec.news_letter_ids)
        service.log_impressions(WARM, rec, rec.news_letter_ids)

    assert not seen & set(fatigued)  # 결정론 목록에도, 탐색으로도 나오지 않는다
    request = log.requests[0]
    assert (request["fatigue_mode"], request["fatigued_count"]) == ("enforce", 4)
    # 탐색 풀이 줄어든 만큼 propensity가 그 요청의 실제 풀 크기로 기록된다
    assert request["eligible_count"] == 26 and request["explore_pool_size"] == 26 - 18
    assert not set(request["candidate_ids"]) & set(fatigued)
    explored = [s for s in log.slots_of(request["request_id"]) if s["explored"]]
    assert all(s["propensity"] == pytest.approx((2 / 20) / 8) for s in explored)


def test_fatigue_off_does_not_query_the_impression_log():
    repo = _repo()
    service = _service(repo, RecsysConfig(fatigue_mode="off"))

    rec = service.recommend(WARM, fallback_repo=repo)

    assert "fatigued_among" not in repo.calls
    assert (rec.fatigue_mode, rec.fatigued_count) == ("off", None)


def test_a_failing_fatigue_lookup_does_not_fail_the_request_in_log_mode_but_does_in_enforce_mode():
    repo = _repo()
    repo.fail_on.add("fatigued_among")
    log_mode = _service(repo, RecsysConfig(fatigue_mode="log"))
    enforce_mode = _service(repo, RecsysConfig(fatigue_mode="enforce"))

    logged = log_mode.recommend(WARM, fallback_repo=repo)
    enforced = enforce_mode.recommend(WARM, fallback_repo=repo)

    assert logged.source == SOURCE_REALTIME and logged.fatigued_count is None
    assert log_mode.counters.get("fatigue.lookup_error") == 1
    assert (enforced.source, enforced.fallback_reason) == (SOURCE_POPULAR, "error")


def test_fatigue_applies_to_users_without_signal_too():
    repo = _repo()
    service = _service(repo, RecsysConfig(fatigue_mode="enforce", explore_enabled=False))
    top = service.recommend(NOBODY, fallback_repo=repo).news_letter_ids[:4]
    _impress_three_times(repo, NOBODY, top)
    service.cache.clear()

    rec = service.recommend(NOBODY, fallback_repo=repo)

    assert rec.source == SOURCE_COLD_POPULAR
    assert not set(rec.news_letter_ids) & set(top)
    assert rec.fatigued_count == 4


def test_invalid_exploration_or_fatigue_settings_fail_at_startup():
    for bad in ({"fatigue_mode": "on"}, {"explore_slots": -1}, {"explore_slots_cold": -2}, {"shadow_max": -1}):
        with pytest.raises(ValueError):
            RecsysConfig(**bad)
    env = {"RECSYS_EXPLORE_ENABLED": "false", "RECSYS_EXPLORE_SLOTS_COLD": "6", "RECSYS_FATIGUE_MODE": "enforce"}
    cfg = RecsysConfig.from_env(env)
    assert (cfg.explore_enabled, cfg.explore_slots_cold, cfg.fatigue_mode) == (False, 6, "enforce")
    assert cfg.explore_slots_for(cold=True) == 0
    assert RecsysConfig().explore_slots_for(cold=True) == 4 and RecsysConfig().explore_slots_for(cold=False) == 2


def test_a_persistently_failing_fatigue_lookup_logs_one_traceback_not_one_per_request(caplog):
    import logging

    repo = _repo()
    repo.fail_on.add("fatigued_among")
    service = _service(repo, RecsysConfig(fatigue_mode="log", cache_ttl_s=0))

    with caplog.at_level(logging.ERROR, logger="app.recsys.pipeline"):
        recs = [service.recommend(WARM, fallback_repo=repo) for _ in range(5)]

    assert all(rec.source == SOURCE_REALTIME for rec in recs)
    assert service.counters.get("fatigue.lookup_error") == 5  # 건수는 빠짐없이 센다
    assert len([r for r in caplog.records if r.exc_info and "fatigue lookup failed" in r.getMessage()]) == 1
