"""Locust load test for the newsletter API, reusing the simulator's users (ADR 0019).

    locust -f sim/locustfile.py --host http://localhost:8000 --headless -u 21 -r 21 -t 2m --reset-stats

Staged 5/20/50 RPS runs with a percentile table: `python -m sim.loadtest` (sim/README.md).

ActiveReader users each run ~SIM_TASKS_PER_USER_PER_SEC tasks/s (constant
throughput), so N readers ~ N RPS of steady-state traffic: /newsletters/today
(weight 9) and a click (weight 1). One Newcomer user (fixed_count) creates a new
account every 1/SIM_NEWCOMERS_PER_SEC seconds and walks signup -> login ->
onboarding -> first /today, reported separately as `today_first_view`. That flow
is 6-8 requests, so at the default rate it adds about 1.2-1.6 requests/s on top
of the readers' N.

Measured window: Locust's --reset-stats fires when the users are spawned, while
their account setup (signup, bcrypt login, onboarding) is still in flight. Tasks
therefore wait on a ReadyGate; when every reader has finished setup the
statistics and the source tally are reset (only with --reset-stats) and the
tasks start. `-t` still counts from the start of the run, so the measured window
is the run time minus the setup time; both are written to SIM_SOURCES_OUT.

constant_throughput is a closed-loop model: a user sends its next request only
after the previous one returned. When the target saturates, the offered rate
drops instead of queueing, and the latency percentiles understate what an open
arrival process would see (coordinated omission). Read a stage's percentiles
only together with its achieved requests/s.

Environment: SIM_LOAD_USERS (reader identities, default 1000), SIM_RUN_TAG
(default "load"; the same tag re-uses reader accounts across runs), SIM_SEED,
SIM_TASKS_PER_USER_PER_SEC (1), SIM_NEWCOMERS_PER_SEC (0.2), SIM_READY_TIMEOUT_S
(120; the gate is forced open, and flagged, if setup takes longer),
SIM_SOURCES_OUT (optional path: on test stop the stage report is written there
as JSON - /today responses counted by X-Rec-Source with empty and fallback
rates, skipped clicks, and the measured window). Accounts are @sim.invalid
users - run only against a disposable database. Single Locust process only (no
--processes): the gate and the source tally live in this process.
"""

import os
import sys
import time
from pathlib import Path

# The `locust` console script puts only this file's directory on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gevent  # noqa: E402
from locust import HttpUser, constant_throughput, events, task  # noqa: E402

from sim.driver import ApiClient  # noqa: E402
from sim.load import (  # noqa: E402
    CLICK_WEIGHT,
    TODAY_WEIGHT,
    LoadUser,
    ReadyGate,
    SourceTally,
    UserPool,
    setup_reader,
    swallow_api_errors,
)
from sim.text import nouns  # noqa: E402

# Kiwi loads its model on first use (seconds of CPU). Inside a spawned user that
# blocks the gevent hub and inflates every in-flight request's measured latency.
nouns("부하 테스트 준비")

N_USERS = int(os.getenv("SIM_LOAD_USERS", "1000"))
RUN_TAG = os.getenv("SIM_RUN_TAG", "load")
SEED = int(os.getenv("SIM_SEED", "0"))
TASKS_PER_SEC = float(os.getenv("SIM_TASKS_PER_USER_PER_SEC", "1"))
NEWCOMERS_PER_SEC = float(os.getenv("SIM_NEWCOMERS_PER_SEC", "0.2"))
READY_TIMEOUT_S = float(os.getenv("SIM_READY_TIMEOUT_S", "120"))
SOURCES_OUT = os.getenv("SIM_SOURCES_OUT")
SOURCES = SourceTally()
GATE = ReadyGate()

READERS = UserPool(N_USERS, seed=SEED, run_tag=RUN_TAG)
# A per-process timestamp keeps newcomers genuinely new on every run (cold-start path).
NEWCOMERS = UserPool(N_USERS, seed=SEED, run_tag=f"{RUN_TAG}-new-{int(time.time())}")

# The hub's cached clock went stale during the blocking setup above; without this
# `-t/--run-time` is measured from before the setup and can expire immediately.
gevent.get_hub().loop.update_now()


_LOCUST = {}


@events.init.add_listener
def _keep_environment(environment, **_kwargs):
    _LOCUST["env"] = environment


@events.test_start.add_listener
def _mark_stage_start(**_kwargs):
    GATE.start()


@events.spawning_complete.add_listener
def _expect_readers(user_count, **_kwargs):
    env = _LOCUST.get("env")
    runner = getattr(env, "runner", None)
    counts = runner.user_classes_count if runner is not None else {}
    GATE.expect(counts.get("ActiveReader", max(0, user_count - 1)))


def _begin_measured_window():
    # Every reader has reported. Nothing is in flight (readers and the newcomer are all blocked on
    # the gate), so this is a clean cut: the CSV and the source tally cover the same window.
    env = _LOCUST.get("env")
    if env is not None and getattr(env, "reset_stats", False):
        env.stats.reset_all()
        SOURCES.reset()


GATE.on_open(_begin_measured_window)


@events.test_stop.add_listener
def _write_stage_report(**_kwargs):
    if SOURCES_OUT:
        SOURCES.write(Path(SOURCES_OUT), window=GATE.summary())


class ActiveReader(HttpUser):
    wait_time = constant_throughput(TASKS_PER_SEC)

    def on_start(self):
        self.load = LoadUser(READERS.take(), ApiClient(self.client, locust=True), seed=SEED, tally=SOURCES)
        setup_reader(self.load, GATE, READY_TIMEOUT_S)

    @task(TODAY_WEIGHT)
    def today(self):
        swallow_api_errors(self.load.today)

    @task(CLICK_WEIGHT)
    def click(self):
        swallow_api_errors(self.load.click)


class Newcomer(HttpUser):
    fixed_count = 1
    wait_time = constant_throughput(NEWCOMERS_PER_SEC)

    def on_start(self):
        GATE.wait(READY_TIMEOUT_S)  # no signup flow in flight when the measured window starts

    @task
    def signup_onboard_first_view(self):
        load = LoadUser(NEWCOMERS.take(), ApiClient(self.client, locust=True), seed=SEED, tally=SOURCES)
        swallow_api_errors(load.newcomer_flow)
