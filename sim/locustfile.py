"""Locust load test for the newsletter API, reusing the simulator's users (ADR 0019).

    locust -f sim/locustfile.py --host http://localhost:8000 --headless -u 21 -r 21 -t 2m --reset-stats

Staged 5/20/50 RPS runs with a percentile table: `python -m sim.loadtest` (sim/README.md).

ActiveReader users each run ~SIM_TASKS_PER_USER_PER_SEC tasks/s (constant
throughput), so N readers ~ N RPS of steady-state traffic: /newsletters/today
(weight 9) and a click (weight 1). One Newcomer user (fixed_count) creates a new
account every 1/SIM_NEWCOMERS_PER_SEC seconds and walks signup -> login ->
onboarding -> first /today, reported separately as `today_first_view`.

Environment: SIM_LOAD_USERS (reader identities, default 1000), SIM_RUN_TAG
(default "load"; the same tag re-uses reader accounts across runs), SIM_SEED,
SIM_TASKS_PER_USER_PER_SEC (1), SIM_NEWCOMERS_PER_SEC (0.2), SIM_SOURCES_OUT
(optional path: on test stop, /today responses counted by X-Rec-Source, with
empty and fallback rates, are written there as JSON). Accounts are
@sim.invalid users - run only against a disposable database. Single Locust
process only (no --processes): the source tally lives in this process.
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
from sim.load import CLICK_WEIGHT, TODAY_WEIGHT, LoadUser, SourceTally, UserPool, swallow_api_errors  # noqa: E402
from sim.text import nouns  # noqa: E402

# Kiwi loads its model on first use (seconds of CPU). Inside a spawned user that
# blocks the gevent hub and inflates every in-flight request's measured latency.
nouns("부하 테스트 준비")

N_USERS = int(os.getenv("SIM_LOAD_USERS", "1000"))
RUN_TAG = os.getenv("SIM_RUN_TAG", "load")
SEED = int(os.getenv("SIM_SEED", "0"))
TASKS_PER_SEC = float(os.getenv("SIM_TASKS_PER_USER_PER_SEC", "1"))
NEWCOMERS_PER_SEC = float(os.getenv("SIM_NEWCOMERS_PER_SEC", "0.2"))
SOURCES_OUT = os.getenv("SIM_SOURCES_OUT")
SOURCES = SourceTally()

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


@events.spawning_complete.add_listener
def _reset_source_tally(user_count, **_kwargs):
    # mirror --reset-stats so the tally covers the same window as Locust's CSV
    env = _LOCUST.get("env")
    if env is not None and getattr(env.parsed_options, "reset_stats", False):
        SOURCES.reset()


@events.test_stop.add_listener
def _write_source_tally(**_kwargs):
    if SOURCES_OUT:
        SOURCES.write(Path(SOURCES_OUT))


class ActiveReader(HttpUser):
    wait_time = constant_throughput(TASKS_PER_SEC)

    def on_start(self):
        self.load = LoadUser(READERS.take(), ApiClient(self.client, locust=True), seed=SEED, tally=SOURCES)
        swallow_api_errors(self.load.start)

    @task(TODAY_WEIGHT)
    def today(self):
        swallow_api_errors(self.load.today)

    @task(CLICK_WEIGHT)
    def click(self):
        swallow_api_errors(self.load.click)


class Newcomer(HttpUser):
    fixed_count = 1
    wait_time = constant_throughput(NEWCOMERS_PER_SEC)

    @task
    def signup_onboard_first_view(self):
        load = LoadUser(NEWCOMERS.take(), ApiClient(self.client, locust=True), seed=SEED, tally=SOURCES)
        swallow_api_errors(load.newcomer_flow)
