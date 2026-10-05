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

The measured window of a stage starts when every reader has finished its account
setup (ReadyGate), not when Locust has spawned the users.
"""

import itertools
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

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
        self.skipped_clicks = 0

    def reset(self) -> None:
        with self._lock:
            self.sources.clear()
            self.empty.clear()
            self.skipped_clicks = 0

    def record(self, endpoint: str, source: Optional[str], n_items: int) -> None:
        with self._lock:
            self.sources.setdefault(endpoint, Counter())[source or self.NO_HEADER] += 1
            self.empty[endpoint] += int(n_items == 0)

    def record_skipped_click(self) -> None:
        """A click task that had nothing to click (the reader's feed was empty)."""
        with self._lock:
            self.skipped_clicks += 1

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
                    # None, not 0, when the API sends no X-Rec-Source (the batch-only /today)
                    "fallback_rate": (sum(c for src, c in counts.items() if src in FALLBACK_SOURCES) / n)
                    if has_header and n else None,
                }
            return out

    def write(self, path: Path, window: Optional[dict] = None) -> None:
        """Stage report: per-endpoint source tally, skipped clicks and the measured window (ReadyGate.summary)."""
        report = {"window": window or {}, "skipped_clicks": self.skipped_clicks, "endpoints": self.summary()}
        Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


class ReadyGate:
    """Keeps the measured window of a load stage closed until every reader has finished account setup.

    Locust's `--reset-stats` resets when the users are *spawned* (spawning_complete), not when
    their on_start has returned. With `-u N -r N` every reader's signup, bcrypt login and
    onboarding calls are still in flight at that moment: they would land in the stage's
    statistics and compete with the first /today requests, inflating p95/p99 most at the
    highest stage. So readers report here when their setup is done (or failed) and then block;
    the gate opens once all of them have reported, and the callbacks registered with on_open
    (statistics reset) run right before the readers are released.

    Works with real threads and, inside Locust, with gevent's patched threading primitives.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._released = threading.Event()
        self._callbacks: List[Callable[[], None]] = []
        self.expected: Optional[int] = None
        self.done = 0
        self.failed = 0
        self.timed_out = False
        self.started_at = clock()
        self.opened_at: Optional[float] = None

    def on_open(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def start(self) -> None:
        """Marks the start of the stage (test start); setup time is counted from here."""
        self.started_at = self._clock()

    def expect(self, n_readers: int) -> None:
        """Called once spawning is complete: how many readers have to report before the gate opens."""
        with self._lock:
            self.expected = n_readers
        self._open_if_ready()

    def reader_done(self, ok: bool) -> None:
        with self._lock:
            self.done += 1
            self.failed += int(not ok)
        self._open_if_ready()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Blocks until the gate is open. After `timeout` seconds the gate is forced open and
        flagged, so one reader stuck in setup cannot leave the whole stage idle. Returns False
        when the window was opened by a timeout."""
        if not self._released.wait(timeout):
            self._open_if_ready(force=True)
        return not self.timed_out

    def _open_if_ready(self, force: bool = False) -> None:
        with self._lock:
            if self.opened_at is not None:
                return
            if not force and (self.expected is None or self.done < self.expected):
                return
            self.opened_at = self._clock()
            self.timed_out = force
        for callback in self._callbacks:
            callback()
        self._released.set()

    @property
    def is_open(self) -> bool:
        return self._released.is_set()

    def summary(self) -> dict:
        """setup_s: stage start -> gate open; measured_s: gate open -> now (call at test stop)."""
        opened = self.opened_at
        return {
            "readers_expected": self.expected,
            "readers_done": self.done,
            "readers_failed_setup": self.failed,
            "ready_timeout": self.timed_out,
            "setup_s": None if opened is None else opened - self.started_at,
            "measured_s": None if opened is None else self._clock() - opened,
        }


class LoadUser:
    def __init__(self, user: SimUser, api: ApiClient, model: Optional[ClickModel] = None, seed: int = 0,
                 tally: Optional[SourceTally] = None):
        self.agent = SimAgent(user, api, model or ClickModel(preset("default")), seed=seed, fetch_detail=False)
        self.api = api
        self.tally = tally
        self._fetched_feed = False

    def start(self) -> None:
        """signup (400 = account exists -> fine) -> login -> onboarding."""
        self.agent.ensure_account()
        self.agent.onboard(0, now_utc())

    def today(self, endpoint: str = "today") -> int:
        feed = self.api.today(endpoint=endpoint)
        if self.tally is not None:
            self.tally.record(endpoint, feed.source, len(feed.items))
        self.agent.last_feed = feed.items
        self._fetched_feed = True
        self.agent.hist.record_view(feed.items[: self.agent.model.cfg.view_depth])
        return len(feed.items)

    def click(self) -> Optional[int]:
        """Clicks one item of the last feed.

        A reader that has not read yet fetches a feed first. If the last feed is empty there is
        nothing to click: the task is counted as a skipped click instead of being turned into
        one more /today, which would silently change the task mix (on an API that answers new
        users with an empty list every click task would become a read)."""
        if not self._fetched_feed:
            self.today()
        nid = self.agent.pick_click_for_load(now_utc())
        if nid is None:
            if self.tally is not None:
                self.tally.record_skipped_click()
            return None
        self.api.click(nid)
        return nid

    def newcomer_flow(self) -> int:
        self.start()
        return self.today(endpoint="today_first_view")


def setup_reader(load: LoadUser, gate: ReadyGate, timeout_s: Optional[float] = None) -> bool:
    """A reader's on_start: account setup, report to the gate, then wait for the other readers.

    A failed setup is already a recorded request failure; the reader still reports (so the gate
    opens) and the stage report counts it under readers_failed_setup."""
    ok = False
    try:
        load.start()
        ok = True
    except ApiError:
        pass
    finally:
        gate.reader_done(ok)
    gate.wait(timeout_s)
    return ok


def swallow_api_errors(fn, *args, **kwargs):
    """Failed requests are already recorded (CallRecord / Locust failure); keep the user running."""
    try:
        return fn(*args, **kwargs)
    except ApiError:
        return None
