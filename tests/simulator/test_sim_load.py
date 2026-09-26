import importlib.util
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sim.catalog import synthetic_catalog
from sim.driver import ApiClient, ApiError
from sim.fake_app import FakeBackend, create_fake_app
from sim.load import LoadUser, UserPool, swallow_api_errors
from sim.loadtest import markdown_table, summarize_locust_csv

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
        subprocess.run([sys.executable, "-m", "locust", "-f", "sim/locustfile.py", "--headless",
                        "--host", f"http://127.0.0.1:{port}", "-u", "4", "-r", "4", "-t", "6s",
                        "--only-summary", "--csv", str(prefix)],
                       cwd=REPO, check=True, timeout=120, env={**os.environ, "SIM_LOAD_USERS": "20", "SIM_NEWCOMERS_PER_SEC": "1"})
        s = summarize_locust_csv(Path(f"{prefix}_stats.csv"))
    finally:
        server.terminate()
        server.wait(timeout=10)
    assert s["Aggregated"]["failures"] == 0
    assert s["today"]["requests"] > 0
    assert s["today_first_view"]["requests"] > 0
    assert {"signup", "login", "onboarding_news", "put_newsletters", "put_categories"} <= set(s)
