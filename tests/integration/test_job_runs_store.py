"""jobs/store.py::PostgresJobRunStore + jobs/runtime.py::run_job을 실제 Postgres에서 검증한다.

잡 이름에 uuid를 붙여 실제 스케줄 잡(ingest 등)과 락 키/행이 겹치지 않게 한다.
"""
import argparse
import time
import uuid

import psycopg2
import pytest

from jobs.runtime import JobSkipped, run_job
from jobs.store import PostgresJobRunStore


def _store(database_url):
    return PostgresJobRunStore(psycopg2.connect(database_url))


def _job_name():
    return f"itest-{uuid.uuid4().hex[:10]}"


def _rows(pg_conn, job):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT status, stats, error, git_sha, finished_at IS NOT NULL FROM job_runs WHERE job = %s ORDER BY id",
            (job,),
        )
        return cur.fetchall()


@pytest.fixture
def cleanup_jobs(pg_conn):
    names = []
    yield names
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM job_runs WHERE job = ANY(%s)", (names,))


def test_advisory_lock_is_exclusive_across_sessions_and_released_on_disconnect(database_url):
    job = _job_name()
    first, second = _store(database_url), _store(database_url)
    try:
        assert first.try_lock(job) is True
        assert second.try_lock(job) is False
        # 프로세스가 죽어 연결이 끊긴 상황 - 세션 락은 자동으로 풀려야 한다.
        # 서버 쪽 백엔드 종료는 클라이언트 close()보다 약간 늦게 끝나므로 잠깐 기다린다.
        first.close()
        deadline = time.monotonic() + 5
        acquired = second.try_lock(job)
        while not acquired and time.monotonic() < deadline:
            time.sleep(0.05)
            acquired = second.try_lock(job)
        assert acquired is True
        second.unlock(job)
    finally:
        for store in (first, second):
            if not store.conn.closed:
                store.close()


def test_run_job_records_succeeded_row_with_jsonb_stats(database_url, pg_conn, cleanup_jobs):
    job = _job_name()
    cleanup_jobs.append(job)

    def fn(ctx):
        ctx.stats["rss"] = {"inserted": 3, "per_feed": {"동아일보": {"inserted": 3}}}
        return {"embedded": 2}

    result = run_job(job, fn, args=argparse.Namespace(), store_factory=lambda: _store(database_url),
                     notify=lambda m: None, git_sha="deadbeef")

    assert result.status == "succeeded"
    [(status, stats, error, git_sha, finished)] = _rows(pg_conn, job)
    assert status == "succeeded" and error is None and git_sha == "deadbeef" and finished
    assert stats["rss"]["per_feed"]["동아일보"]["inserted"] == 3
    assert stats["embedded"] == 2


def test_run_job_skips_while_another_session_holds_the_lock(database_url, pg_conn, cleanup_jobs):
    job = _job_name()
    cleanup_jobs.append(job)
    holder = _store(database_url)
    called = []
    try:
        assert holder.try_lock(job)
        result = run_job(job, lambda ctx: called.append(1), args=argparse.Namespace(),
                         store_factory=lambda: _store(database_url), notify=lambda m: None)
    finally:
        holder.close()

    assert called == []
    assert result.status == "skipped" and result.exit_code == 0
    [(status, stats, _, _, finished)] = _rows(pg_conn, job)
    assert status == "skipped" and stats["reason"] == "lock_held" and finished


def test_failed_and_skipped_runs_are_recorded(database_url, pg_conn, cleanup_jobs):
    job = _job_name()
    cleanup_jobs.append(job)
    notified = []

    def failing(ctx):
        ctx.stats["partial"] = 1
        raise RuntimeError("boom")

    def skipping(ctx):
        raise JobSkipped("llm_kill_switch", {"kill_switch": "env"})

    failed = run_job(job, failing, args=argparse.Namespace(), store_factory=lambda: _store(database_url),
                     notify=notified.append)
    skipped = run_job(job, skipping, args=argparse.Namespace(), store_factory=lambda: _store(database_url),
                      notify=notified.append)

    assert (failed.exit_code, skipped.exit_code) == (1, 0)
    (f_status, f_stats, f_error, _, _), (s_status, s_stats, _, _, _) = _rows(pg_conn, job)
    assert f_status == "failed" and f_stats["partial"] == 1 and "boom" in f_error
    assert s_status == "skipped" and s_stats["reason"] == "llm_kill_switch"
    assert len(notified) == 1


def test_running_row_left_by_a_dead_process_is_marked_abandoned(database_url, pg_conn, cleanup_jobs):
    job = _job_name()
    cleanup_jobs.append(job)
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO job_runs (job, status) VALUES (%s, 'running')", (job,))

    notified = []
    result = run_job(job, lambda ctx: {}, args=argparse.Namespace(),
                     store_factory=lambda: _store(database_url), notify=notified.append)

    (stale_status, _, stale_error, _, stale_finished), (new_status, new_stats, _, _, _) = _rows(pg_conn, job)
    assert stale_status == "abandoned" and stale_finished and "without recording" in stale_error
    assert new_status == "succeeded" and new_stats["abandoned_previous_runs"] == 1
    assert result.status == "succeeded"
    assert len(notified) == 1 and "abandoned" in notified[0]


def test_checkpoint_is_visible_to_other_sessions_while_running_and_survives_a_kill(
    database_url, pg_conn, cleanup_jobs
):
    """SIGKILL/OOM으로 죽으면 finish가 불리지 않는다 - 배치마다 쓴 중간 stats가 남아 있어야
    다음 실행이 abandoned로 바꾼 뒤에도 어디까지 했는지 알 수 있다."""
    job = _job_name()
    cleanup_jobs.append(job)
    seen = {}

    class Killed(BaseException):
        pass

    def fn(ctx):
        ctx.stats["embed"] = {"embedded": 40, "remaining": 60}
        ctx.checkpoint()
        [(status, stats, _, _, _)] = _rows(pg_conn, job)
        seen.update(status=status, stats=stats)
        raise Killed()  # finish 전에 프로세스가 사라진 상황 흉내

    store = _store(database_url)
    with pytest.raises(Killed):
        run_job(job, fn, args=argparse.Namespace(), store_factory=lambda: store, notify=lambda m: None)

    assert seen == {"status": "running", "stats": {"embed": {"embedded": 40, "remaining": 60}}}
    run_job(job, lambda ctx: {}, args=argparse.Namespace(),
            store_factory=lambda: _store(database_url), notify=lambda m: None)
    (killed_status, killed_stats, _, _, _), _ = _rows(pg_conn, job)
    assert killed_status == "abandoned" and killed_stats["embed"]["embedded"] == 40


def test_job_runs_status_check_constraint_rejects_unknown_status(pg_conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute("INSERT INTO job_runs (job, status) VALUES ('itest-bad', 'done')")
