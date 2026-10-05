import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")  # not installed in the integration CI job

from fastapi.testclient import TestClient  # noqa: E402

from sim.catalog import synthetic_catalog
from sim.driver import ApiClient, ApiError
from sim.fake_app import FakeBackend, create_fake_app
from sim.load import LoadUser, ReadyGate, SourceTally, UserPool, setup_reader, swallow_api_errors
from sim.loadtest import markdown_table, read_stage_report, source_table, summarize_locust_csv, window_table

REPO = Path(__file__).resolve().parents[2]
MIDNIGHT = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture
def fake():
    backend = FakeBackend(synthetic_catalog(n_days=2, items_per_day=30, seed=0, start=MIDNIGHT), policy="reactive")
    return backend, TestClient(create_fake_app(backend))


def test_reader_onboards_then_reads_and_clicks_an_item_it_was_shown(fake):
    backend, client = fake
    api = ApiClient(client)
    reader = LoadUser(UserPool(3, run_tag="t").take(), api)

    reader.start()
    n = reader.today()
    nid = reader.click()

    assert [c.endpoint for c in api.calls][:2] == ["signup", "login"]
    assert "put_categories" in {c.endpoint for c in api.calls}
    assert n > 0
    assert nid in [it.news_letter_id for it in reader.agent.last_feed]
    assert [c[1] for c in backend.clicks] == [nid]


def test_newcomer_first_view_is_reported_under_its_own_name(fake):
    _, client = fake
    api = ApiClient(client)
    LoadUser(UserPool(1, run_tag="t").take(), api).newcomer_flow()
    assert api.calls[-1].endpoint == "today_first_view" and api.calls[-1].ok


def test_reader_tallies_the_rec_source_of_every_feed(fake):
    _, client = fake
    tally = SourceTally()
    reader = LoadUser(UserPool(2, run_tag="t").take(), ApiClient(client), tally=tally)
    reader.start()
    reader.today()
    reader.today()
    LoadUser(UserPool(1, run_tag="n").take(), ApiClient(client), tally=tally).newcomer_flow()

    s = tally.summary()
    assert s["today"]["responses"] == 2 and s["today_first_view"]["responses"] == 1
    assert s["today"]["source_counts"] == {"personalized": 2}  # the fake app's reactive policy
    assert s["today"]["fallback_rate"] == 0.0 and s["today"]["empty_rate"] == 0.0


def test_source_tally_rates_and_missing_header():
    tally = SourceTally()
    for src, n in [("realtime", 10), ("popular", 3), ("recent", 1), ("empty", 0), ("batch", 10)]:
        tally.record("today", src, n)
    tally.record("legacy", None, 5)
    s = tally.summary()
    assert s["today"]["fallback_rate"] == 3 / 5  # popular, recent, empty; batch is ambiguous -> not counted
    assert s["today"]["empty_rate"] == 1 / 5
    assert s["legacy"]["fallback_rate"] is None  # no X-Rec-Source header: unmeasurable, not zero
    assert s["legacy"]["source_counts"] == {SourceTally.NO_HEADER: 1}
    table = source_table({"20": s})
    assert "| 20 | today | 5 | 20.00% | 60.00% |" in table and "| 20 | legacy | 1 | 0.00% | - |" in table
    tally.reset()
    assert tally.summary() == {}


def test_click_task_with_an_empty_feed_is_tallied_as_skipped_and_sends_nothing():
    # static_batch answers a user without a batch row with [] (the batch-only API's cold start)
    backend = FakeBackend(synthetic_catalog(n_days=2, items_per_day=30, seed=0, start=MIDNIGHT), policy="static_batch")
    api, tally = ApiClient(TestClient(create_fake_app(backend))), SourceTally()
    reader = LoadUser(UserPool(1, run_tag="t").take(), api, tally=tally)
    reader.start()
    assert reader.today() == 0
    n_calls = len(api.calls)

    assert reader.click() is None
    assert reader.click() is None

    assert len(api.calls) == n_calls  # not turned into extra /today requests
    assert tally.skipped_clicks == 2 and backend.clicks == []
    assert tally.summary()["today"]["responses"] == 1


def test_click_task_of_a_reader_that_has_not_read_yet_fetches_a_feed_first(fake):
    backend, client = fake
    api, tally = ApiClient(client), SourceTally()
    reader = LoadUser(UserPool(1, run_tag="t").take(), api, tally=tally)
    reader.start()

    nid = reader.click()

    assert [c.endpoint for c in api.calls[-2:]] == ["today", "click"]
    assert [c[1] for c in backend.clicks] == [nid] and tally.skipped_clicks == 0


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_ready_gate_opens_only_when_every_expected_reader_has_reported():
    clock, opened = _Clock(), []
    gate = ReadyGate(clock=clock)
    gate.on_open(lambda: opened.append(clock.t))
    gate.start()

    clock.t = 1.0
    gate.reader_done(True)
    gate.reader_done(True)
    assert not gate.is_open  # spawning is not complete: the number of readers is still unknown
    gate.expect(3)
    assert not gate.is_open and opened == []
    clock.t = 4.0
    gate.reader_done(False)  # a failed setup still counts as "done", or the gate would never open

    assert gate.is_open and opened == [4.0]
    assert gate.wait(timeout=0) is True
    gate.reader_done(True)  # a late report does not reopen the window
    assert opened == [4.0]
    clock.t = 64.0
    assert gate.summary() == {"readers_expected": 3, "readers_done": 4, "readers_failed_setup": 1,
                              "ready_timeout": False, "setup_s": 4.0, "measured_s": 60.0}


def test_ready_gate_is_forced_open_and_flagged_when_a_reader_never_finishes_setup():
    opened = []
    gate = ReadyGate()
    gate.on_open(lambda: opened.append(True))
    gate.expect(2)
    gate.reader_done(True)

    assert gate.wait(timeout=0.01) is False
    assert gate.is_open and opened == [True]
    assert gate.summary()["ready_timeout"] is True and gate.summary()["readers_done"] == 1


def test_ready_gate_releases_no_reader_before_the_reset_callback_has_run():
    gate, order, lock = ReadyGate(), [], threading.Lock()
    gate.on_open(lambda: order.append("reset"))
    last_may_finish = threading.Event()

    def reader(i):
        if i == 2:
            last_may_finish.wait(5)  # the slow reader: still in account setup
        gate.reader_done(True)
        gate.wait(5)
        with lock:
            order.append(f"task-{i}")

    threads = [threading.Thread(target=reader, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    gate.expect(3)
    time.sleep(0.05)
    assert order == []  # two readers are ready, but nobody runs a task while one is still in setup
    last_may_finish.set()
    for t in threads:
        t.join(5)

    assert order[0] == "reset" and sorted(order[1:]) == ["task-0", "task-1", "task-2"]


def test_setup_reader_reports_to_the_gate_whether_setup_worked_or_not(fake):
    _, client = fake
    gate, results = ReadyGate(), {}
    gate.expect(2)

    def first_reader():  # sets up fine, then blocks until the other reader has reported
        results["ok"] = setup_reader(LoadUser(UserPool(1, run_tag="ok").take(), ApiClient(client)), gate, timeout_s=5)

    t = threading.Thread(target=first_reader)
    t.start()
    for _ in range(200):
        if gate.done == 1:
            break
        time.sleep(0.01)
    assert gate.done == 1 and not gate.is_open

    broken = ApiClient(_LocustLikeSession(500), locust=True)
    results["bad"] = setup_reader(LoadUser(UserPool(1, run_tag="bad").take(), broken), gate, timeout_s=5)
    t.join(5)

    assert results == {"ok": True, "bad": False}
    assert gate.is_open and (gate.done, gate.failed, gate.timed_out) == (2, 1, False)


def test_stage_report_carries_the_window_and_skipped_clicks_next_to_the_source_tally(tmp_path):
    tally = SourceTally()
    tally.record("today", "batch", 20)
    tally.record_skipped_click()
    window = {"readers_expected": 5, "readers_done": 5, "readers_failed_setup": 1, "ready_timeout": False,
              "setup_s": 7.25, "measured_s": 112.75}
    tally.write(tmp_path / "rps5_sources.json", window=window)

    endpoints, win = read_stage_report(tmp_path / "rps5_sources.json")

    assert endpoints == tally.summary()
    assert win == {**window, "skipped_clicks": 1}
    assert "| 5 | 112.8 | 7.2 | 1/5 | 아니오 | 1 |" in window_table({"5": win})
    assert read_stage_report(tmp_path / "missing.json") == ({}, {})
    tally.reset()
    assert tally.skipped_clicks == 0


def test_user_pool_hands_out_distinct_users_then_wraps():
    pool = UserPool(3, run_tag="t")
    emails = [pool.take().email for _ in range(4)]
    assert len(set(emails[:3])) == 3 and emails[3] == emails[0]
    assert all(e.endswith("@sim.invalid") for e in emails)


def test_swallow_api_errors_keeps_the_virtual_user_alive():
    def boom():
        raise ApiError("today", 500)

    assert swallow_api_errors(boom) is None


class _LocustLikeSession:
    """Mimics locust.clients.HttpSession with catch_response=True."""

    def __init__(self, status):
        self.status = status
        self.results = []

    @contextmanager
    def request(self, method, url, name=None, catch_response=False, **kw):
        assert catch_response
        resp = SimpleNamespace(status_code=self.status, headers={}, json=lambda: {"detail": "x"})
        resp.success = lambda: self.results.append((name, "success"))
        resp.failure = lambda msg: self.results.append((name, "failure"))
        yield resp


def test_locust_mode_judges_success_by_the_contract_not_by_2xx():
    user = UserPool(1, run_tag="t").take()
    existing = _LocustLikeSession(400)
    assert ApiClient(existing, locust=True).signup(user) is False
    assert existing.results == [("signup", "success")]

    broken = _LocustLikeSession(500)
    with pytest.raises(ApiError):
        ApiClient(broken, locust=True).signup(user)
    assert broken.results == [("signup", "failure")]


class _ScriptedLocustSession:
    """Locust-like session answering from a script of (status, body); keeps the reported name and verdict."""

    def __init__(self, script):
        self.script = list(script)
        self.results = []

    @contextmanager
    def request(self, method, url, name=None, catch_response=False, **kw):
        status, body = self.script.pop(0)
        resp = SimpleNamespace(status_code=status, headers={}, json=lambda: body, request_meta={"name": name})
        verdict = []
        resp.success = lambda: verdict.append("success")
        resp.failure = lambda msg: verdict.append("failure")
        yield resp
        self.results.append((resp.request_meta["name"], verdict[-1]))


def test_locust_mode_reports_a_retried_401_under_its_own_name_and_not_as_a_failure():
    session = _ScriptedLocustSession([(200, {"access_token": "t1"}), (401, {"detail": "expired"}),
                                      (200, {"access_token": "t2"}), (200, [])])
    api = ApiClient(session, locust=True)
    api.login("a@sim.invalid", "pw")

    feed = api.today()

    assert feed.items == [] and api.token == "t2"
    assert session.results == [("login", "success"), ("today_token_expired", "success"), ("login", "success"),
                               ("today", "success")]


STATS_CSV = """Type,Name,Request Count,Failure Count,Median Response Time,Average Response Time,Min Response Time,Max Response Time,Average Content Size,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%
GET,today,900,9,12,15.2,3,80,4000,15.0,0.15,12,14,15,16,20,31,40,52,79,80,80
POST,click,100,0,8,9.0,2,30,40,1.7,0.0,8,9,9,10,12,14,20,25,30,30,30
GET,signup,0,0,0,0,0,0,0,0,0,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A,N/A
,Aggregated,1000,9,11,14.6,2,80,3604,16.7,0.15,11,13,15,15,19,30,39,50,79,80,80
"""


def test_locust_stats_csv_summary_and_table(tmp_path):
    path = tmp_path / "rps20_stats.csv"
    path.write_text(STATS_CSV, encoding="utf-8")
    s = summarize_locust_csv(path)
    assert s["today"] == {"requests": 900, "failures": 9, "error_rate": 0.01, "rps": 15.0,
                          "p50_ms": 12.0, "p95_ms": 31.0, "p99_ms": 52.0}
    assert s["signup"]["error_rate"] is None and s["signup"]["p95_ms"] is None
    table = markdown_table({"20": s}, ["today", "Aggregated", "missing"])
    assert table.count("\n") == 3  # header, rule, two rows


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_locustfile_runs_headless_against_the_fake_server(tmp_path):
    # Starts a uvicorn server and a 6 s Locust run: GitHub Actions or an explicit opt-in only, so
    # a plain local `pytest` never generates load on a dev machine. GITHUB_ACTIONS, not CI: other
    # local tools also export CI=true.
    if not (os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("SIM_LOAD_SMOKE") == "1"):
        pytest.skip("load smoke runs in GitHub Actions only (set SIM_LOAD_SMOKE=1 to opt in locally)")
    # find_spec, not importorskip: importing locust monkey-patches this pytest process (gevent)
    if importlib.util.find_spec("locust") is None or importlib.util.find_spec("uvicorn") is None:
        pytest.skip("locust/uvicorn not installed")
    port = _free_port()
    server = subprocess.Popen([sys.executable, "-m", "sim.fake_app", "--port", str(port)], cwd=REPO)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if server.poll() is not None:
                    pytest.fail("fake server exited")
                time.sleep(0.2)
        prefix = tmp_path / "smoke"
        sources = tmp_path / "smoke_sources.json"
        subprocess.run([sys.executable, "-m", "locust", "-f", "sim/locustfile.py", "--headless",
                        "--host", f"http://127.0.0.1:{port}", "-u", "4", "-r", "4", "-t", "6s",
                        "--reset-stats", "--only-summary", "--csv", str(prefix)],
                       cwd=REPO, check=True, timeout=120,
                       env={**os.environ, "SIM_LOAD_USERS": "20", "SIM_NEWCOMERS_PER_SEC": "1",
                            "SIM_SOURCES_OUT": str(sources)})
        s = summarize_locust_csv(Path(f"{prefix}_stats.csv"))
        tallied, window = read_stage_report(sources)
    finally:
        server.terminate()
        server.wait(timeout=10)
    assert s["Aggregated"]["failures"] == 0
    assert s["today"]["requests"] > 0
    assert s["today_first_view"]["requests"] > 0
    assert {"signup", "login", "onboarding_news", "put_newsletters", "put_categories"} <= set(s)
    assert tallied["today"]["responses"] > 0 and tallied["today"]["fallback_rate"] is not None
    # the measured window opened after all 3 readers finished setup, and the CSV covers that window only
    assert (window["readers_expected"], window["readers_done"], window["readers_failed_setup"]) == (3, 3, 0)
    assert window["ready_timeout"] is False and 0 < window["measured_s"] < 6
    assert s["today"]["requests"] == tallied["today"]["responses"]
