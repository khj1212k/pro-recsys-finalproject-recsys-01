"""jobs/runtime.py::run_job의 실행 기록/락/알림 오케스트레이션 단위 테스트.

실제 Postgres 동작(advisory lock 경합, job_runs 행)은
tests/integration/test_job_runs_store.py가 검증한다. 여기서는 인메모리 store로
상태 전이와 종료 코드, 알림, 락 해제만 본다.
"""
import argparse

import pytest

from jobs.runtime import JobSkipped, advisory_lock_key, run_job


class FakeStore:
    def __init__(self, lock_available=True, stale_running=0, fail_on=None):
        self.lock_available = lock_available
        self.stale_running = stale_running
        self.fail_on = fail_on or set()
        self.rows = {}
        self.next_id = 1
        self.locked = []
        self.unlocked = []
        self.closed = False

    def _maybe_fail(self, op):
        if op in self.fail_on:
            raise RuntimeError(f"store failure in {op}")

    def try_lock(self, job):
        self._maybe_fail("try_lock")
        if self.lock_available:
            self.locked.append(job)
        return self.lock_available

    def unlock(self, job):
        self.unlocked.append(job)

    def abandon_stale(self, job):
        self._maybe_fail("abandon_stale")
        return self.stale_running

    def start(self, job, git_sha):
        self._maybe_fail("start")
        run_id = self.next_id
        self.next_id += 1
        self.rows[run_id] = {"job": job, "status": "running", "git_sha": git_sha, "stats": {}, "error": None}
        return run_id

    def finish(self, run_id, status, stats, error):
        self._maybe_fail("finish")
        self.rows[run_id].update(status=status, stats=stats, error=error)

    def record(self, job, status, stats, git_sha, error=None):
        run_id = self.next_id
        self.next_id += 1
        self.rows[run_id] = {"job": job, "status": status, "git_sha": git_sha, "stats": stats, "error": error}
        return run_id

    def close(self):
        self.closed = True


def _run(fn, store, notified=None, job="ingest"):
    notified = notified if notified is not None else []
    return run_job(
        job,
        fn,
        args=argparse.Namespace(),
        store_factory=lambda: store,
        notify=notified.append,
        git_sha="abc1234",
    )


def test_advisory_lock_key_is_stable_signed_64bit_and_distinct_per_job():
    key = advisory_lock_key("ingest")
    assert key == advisory_lock_key("ingest")
    assert -(2**63) <= key < 2**63
    assert key != advisory_lock_key("popularity")


def test_success_records_running_then_succeeded_with_stats_and_releases_lock():
    store = FakeStore()
    notified = []

    def job(ctx):
        ctx.stats["rss"] = {"inserted": 3}
        return {"embedded": 2}

    result = _run(job, store, notified)

    assert result.exit_code == 0
    assert result.status == "succeeded"
    row = store.rows[result.run_id]
    assert row["status"] == "succeeded"
    assert row["git_sha"] == "abc1234"
    assert row["stats"]["rss"] == {"inserted": 3}
    assert row["stats"]["embedded"] == 2
    assert row["stats"]["duration_s"] >= 0
    assert store.unlocked == ["ingest"]
    assert store.closed
    assert notified == []


def test_failure_keeps_partial_stats_records_error_notifies_and_exits_nonzero():
    store = FakeStore()
    notified = []

    def job(ctx):
        ctx.stats["rss"] = {"inserted": 5}
        raise ValueError("embedding model missing")

    result = _run(job, store, notified)

    assert result.exit_code != 0
    assert result.status == "failed"
    row = store.rows[result.run_id]
    assert row["status"] == "failed"
    assert row["stats"]["rss"] == {"inserted": 5}
    assert "embedding model missing" in row["error"]
    assert len(notified) == 1
    assert "ingest" in notified[0] and "embedding model missing" in notified[0]
    assert store.unlocked == ["ingest"]


def test_lock_held_by_another_run_records_skipped_without_running_job():
    store = FakeStore(lock_available=False)
    called = []

    result = _run(lambda ctx: called.append(1), store)

    assert called == []
    assert result.exit_code == 0
    assert result.status == "skipped"
    (row,) = store.rows.values()
    assert row["status"] == "skipped"
    assert row["stats"]["reason"] == "lock_held"
    assert store.unlocked == []


def test_job_skipped_exception_is_a_clean_skip_not_a_failure():
    store = FakeStore()
    notified = []

    def job(ctx):
        ctx.stats["checked"] = True
        raise JobSkipped("llm_kill_switch", {"kill_switch": "file /ops/LLM_KILL_SWITCH"})

    result = _run(job, store, notified, job="generate")

    assert result.exit_code == 0
    assert result.status == "skipped"
    row = store.rows[result.run_id]
    assert row["stats"]["reason"] == "llm_kill_switch"
    assert row["stats"]["kill_switch"] == "file /ops/LLM_KILL_SWITCH"
    assert row["stats"]["checked"] is True
    assert notified == []


def test_stale_running_rows_are_reported_after_taking_the_lock():
    store = FakeStore(stale_running=2)

    result = _run(lambda ctx: {}, store)

    assert store.rows[result.run_id]["stats"]["abandoned_previous_runs"] == 2


def test_database_unreachable_notifies_and_exits_nonzero():
    notified = []

    def broken_factory():
        raise ConnectionError("could not connect to server")

    result = run_job(
        "ingest",
        lambda ctx: {},
        args=argparse.Namespace(),
        store_factory=broken_factory,
        notify=notified.append,
        git_sha=None,
    )

    assert result.exit_code != 0
    assert result.status == "failed"
    assert len(notified) == 1
    assert "could not connect" in notified[0]


def test_bookkeeping_failure_after_job_ran_still_unlocks_and_fails():
    store = FakeStore(fail_on={"finish"})
    notified = []

    result = _run(lambda ctx: {"ok": 1}, store, notified)

    assert result.exit_code != 0
    assert store.unlocked == ["ingest"]
    assert store.closed
    assert len(notified) == 1


def test_non_json_native_stats_values_are_normalized():
    np = pytest.importorskip("numpy")
    store = FakeStore()

    result = _run(lambda ctx: {"n": np.int64(7), "ratio": np.float32(0.5), "arr": np.arange(2)}, store)

    stats = store.rows[result.run_id]["stats"]
    assert stats["n"] == 7 and type(stats["n"]) is int
    assert stats["ratio"] == 0.5 and type(stats["ratio"]) is float
    assert stats["arr"] == [0, 1]


def test_termination_signal_is_recorded_as_failed_with_partial_stats_and_unlocks():
    from jobs.runtime import JobTerminated

    store = FakeStore()
    notified = []

    def job(ctx):
        ctx.stats["embed"] = {"embedded": 40}
        raise JobTerminated(15)

    result = _run(job, store, notified, job="embed")

    assert result.status == "failed"
    assert result.exit_code != 0
    row = store.rows[result.run_id]
    assert row["stats"]["embed"] == {"embedded": 40}
    assert "SIGTERM" in row["error"]
    assert store.unlocked == ["embed"]
    assert len(notified) == 1 and "SIGTERM" in notified[0]


def test_termination_is_not_swallowed_by_generic_exception_handlers():
    """잡 코드 곳곳의 `except Exception`(예: 배치 실패 후 계속)이 중단 신호를 삼키면
    SIGTERM을 받고도 계속 돈다 - JobTerminated는 Exception이 아니어야 한다."""
    from jobs.runtime import JobTerminated

    assert not issubclass(JobTerminated, Exception)


def test_sigterm_handler_raises_job_terminated():
    import signal

    from jobs.run import _raise_terminated
    from jobs.runtime import JobTerminated

    with pytest.raises(JobTerminated) as exc:
        _raise_terminated(signal.SIGTERM, None)
    assert exc.value.signum == signal.SIGTERM
