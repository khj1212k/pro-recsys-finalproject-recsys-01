"""Load-test behaviors shared by the Locust file and its tests.

The same SimUser / ClickModel / ApiClient as the behavior simulation, but driven
by wall-clock time and a fixed task mix instead of per-user session schedules:

  ActiveReader  existing account: /newsletters/today (weight 9) and a click on an
                item from the last feed (weight 1)
  Newcomer      signup -> login -> onboarding -> first /newsletters/today, with a
                fresh synthetic identity every iteration (cold-start path)

Locust counts requests by endpoint name (ApiClient passes `name=`), so the
first-view request of a newcomer is reported as `today_first_view` separately
from the steady-state `today`. Locust's CSV has no notion of *which path*
answered, so every /today response's `X-Rec-Source` header (and whether the list
was empty) is tallied in a SourceTally; the fallback rate per load stage comes
from there (same FALLBACK_SOURCES definition as the behavior metrics).
"""

import itertools
import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from sim.click_model import ClickModel, preset
from sim.driver import ApiClient, ApiError, SimAgent
from sim.metrics import FALLBACK_SOURCES
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


class SourceTally:
    """Counts /today responses by (endpoint, X-Rec-Source) and empty lists; thread-safe."""

    NO_HEADER = "(none)"

    def __init__(self):
        self._lock = threading.Lock()
        self.sources: Dict[str, Counter] = {}
        self.empty: Counter = Counter()

    def reset(self) -> None:
        with self._lock:
            self.sources.clear()
            self.empty.clear()

    def record(self, endpoint: str, source: Optional[str], n_items: int) -> None:
        with self._lock:
            self.sources.setdefault(endpoint, Counter())[source or self.NO_HEADER] += 1
            self.empty[endpoint] += int(n_items == 0)

    def summary(self) -> Dict[str, dict]:
        with self._lock:
            out = {}
            for ep, counts in sorted(self.sources.items()):
                n = sum(counts.values())
                has_header = any(src != self.NO_HEADER for src in counts)
                out[ep] = {
                    "responses": n,
                    "source_counts": dict(counts),
                    "empty_rate": self.empty[ep] / n if n else None,
                    # None, not 0, when the API sends no X-Rec-Source (current main)
                    "fallback_rate": (sum(c for src, c in counts.items() if src in FALLBACK_SOURCES) / n)
                    if has_header and n else None,
                }
            return out

    def write(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8")


class LoadUser:
    def __init__(self, user: SimUser, api: ApiClient, model: Optional[ClickModel] = None, seed: int = 0,
                 tally: Optional[SourceTally] = None):
        self.agent = SimAgent(user, api, model or ClickModel(preset("default")), seed=seed, fetch_detail=False)
        self.api = api
        self.tally = tally

    def start(self) -> None:
        """signup (400 = account exists -> fine) -> login -> onboarding."""
        self.agent.ensure_account()
        self.agent.onboard(0, now_utc())

    def today(self, endpoint: str = "today") -> int:
        feed = self.api.today(endpoint=endpoint)
        if self.tally is not None:
            self.tally.record(endpoint, feed.source, len(feed.items))
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
