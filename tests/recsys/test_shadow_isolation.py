"""shadow 스코어러와 로그용 피처는 요청 경로 밖에서 돈다 (ADR 0033 - ADR 0025가 남긴 일).

지키려는 것: 느리거나 실패하는 shadow가 요청을 시간 초과 폴백으로 만들지 못한다. 실행기(app/recsys/shadow.py)의
시간 예산·버리기·밀림 상한·차단을 따로 보고, 서비스를 거쳐 응답과 로그가 어떻게 되는지를 본다.
"""
import threading
import time
from contextlib import contextmanager

import numpy as np
import pytest

from app.recsys.config import RecsysConfig
from app.recsys.metrics import RecsysCounters
from app.recsys.scoring import HeuristicScorer, ScorerStack
from app.recsys.service import build_service
from app.recsys.shadow import DeferredResult, ShadowRunner
from app.recsys.types import SOURCE_REALTIME, ScoreResult
from tests.recsys.fakes import NOW, FakeRepo, FakeUser, LogRecorder, axis_vec, two_topic_corpus

DIM = 16
WARM = 1
# 결과가 로그에 남는 것을 보는 테스트는 로그 쓰기의 대기 상한(기본 100ms)을 넉넉히 준다: 느린 기계에서 전용
# 스레드가 늦게 깨어나도 결과가 버려지지 않게. 상한 자체는 따로 본다.
WAIT_FOR_RESULT_MS = 5000


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


@pytest.fixture
def runners():
    made = []

    def make(**kw):
        runner = ShadowRunner(**kw)
        made.append(runner)
        return runner

    yield make
    for runner in made:
        runner.shutdown()


# ----------------------------------------------------------------------------- 실행기
def test_submit_returns_before_the_job_finishes_and_wait_hands_back_its_result(runners):
    runner = runners(budget_s=5.0)
    release = threading.Event()

    def job(expired):
        release.wait(5)
        return DeferredResult(extra_scores={"m": np.array([1.0, 2.0])})

    started = time.monotonic()
    handle = runner.submit(job)
    assert time.monotonic() - started < 0.2 and handle is not None and not handle.done()
    assert runner.pending == 1

    release.set()
    result = handle.wait()

    assert result.extra_scores["m"].tolist() == [1.0, 2.0]
    assert _wait_until(lambda: runner.pending == 0)
    assert runner.counters.get("shadow.submitted") == 1 and runner.counters.get("shadow.timeout") == 0


def test_wait_gives_up_when_the_jobs_own_budget_runs_out_and_never_waits_for_it_again(runners):
    runner = runners(budget_s=0.05)
    release = threading.Event()
    saw_expired = []

    def stuck(expired):
        release.wait(5)
        saw_expired.append(expired())
        return DeferredResult()

    handle = runner.submit(stuck)
    started = time.monotonic()
    first = handle.wait()
    waited = time.monotonic() - started
    started = time.monotonic()
    second = handle.wait()
    waited_again = time.monotonic() - started

    assert first is None and second is None
    assert 0.03 < waited < 1.0 and waited_again < 0.02
    assert runner.counters.get("shadow.timeout") == 1  # 같은 작업을 두 번 세지 않는다
    release.set()
    assert _wait_until(lambda: saw_expired == [True])  # 돌던 작업은 다음 단계에서 버려진 것을 안다


def test_a_capped_wait_gives_up_at_the_cap_even_though_the_jobs_budget_remains(runners):
    """기다리는 쪽(응답 뒤의 로그 쓰기)은 요청을 받는 스레드 풀의 자리를 쓴다. 작업의 예산이 5초 남았어도 자기
    상한만큼만 기다리고, 그 작업은 버린다(다음에 다시 기다리지 않는다)."""
    runner = runners(budget_s=5.0)
    release = threading.Event()
    saw_expired = []

    def stuck(expired):
        release.wait(5)
        saw_expired.append(expired())
        return DeferredResult()

    handle = runner.submit(stuck)
    started = time.monotonic()
    first = handle.wait(max_wait_s=0.05)
    waited = time.monotonic() - started
    started = time.monotonic()
    second = handle.wait(max_wait_s=0.05)
    waited_again = time.monotonic() - started

    assert first is None and second is None
    assert 0.03 < waited < 1.0 and waited_again < 0.02
    assert runner.counters.get("shadow.timeout") == 1 and runner.counters.get("shadow.log_wait_exceeded") == 1
    release.set()
    assert _wait_until(lambda: saw_expired == [True])  # 버려진 작업은 다음 단계에서 멈춘다


def test_a_capped_wait_returns_a_result_that_arrives_within_the_cap(runners):
    runner = runners(budget_s=5.0)
    release = threading.Event()

    def job(expired):
        release.wait(5)
        return DeferredResult(extra_scores={"m": np.array([1.0, 2.0])})

    handle = runner.submit(job)
    threading.Timer(0.03, release.set).start()
    result = handle.wait(max_wait_s=3.0)

    assert result is not None and result.extra_scores["m"].tolist() == [1.0, 2.0]
    assert runner.counters.get("shadow.timeout") == 0 and runner.counters.get("shadow.log_wait_exceeded") == 0


def test_a_budget_that_runs_out_before_the_cap_is_a_budget_timeout_not_a_log_wait_one(runners):
    runner = runners(budget_s=0.05)
    release = threading.Event()
    handle = runner.submit(lambda expired: release.wait(5) and DeferredResult())

    started = time.monotonic()
    assert handle.wait(max_wait_s=3.0) is None

    assert time.monotonic() - started < 1.0
    assert runner.counters.get("shadow.timeout") == 1 and runner.counters.get("shadow.log_wait_exceeded") == 0
    release.set()


def test_capped_waits_that_keep_giving_up_open_the_breaker_so_later_log_writes_do_not_wait_at_all(runners):
    """작업이 계속 상한보다 늦으면 받지 않게 된다: 로그 쓰기마다 상한만큼 스레드를 붙잡는 일이 이어지지 않는다."""
    now = [0.0]
    runner = runners(workers=1, max_pending=50, budget_s=10.0, breaker_after=3, cooldown_s=30.0, clock=lambda: now[0])
    release = threading.Event()
    handles = [runner.submit(lambda expired: release.wait(5) and DeferredResult()) for _ in range(3)]

    assert [h.wait(max_wait_s=0.01) for h in handles] == [None, None, None]

    assert runner.counters.get("shadow.log_wait_exceeded") == 3 and runner.counters.get("shadow.breaker_open") == 1
    assert runner.submit(lambda expired: DeferredResult()) is None
    release.set()


def test_a_job_that_was_still_queued_when_abandoned_never_starts(runners):
    runner = runners(workers=1, budget_s=0.05)
    release = threading.Event()
    ran = []
    runner.submit(lambda expired: release.wait(5) and DeferredResult())  # 스레드 하나를 붙잡는다
    queued = runner.submit(lambda expired: ran.append("queued") or DeferredResult())

    assert queued.wait() is None

    release.set()
    assert _wait_until(lambda: runner.pending == 0)
    assert ran == []


def test_a_job_whose_budget_passed_while_queued_is_dropped_without_running(runners):
    runner = runners(workers=1, budget_s=0.03)
    release = threading.Event()
    ran = []
    runner.submit(lambda expired: release.wait(5) and DeferredResult())
    runner.submit(lambda expired: ran.append("late") or DeferredResult())  # 아무도 기다리지 않는 작업

    time.sleep(0.08)
    release.set()

    assert _wait_until(lambda: runner.pending == 0)
    assert ran == [] and runner.counters.get("shadow.expired") >= 1


def test_the_backlog_is_bounded(runners):
    runner = runners(workers=1, max_pending=3, budget_s=5.0)
    release = threading.Event()
    handles = [runner.submit(lambda expired: release.wait(5) and DeferredResult()) for _ in range(5)]

    assert [h is not None for h in handles] == [True, True, True, False, False]
    assert runner.counters.get("shadow.rejected") == 2
    release.set()
    assert _wait_until(lambda: runner.pending == 0)
    assert runner.submit(lambda expired: DeferredResult()) is not None  # 비면 다시 받는다


def test_repeated_timeouts_open_a_breaker_for_the_cooldown(runners):
    now = [0.0]
    runner = runners(workers=1, max_pending=50, budget_s=10.0, breaker_after=3, cooldown_s=30.0,
                     clock=lambda: now[0])
    release = threading.Event()
    handles = [runner.submit(lambda expired: release.wait(5) and DeferredResult()) for _ in range(3)]
    now[0] = 11.0  # 세 작업 모두 예산(10초)을 넘겼다: 하나는 갇혀 있고 둘은 그 뒤에 줄을 서 있다

    assert [h.wait() for h in handles] == [None, None, None]

    assert runner.counters.get("shadow.timeout") == 3 and runner.counters.get("shadow.breaker_open") == 1
    assert runner.submit(lambda expired: DeferredResult()) is None  # 차단 중: 받지 않는다
    now[0] = 40.9
    assert runner.submit(lambda expired: DeferredResult()) is None
    now[0] = 41.1
    release.set()
    assert _wait_until(lambda: runner.pending == 0)
    assert runner.submit(lambda expired: DeferredResult()) is not None  # 식은 뒤에는 다시 받는다
    assert runner.counters.get("shadow.rejected") == 2


def test_a_failing_job_is_counted_and_reads_as_no_result(runners):
    runner = runners(budget_s=2.0)

    def boom(expired):
        raise RuntimeError("shadow job exploded")

    handle = runner.submit(boom)

    assert handle.wait() is None
    assert runner.counters.get("shadow.job_error") == 1 and runner.counters.get("shadow.timeout") == 0
    assert _wait_until(lambda: runner.pending == 0)


def test_a_shut_down_runner_rejects_instead_of_raising():
    runner = ShadowRunner()
    runner.shutdown()

    assert runner.submit(lambda expired: DeferredResult()) is None
    assert runner.counters.get("shadow.rejected") == 1 and runner.pending == 0


# ----------------------------------------------------------------------------- 서비스를 거쳐서
class Shadow:
    def __init__(self, version="shadow-a", sleep_s=0.0, fail=False, gate=None):
        self.version, self.sleep_s, self.fail, self.gate = version, sleep_s, fail, gate
        self.calls = 0

    def score(self, state, items, now):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.fail:
            raise RuntimeError("shadow model exploded")
        return ScoreResult(scores=-HeuristicScorer().score(state, items, now).scores, model_version=self.version)


def _repo():
    return FakeRepo(two_topic_corpus(dim=DIM, per_topic=15), [FakeUser(WARM, long_term=axis_vec(DIM, 0))])


def _service(repo, scorer, log=None, **cfg):
    @contextmanager
    def factory():
        yield repo

    rng = np.random.default_rng(3)
    return build_service(
        RecsysConfig(**cfg), repo_factory=factory, scorer=scorer, now_fn=lambda: NOW, impression_writer=log,
        rng_factory=lambda request_id: rng,
    )


def test_a_shadow_slower_than_the_request_budget_does_not_turn_the_request_into_a_fallback(runners):
    """shadow가 2초 걸리는데 요청 예산은 0.3초다. 요청 경로에서 돌리던 때에는 이 요청이 timeout 폴백이었다."""
    counters = RecsysCounters()
    release = threading.Event()
    shadow = Shadow(gate=release)
    runner = runners(budget_s=0.1, counters=counters)
    repo, log = _repo(), LogRecorder()
    service = _service(repo, ScorerStack(HeuristicScorer(), [shadow], counters=counters, runner=runner), log,
                       time_budget_ms=300)
    plain = _service(_repo(), HeuristicScorer(), time_budget_ms=300)
    try:
        started = time.monotonic()
        rec = service.recommend(WARM, fallback_repo=repo)
        elapsed = time.monotonic() - started
        expected = plain.recommend(WARM, fallback_repo=_repo())

        assert rec.source == SOURCE_REALTIME and rec.fallback_reason is None
        assert elapsed < 0.3
        assert rec.news_letter_ids == expected.news_letter_ids and rec.scores == expected.scores
        assert service.counters.get("fallback.timeout") == 0

        # 로그 쓰기(응답 뒤)는 shadow의 남은 예산만큼만 기다리고 shadow 점수 없이 쓴다
        started = time.monotonic()
        service.log_impressions(WARM, rec, rec.news_letter_ids)
        log_wait = time.monotonic() - started
        assert log_wait < 0.5
        assert log.requests[0]["shadow_versions"] is None
        assert all(s["scores_shadow"] is None for s in log.slots)
        assert all(s["features"] is not None for s in log.slots)  # 활성 휴리스틱의 피처는 남는다
        assert counters.get("shadow.timeout") == 1

        # 같은 결정론 목록을 쓰는 다음 요청(캐시 적중)은 버린 작업을 다시 기다리지 않는다
        again = service.recommend(WARM, fallback_repo=repo)
        started = time.monotonic()
        service.log_impressions(WARM, again, again.news_letter_ids)
        assert again.cache_hit and time.monotonic() - started < 0.05
        assert counters.get("shadow.timeout") == 1 and shadow.calls == 1
    finally:
        release.set()
        service.shutdown()
        plain.shutdown()


def test_the_log_writer_waits_for_shadow_results_no_longer_than_its_own_cap(runners):
    """응답 뒤의 로그 쓰기는 요청을 받는 스레드 풀(Starlette의 BackgroundTask)에서 돈다. shadow의 예산(5초)이 남아
    있어도 로그 쓰기는 RECSYS_SHADOW_LOG_WAIT_MS만큼만 기다리고, 요청·칸 로그는 shadow 점수 없이 쓴다."""
    counters = RecsysCounters()
    release = threading.Event()
    shadow = Shadow(gate=release)
    runner = runners(budget_s=5.0, counters=counters)
    repo, log = _repo(), LogRecorder()
    service = _service(repo, ScorerStack(HeuristicScorer(), [shadow], counters=counters, runner=runner), log,
                       shadow_log_wait_ms=50)
    try:
        rec = service.recommend(WARM, fallback_repo=repo)
        started = time.monotonic()
        service.log_impressions(WARM, rec, rec.news_letter_ids)
        waited = time.monotonic() - started

        assert 0.03 < waited < 1.0  # 예산 5초가 아니라 상한 0.05초 근처
        assert len(log.requests) == 1 and len(log.slots) == 20
        assert log.requests[0]["shadow_versions"] is None and all(s["scores_shadow"] is None for s in log.slots)
        assert all(s["features"] is not None for s in log.slots)  # 요청 경로에서 나온 활성 휴리스틱의 피처
        assert counters.get("shadow.log_wait_exceeded") == 1 and counters.get("shadow.timeout") == 1

        # 같은 목록의 다음 요청(캐시 적중)은 버린 작업을 다시 기다리지 않는다
        again = service.recommend(WARM, fallback_repo=repo)
        started = time.monotonic()
        service.log_impressions(WARM, again, again.news_letter_ids)
        assert again.cache_hit and time.monotonic() - started < 0.03
        assert counters.get("shadow.log_wait_exceeded") == 1
    finally:
        release.set()
        service.shutdown()


def test_the_log_wait_cap_is_a_setting_with_a_default_well_below_the_shadow_budget():
    cfg = RecsysConfig()

    assert cfg.shadow_log_wait_ms == 100 and cfg.shadow_log_wait_ms < cfg.shadow_budget_ms == 500
    assert RecsysConfig.from_env({"RECSYS_SHADOW_LOG_WAIT_MS": "20"}).shadow_log_wait_ms == 20
    assert RecsysConfig(shadow_log_wait_ms=0).shadow_log_wait_ms == 0  # 0: 이미 끝난 결과만 쓴다
    with pytest.raises(ValueError, match="SHADOW_LOG_WAIT_MS"):
        RecsysConfig(shadow_log_wait_ms=-1)


def test_a_failing_shadow_changes_neither_the_response_nor_the_fallback_counters(runners):
    counters = RecsysCounters()
    runner = runners(budget_s=2.0, counters=counters)
    repo, log = _repo(), LogRecorder()
    service = _service(repo, ScorerStack(HeuristicScorer(), [Shadow(fail=True)], counters=counters, runner=runner), log,
                       shadow_log_wait_ms=WAIT_FOR_RESULT_MS)
    plain = _service(_repo(), HeuristicScorer())
    try:
        rec = service.recommend(WARM, fallback_repo=repo)
        expected = plain.recommend(WARM, fallback_repo=_repo())
        service.log_impressions(WARM, rec, rec.news_letter_ids)

        assert rec.source == SOURCE_REALTIME and rec.news_letter_ids == expected.news_letter_ids
        assert counters.get("shadow.error") == 1 and service.counters.get("fallback.error") == 0
        assert log.requests[0]["shadow_versions"] is None and all(s["scores_shadow"] is None for s in log.slots)
    finally:
        service.shutdown()
        plain.shutdown()


def test_a_shadow_scored_off_the_request_path_is_logged_for_every_slot_and_reused_from_the_cache(runners):
    counters = RecsysCounters()
    shadow = Shadow()
    runner = runners(budget_s=5.0, counters=counters)
    repo, log = _repo(), LogRecorder()
    service = _service(repo, ScorerStack(HeuristicScorer(), [shadow], counters=counters, runner=runner), log,
                       shadow_log_wait_ms=WAIT_FOR_RESULT_MS)
    try:
        first = service.recommend(WARM, fallback_repo=repo)
        second = service.recommend(WARM, fallback_repo=repo)
        for rec in (first, second):
            service.log_impressions(WARM, rec, rec.news_letter_ids)

        assert not first.cache_hit and second.cache_hit and shadow.calls == 1
        assert first.shadow_versions == []  # 응답 시점에는 shadow 결과가 아직 요청 경로에 없다
        assert [r["shadow_versions"] for r in log.requests] == [["shadow-a"], ["shadow-a"]]
        assert len(log.slots) == 40
        for slot in log.slots:  # 탐색 칸 포함 모든 칸
            assert slot["scores_shadow"] == {"shadow-a": pytest.approx(-slot["score"])}
        assert counters.get("shadow.scored") == 1 and counters.get("shadow.timeout") == 0
    finally:
        service.shutdown()


def test_rejected_shadow_work_leaves_the_response_and_the_log_intact(runners):
    counters = RecsysCounters()
    runner = runners(workers=1, max_pending=1, budget_s=5.0, counters=counters)
    release = threading.Event()
    runner.submit(lambda expired: release.wait(10) and DeferredResult())  # 밀림 상한을 채운다
    repo, log = _repo(), LogRecorder()
    service = _service(repo, ScorerStack(HeuristicScorer(), [Shadow()], counters=counters, runner=runner), log)
    try:
        rec = service.recommend(WARM, fallback_repo=repo)
        service.log_impressions(WARM, rec, rec.news_letter_ids)

        assert rec.source == SOURCE_REALTIME and rec.deferred is None
        assert counters.get("shadow.rejected") == 1
        assert len(log.slots) == 20 and all(s["scores_shadow"] is None for s in log.slots)
    finally:
        release.set()
        service.shutdown()


def test_shadow_work_is_shed_when_the_request_has_already_used_most_of_its_budget(runners):
    """활성 점수를 낸 시점에 남은 예산이 절반 미만이면 프로세스가 바쁜 것이다: 전용 스레드에 일을 더 얹지 않는다."""
    from datetime import timedelta

    from app.recsys.deadline import Deadline
    from app.recsys.types import Item, UserState

    counters = RecsysCounters()
    runner = runners(budget_s=5.0, counters=counters)
    shadow = Shadow()
    stack = ScorerStack(HeuristicScorer(), [shadow], deadline_fraction=0.5, counters=counters, runner=runner)
    items = [Item(1, axis_vec(DIM, 0), NOW - timedelta(hours=1), 1), Item(2, axis_vec(DIM, 1), NOW, 1)]
    state = UserState(user_id=1, profile=axis_vec(DIM, 1))
    clock = [0.0]

    def score_at(elapsed):
        deadline = Deadline(0.3, clock=lambda: clock[0])
        clock[0] += elapsed
        return stack.score(state, items, NOW, deadline)

    busy = score_at(0.2)    # 예산 0.3초 중 0.2초를 이미 썼다
    relaxed = score_at(0.05)

    assert busy.deferred is None and counters.get("shadow.shed") == 1
    assert relaxed.deferred is not None and relaxed.deferred.wait().extra_scores.keys() == {"shadow-a"}
    assert shadow.calls == 1 and busy.scores.tolist() == relaxed.scores.tolist()


def test_for_contrast_a_slow_shadow_run_on_the_request_path_does_cost_the_request_its_budget():
    """전용 스레드 없이 요청 경로에서 바로 도는 배선(runner=None)의 한계를 고정해 둔다: 운영 배선이
    전용 스레드를 쓰는 이유다. 건너뛰기는 shadow를 시작하기 전에만 판단할 수 있다."""
    repo = _repo()
    service = _service(repo, ScorerStack(HeuristicScorer(), [Shadow(sleep_s=0.6)]), time_budget_ms=200)
    try:
        rec = service.recommend(WARM, fallback_repo=repo)

        assert rec.fallback_reason == "timeout" and rec.source != SOURCE_REALTIME
    finally:
        service.shutdown()
