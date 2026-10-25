"""jobs/tasks/rss_retry.py - 직전 ingest가 읽지 못한 RSS 피드만 다시 읽는 잡(ADR 0026 2026-10-25 갱신 2). DB·네트워크 없이.

실제 advisory lock 경합과 job_runs 조회는 Postgres가 있어야 보인다 - 여기서는 락 호출 순서(어느 이름으로 잡고 언제 푸는가)와
조회 SQL의 모양, 재시도 대상 선택, 요청이 0인 경로, 통계 키를 본다.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobs.run import JOBS, build_parser
from jobs.runtime import JobContext, JobSkipped, run_job
from jobs.schedule import parse_crontab
from jobs.tasks import rss_retry

REPO = Path(__file__).resolve().parents[1]

CONFIGURED = {
    "동아일보": ("rss_summary", "https://rss.donga.com/total.xml"),
    "경향신문": ("direct", "https://www.khan.co.kr/rss/rssdata/total_news.xml"),
    "국민일보": ("direct", "https://www.kmib.co.kr/rss/data/kmibRssAll.xml"),
    "국민일보_정치": ("direct", "https://www.kmib.co.kr/rss/data/kmibPolRss.xml"),
    "전자신문_IT": ("direct", "http://rss.etnews.com/03.xml"),
}
RESET = "RemoteDisconnected: Remote end closed connection without response"


def _feed(error=None, **extra):
    return {"press": "x", "entries": 50, "inserted": 0, "skipped": 0, "too_old": 0, "bad_date": 0, "error": error, **extra}


def _source(per_feed, run_id=1371, already_retried=False, status="succeeded"):
    return {"run_id": run_id, "status": status, "started_at": datetime(2026, 10, 25, 5, 5, 1, tzinfo=timezone.utc),
            "age_min": 35.04, "per_feed": per_feed, "already_retried": already_retried}


# ----------------------------------------------------------------------------- 재시도 대상 선택


def test_only_feeds_with_an_error_are_selected_in_configured_order():
    per_feed = {"전자신문_IT": _feed("feed unreadable (HTTP 503): x"), "경향신문": _feed(), "동아일보": _feed(RESET)}

    assert rss_retry.select_retry_feeds(per_feed, CONFIGURED) == ["동아일보", "전자신문_IT"]


def test_nothing_is_selected_when_no_feed_failed_or_there_are_no_stats():
    assert rss_retry.select_retry_feeds({"동아일보": _feed(), "경향신문": _feed()}, CONFIGURED) == []
    assert rss_retry.select_retry_feeds({}, CONFIGURED) == []
    assert rss_retry.select_retry_feeds(None, CONFIGURED) == []
    assert rss_retry.select_retry_feeds({"동아일보": None}, CONFIGURED) == []  # 통계가 비어 있는 피드


def test_feeds_skipped_for_their_host_are_selected_too_and_feeds_dropped_from_the_config_are_not():
    per_feed = {
        "국민일보": _feed(RESET),
        "국민일보_정치": _feed("skipped: www.kmib.co.kr unreadable earlier in this run"),
        "없어진피드": _feed(RESET),  # 그 사이 설정에서 빠졌다
    }

    assert rss_retry.select_retry_feeds(per_feed, CONFIGURED) == ["국민일보", "국민일보_정치"]


def test_a_missing_press_row_is_not_retried_because_no_request_would_change_it():
    per_feed = {"동아일보": _feed("press_id not found"), "경향신문": _feed(RESET)}

    assert rss_retry.select_retry_feeds(per_feed, CONFIGURED) == ["경향신문"]


# ----------------------------------------------------------------------------- 잡 실행


def _ctx(argv=()):
    checkpoints = []
    ctx = JobContext(job="rss_retry", args=build_parser().parse_args(["rss_retry", *argv]))
    ctx._checkpoint = lambda: checkpoints.append(dict(ctx.stats))
    return ctx, checkpoints


@contextmanager
def _lock_free():
    yield


@contextmanager
def _lock_busy():
    raise JobSkipped("ingest_running")
    yield  # pragma: no cover


@pytest.fixture
def wired(monkeypatch):
    """설정 피드를 CONFIGURED로, ingest 락을 비어 있는 것으로 바꾼다. 돌려주는 함수로 직전 ingest 실행과 collect_rss를 끼운다."""
    import crawler.rss_collector as rss_module
    from config.settings import Settings

    monkeypatch.setattr(Settings, "RSS_FEEDS", CONFIGURED)
    monkeypatch.setattr(rss_retry, "ingest_lock", _lock_free)

    def wire(source, collect=None):
        calls = []

        def fake_collect(hours, feeds):
            calls.append({"hours": hours, "feeds": list(feeds)})
            return collect(hours, feeds) if collect else {"inserted": 0, "skipped": 0, "failed_feeds": 0, "per_feed": {}}

        monkeypatch.setattr(rss_retry, "read_source", lambda: source)
        monkeypatch.setattr(rss_module, "collect_rss", fake_collect)
        return calls

    return wire


def test_no_failed_feed_ends_without_a_single_request(wired):
    calls = wired(_source({"동아일보": _feed(), "경향신문": _feed()}))
    ctx, checkpoints = _ctx()

    assert rss_retry.run(ctx) == {}

    assert calls == []  # collect_rss가 불리지 않는다 = 요청도 DB 쓰기도 없다
    assert ctx.stats["retry_feeds"] == [] and ctx.stats["source_run_id"] == 1371
    assert "rss" not in ctx.stats and "recovered" not in ctx.stats
    assert ctx.warnings == []


def test_only_the_failed_feeds_are_read_again_with_the_ingest_cutoff(wired):
    calls = wired(_source({"전자신문_IT": _feed(RESET), "경향신문": _feed(), "동아일보": _feed(RESET)}))
    ctx, _ = _ctx()

    rss_retry.run(ctx)

    assert calls == [{"hours": 100, "feeds": ["동아일보", "전자신문_IT"]}]
    ctx2, _ = _ctx(["--rss-hours", "48"])
    rss_retry.run(ctx2)
    assert calls[-1]["hours"] == 48


def test_stats_keep_the_ingest_shape_and_say_what_recovered(wired):
    def collect(hours, feeds):
        return {"inserted": 12, "skipped": 0, "failed_feeds": 1, "per_feed": {
            "동아일보": _feed(inserted=12, summary={"stored": 12, "dropped": 0, "duplicate": 0}),
            "전자신문_IT": _feed("feed unreadable (HTTP 503): x"),
        }}

    wired(_source({"동아일보": _feed(RESET), "전자신문_IT": _feed("feed unreadable (HTTP 503): x")}), collect)
    ctx, _ = _ctx()

    rss_retry.run(ctx)

    assert ctx.stats["retry_feeds"] == ["동아일보", "전자신문_IT"]
    assert ctx.stats["recovered"] == ["동아일보"]
    assert ctx.stats["still_failed"] == ["전자신문_IT"]
    assert ctx.stats["rss"]["inserted"] == 12 and "duration_s" in ctx.stats["rss"]
    assert ctx.stats["rss"]["per_feed"]["동아일보"]["summary"]["stored"] == 12  # stats.rss.per_feed는 ingest와 같은 자리·모양
    assert (ctx.stats["source_run_id"], ctx.stats["source_status"], ctx.stats["source_age_min"]) == (1371, "succeeded", 35.0)
    assert ctx.stats["source_started_at"] == "2026-10-25T05:05:01+00:00"
    assert ctx.warnings == []


def test_a_retry_that_fails_again_is_not_a_failure_and_adds_no_warning(wired):
    # 그 회차의 ingest가 이미 피드별 경고를 냈다 - 같은 실패를 한 번 더 알리지 않는다(알림이 매시 두 배가 된다)
    def collect(hours, feeds):
        return {"inserted": 0, "skipped": 0, "failed_feeds": 1, "per_feed": {"동아일보": _feed(RESET, entries=0)}}

    wired(_source({"동아일보": _feed(RESET)}), collect)
    ctx, _ = _ctx()

    assert rss_retry.run(ctx) == {}

    assert ctx.stats["recovered"] == [] and ctx.stats["still_failed"] == ["동아일보"]
    assert ctx.warnings == []


def test_the_source_run_is_recorded_before_the_first_request(wired):
    # 읽는 도중 프로세스가 죽어도 job_runs에 source_run_id가 남아 같은 ingest 실행의 피드를 다시 읽지 않는다
    ctx, checkpoints = _ctx()
    seen = {}

    def collect(hours, feeds):
        seen["checkpointed_source"] = [c.get("source_run_id") for c in checkpoints]
        return {"inserted": 0, "skipped": 0, "failed_feeds": 0, "per_feed": {"동아일보": _feed()}}

    wired(_source({"동아일보": _feed(RESET)}), collect)

    rss_retry.run(ctx)

    assert seen["checkpointed_source"] == [1371]


def test_a_summary_feed_that_stored_nothing_warns_after_a_successful_retry(wired):
    def collect(hours, feeds):
        return {"inserted": 6, "skipped": 0, "failed_feeds": 0, "per_feed": {
            "동아일보": _feed(inserted=6, summary={"stored": 0, "dropped": 5, "duplicate": 1})}}

    wired(_source({"동아일보": _feed(RESET)}), collect)
    ctx, _ = _ctx()

    rss_retry.run(ctx)

    assert len(ctx.warnings) == 1 and ctx.warnings[0].startswith("동아일보: RSS 요약으로 저장된 기사가 없다")


def test_collect_errors_fail_the_job_with_the_stats_so_far(wired):
    def collect(hours, feeds):
        raise RuntimeError("db down")

    wired(_source({"동아일보": _feed(RESET)}), collect)
    ctx, _ = _ctx()

    with pytest.raises(RuntimeError, match="db down"):
        rss_retry.run(ctx)

    assert ctx.stats["retry_feeds"] == ["동아일보"]


# ----------------------------------------------------------------------------- 건너뛰는 경우


def test_a_running_ingest_skips_the_job_before_anything_is_read(monkeypatch, wired):
    calls = wired(_source({"동아일보": _feed(RESET)}))
    monkeypatch.setattr(rss_retry, "ingest_lock", _lock_busy)
    monkeypatch.setattr(rss_retry, "read_source", lambda: pytest.fail("락을 못 잡았는데 job_runs를 읽었다"))
    ctx, _ = _ctx()

    with pytest.raises(JobSkipped) as skipped:
        rss_retry.run(ctx)

    assert skipped.value.reason == "ingest_running" and calls == []


def test_no_ingest_rss_stats_yet_skips(wired):
    calls = wired(None)
    ctx, _ = _ctx()

    with pytest.raises(JobSkipped) as skipped:
        rss_retry.run(ctx)

    assert skipped.value.reason == "no_ingest_rss_stats" and calls == []


def test_the_same_ingest_run_is_never_retried_twice(wired):
    # ingest가 건너뛰어져 새 실행이 없는 시간에도 같은 실패 목록을 되풀이하지 않는다 - 시간당 읽기가 2회를 넘지 않는다
    calls = wired(_source({"동아일보": _feed(RESET)}, already_retried=True))
    ctx, _ = _ctx()

    with pytest.raises(JobSkipped) as skipped:
        rss_retry.run(ctx)

    assert skipped.value.reason == "already_retried" and skipped.value.stats == {"skipped_source_run_id": 1371}
    assert calls == []
    assert "source_run_id" not in skipped.value.stats  # 건너뛴 행이 '이미 읽었다' 표시로 세어지지 않는다


# ----------------------------------------------------------------------------- 잡 런타임과 함께


class FakeStore:
    def __init__(self):
        self.rows = {}
        self.locked = []

    def try_lock(self, job):
        self.locked.append(job)
        return True

    def unlock(self, job):
        pass

    def abandon_stale(self, job):
        return 0

    def start(self, job, git_sha):
        self.rows[1] = {"job": job, "status": "running", "stats": {}, "error": None}
        return 1

    def finish(self, run_id, status, stats, error):
        self.rows[run_id].update(status=status, stats=stats, error=error)

    def update_stats(self, run_id, stats):
        self.rows[run_id]["stats"] = stats

    def record(self, job, status, stats, git_sha, error=None):
        raise AssertionError("락을 못 잡은 실행이 아니다")

    def close(self):
        pass


def _run_through_runtime(fn_args=("rss_retry",)):
    store, notified = FakeStore(), []
    result = run_job("rss_retry", rss_retry.run, args=build_parser().parse_args(list(fn_args)),
                     store_factory=lambda: store, notify=notified.append, git_sha="abc1234")
    return result, store, notified


def test_runtime_records_a_retry_as_succeeded_and_a_blocked_one_as_skipped_without_alerts(monkeypatch, wired):
    wired(_source({"동아일보": _feed(RESET)}), lambda hours, feeds: {
        "inserted": 3, "skipped": 0, "failed_feeds": 0, "per_feed": {"동아일보": _feed(inserted=3)}})
    result, store, notified = _run_through_runtime()
    assert (result.status, result.exit_code) == ("succeeded", 0)
    assert store.locked == ["rss_retry"]  # 런타임이 잡는 락은 자기 이름이다 - ingest의 락은 ingest_lock이 따로 잡는다
    assert result.stats["recovered"] == ["동아일보"] and result.stats["retry_feeds"] == ["동아일보"]
    assert notified == []

    monkeypatch.setattr(rss_retry, "ingest_lock", _lock_busy)
    result, _, notified = _run_through_runtime()
    assert (result.status, result.exit_code) == ("skipped", 0)
    assert result.stats["reason"] == "ingest_running"
    assert notified == []


# ----------------------------------------------------------------------------- ingest 락


class LockStore:
    def __init__(self, available=True, unlock_error=False):
        self.available, self.unlock_error = available, unlock_error
        self.tried, self.unlocked, self.closed = [], [], False

    def try_lock(self, job):
        self.tried.append(job)
        return self.available

    def unlock(self, job):
        self.unlocked.append(job)
        if self.unlock_error:
            raise RuntimeError("connection lost")

    def close(self):
        self.closed = True


@pytest.fixture
def lock_store(monkeypatch):
    from jobs.store import PostgresJobRunStore

    def install(store):
        connected = []
        monkeypatch.setattr(PostgresJobRunStore, "connect", staticmethod(lambda job: connected.append(job) or store))
        return connected

    return install


def test_ingest_lock_takes_the_lock_the_ingest_job_holds_and_releases_it_afterwards(lock_store):
    store = LockStore()
    connected = lock_store(store)

    with rss_retry.ingest_lock():
        assert store.unlocked == [] and not store.closed  # 읽는 동안 쥐고 있다

    assert connected == ["rss_retry"]  # application_name용 - 락 이름이 아니다
    assert store.tried == [rss_retry.INGEST_JOB] == ["ingest"] and "ingest" in JOBS
    assert store.unlocked == ["ingest"] and store.closed


def test_ingest_lock_skips_when_ingest_holds_it_and_still_closes_the_connection(lock_store):
    store = LockStore(available=False)
    lock_store(store)

    with pytest.raises(JobSkipped) as skipped:
        with rss_retry.ingest_lock():
            pytest.fail("락을 못 잡았는데 본문이 실행됐다")

    assert skipped.value.reason == "ingest_running"
    assert store.unlocked == [] and store.closed  # 잡지 못한 락은 풀지 않는다


def test_ingest_lock_is_released_when_the_body_raises_and_a_failed_unlock_does_not_mask_it(lock_store):
    store = LockStore(unlock_error=True)
    lock_store(store)

    with pytest.raises(ValueError, match="본문 오류"):
        with rss_retry.ingest_lock():
            raise ValueError("본문 오류")

    assert store.unlocked == ["ingest"] and store.closed  # 풀기에 실패해도 연결을 닫는다 - 세션 락은 연결이 끊기면 풀린다


# ----------------------------------------------------------------------------- 직전 ingest 실행 조회


class Cur:
    def __init__(self, row):
        self.row = row
        self.sql = None

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchone(self):
        return self.row


def test_load_source_reads_the_latest_ingest_run_that_has_rss_stats_and_whether_it_was_retried():
    started = datetime(2026, 10, 25, 5, 5, 1, tzinfo=timezone.utc)
    per_feed = {"동아일보": _feed(RESET)}
    cur = Cur((1371, "succeeded", started, datetime(2026, 10, 25, 5, 40, 1, tzinfo=timezone.utc), per_feed, True))

    source = rss_retry.load_source(cur)

    assert source["run_id"] == 1371 and source["status"] == "succeeded" and source["per_feed"] == per_feed
    assert source["already_retried"] is True and round(source["age_min"], 1) == 35.0
    sql = " ".join(cur.sql.split())
    assert "j.job = 'ingest'" in sql and "'{rss,per_feed}'" in sql and "ORDER BY j.started_at DESC LIMIT 1" in sql
    assert "r.job = 'rss_retry'" in sql and "r.stats ->> 'source_run_id' = j.id::text" in sql


def test_load_source_without_any_ingest_stats_is_none():
    assert rss_retry.load_source(Cur(None)) is None


# ----------------------------------------------------------------------------- 스케줄


def test_the_micro_crontab_runs_the_retry_at_minute_40_after_popularity_and_every_hour():
    entries, unsupported = parse_crontab((REPO / "docker" / "crontab.micro").read_text(encoding="utf-8"))
    assert unsupported == []
    by_job = {e.job: e for e in entries}
    retry = by_job["rss_retry"]
    assert (retry.minutes, len(retry.hours)) == ((40,), 24)
    assert max(by_job["popularity"].minutes) < 40 and by_job["ingest"].minutes == (5,)
    assert "rss_retry" in JOBS


def test_the_retry_takes_the_rss_hours_option_with_the_ingest_default():
    args = build_parser().parse_args(["rss_retry"])
    assert args.rss_hours == build_parser().parse_args(["ingest"]).rss_hours == 100
    assert isinstance(args, argparse.Namespace)
