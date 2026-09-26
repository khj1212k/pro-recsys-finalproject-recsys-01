"""Load-test behaviors shared by the Locust file and its tests.

The same SimUser / ClickModel / ApiClient as the behavior simulation, but driven
by wall-clock time and a fixed task mix instead of per-user session schedules:

  ActiveReader  existing account: /newsletters/today (weight 9) and a click on an
                item from the last feed (weight 1)
  Newcomer      signup -> login -> onboarding -> first /newsletters/today, with a
                fresh synthetic identity every iteration (cold-start path)

Locust counts requests by endpoint name (ApiClient passes `name=`), so the
first-view request of a newcomer is reported as `today_first_view` separately
from the steady-state `today`.
"""

import itertools
import threading
from datetime import datetime, timezone
from typing import List, Optional

from sim.click_model import ClickModel, preset
from sim.driver import ApiClient, ApiError, SimAgent
from sim.personas import PopulationConfig, SimUser, generate_population

TODAY_WEIGHT = 9
CLICK_WEIGHT = 1


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class UserPool:
    """Hands out distinct synthetic users; wraps around (re-login) when exhausted.

    The same run_tag + seed yields the same emails, so repeated load runs reuse
    accounts instead of growing the user table.
    """

    def __init__(self, n_users: int, seed: int = 0, run_tag: str = "load"):
        self.users: List[SimUser] = generate_population(
            PopulationConfig(n_users=n_users, seed=seed, n_days=1, late_join_frac=0.0, drift_frac=0.0,
                             run_tag=run_tag))
        self._next = itertools.count()
        self._lock = threading.Lock()

    def take(self) -> SimUser:
        with self._lock:
            return self.users[next(self._next) % len(self.users)]


class LoadUser:
    def __init__(self, user: SimUser, api: ApiClient, model: Optional[ClickModel] = None, seed: int = 0):
        self.agent = SimAgent(user, api, model or ClickModel(preset("default")), seed=seed, fetch_detail=False)
        self.api = api

    def start(self) -> None:
        """signup (400 = account exists -> fine) -> login -> onboarding."""
        self.agent.ensure_account()
        self.agent.onboard(0, now_utc())

    def today(self, endpoint: str = "today") -> int:
        feed = self.api.today(endpoint=endpoint)
        self.agent.last_feed = feed.items
        self.agent.hist.record_view(feed.items[: self.agent.model.cfg.view_depth])
        return len(feed.items)

    def click(self) -> Optional[int]:
        """Clicks one item of the last feed (fetching a feed first if there is none)."""
        if not self.agent.last_feed:
            self.today()
        nid = self.agent.pick_click_for_load(now_utc())
        if nid is None:
            return None
        self.api.click(nid)
        return nid

    def newcomer_flow(self) -> int:
        self.start()
        return self.today(endpoint="today_first_view")


def swallow_api_errors(fn, *args, **kwargs):
    """Failed requests are already recorded (CallRecord / Locust failure); keep the user running."""
    try:
        return fn(*args, **kwargs)
    except ApiError:
        return None
