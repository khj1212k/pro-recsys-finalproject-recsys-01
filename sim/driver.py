"""HTTP driver: synthetic users exercising the public API contract as a black box.

Routes/payloads mirror backend/app/api/*:
  POST /auth/signup {email,password,nickname,gender,birth_year} -> 201 | 400 (already registered)
  POST /auth/login {email,password} -> {access_token, ...}
  GET  /onboarding/news?category=<code>&limit=6
  PUT  /users/me/newsletters {news_letter_ids}   PUT /users/me/categories {categories}
  GET  /newsletters/today (Bearer)               POST /logs/newsletter/click {news_letter_id} (Bearer)
  GET  /newsletters/{id}
The frontend's onboarding order (newsletters, then categories) is kept.

`session` can be anything with requests' `.request(method, url, **kw)`:
requests.Session, fastapi TestClient, httpx.Client or Locust's HttpSession.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from sim.catalog import Item
from sim.click_model import ClickModel, ExposureHistory
from sim.personas import Profile, SimUser

REC_SOURCE_HEADER = "X-Rec-Source"


@dataclass
class CallRecord:
    endpoint: str
    method: str
    status: Optional[int]
    latency_ms: float
    ok: bool
    user: Optional[int] = None
    error: Optional[str] = None


class ApiError(RuntimeError):
    def __init__(self, endpoint: str, status: Optional[int], detail: str = ""):
        super().__init__(f"{endpoint} failed (status={status}) {detail}".strip())
        self.endpoint = endpoint
        self.status = status


@dataclass
class Feed:
    items: List[Item]
    source: Optional[str]


class ApiClient:
    def __init__(self, session, base_url: str = "", calls: Optional[List[CallRecord]] = None,
                 user_index: Optional[int] = None, locust: bool = False):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.calls = calls if calls is not None else []
        self.user_index = user_index
        self.locust = locust
        self.token: Optional[str] = None
        self._credentials: Optional[Tuple[str, str]] = None

    def _request(self, endpoint: str, method: str, path: str, *, auth: bool = False,
                 expected: Sequence[int] = (200,), retry_auth: bool = True, **kw):
        headers = {"Authorization": f"Bearer {self.token}"} if auth and self.token else {}
        if self.locust:
            kw["name"] = endpoint
        t0 = time.perf_counter()
        try:
            resp = self.session.request(method, self.base_url + path, headers=headers, **kw)
        except Exception as e:  # transport errors are data for the error-rate metric
            self.calls.append(CallRecord(endpoint, method, None, (time.perf_counter() - t0) * 1000, False,
                                         self.user_index, type(e).__name__))
            raise ApiError(endpoint, None, type(e).__name__) from e
        ok = resp.status_code in expected
        self.calls.append(CallRecord(endpoint, method, resp.status_code, (time.perf_counter() - t0) * 1000, ok,
                                     self.user_index, None if ok else _detail(resp)))
        # Tokens expire (ACCESS_TOKEN_EXPIRE_MINUTES); a multi-day run must re-login once.
        if auth and resp.status_code == 401 and retry_auth and self._credentials:
            self.login(*self._credentials)
            return self._request(endpoint, method, path, auth=auth, expected=expected, retry_auth=False, **kw)
        if not ok:
            raise ApiError(endpoint, resp.status_code, _detail(resp))
        return resp

    def signup(self, user: SimUser) -> bool:
        """True if created, False if the account already existed (re-run with same seed)."""
        payload = {"email": user.email, "password": user.password, "nickname": user.nickname,
                   "gender": user.gender, "birth_year": user.birth_year}
        resp = self._request("signup", "POST", "/auth/signup", json=payload, expected=(201, 400))
        return resp.status_code == 201

    def login(self, email: str, password: str) -> str:
        resp = self._request("login", "POST", "/auth/login", json={"email": email, "password": password})
        self.token = resp.json()["access_token"]
        self._credentials = (email, password)
        return self.token

    def onboarding_news(self, category_code: int, limit: int = 6) -> List[Item]:
        resp = self._request("onboarding_news", "GET", "/onboarding/news",
                             params={"category": category_code, "limit": limit})
        return [Item.from_api(p, category_id=category_code) for p in resp.json()]

    def put_newsletters(self, ids: Sequence[int]) -> None:
        self._request("put_newsletters", "PUT", "/users/me/newsletters", auth=True,
                      json={"news_letter_ids": list(ids)})

    def put_categories(self, codes: Sequence[int]) -> None:
        self._request("put_categories", "PUT", "/users/me/categories", auth=True, json={"categories": list(codes)})

    def today(self) -> Feed:
        resp = self._request("today", "GET", "/newsletters/today", auth=True)
        return Feed([Item.from_api(p) for p in resp.json()], resp.headers.get(REC_SOURCE_HEADER))

    def click(self, news_letter_id: int) -> int:
        resp = self._request("click", "POST", "/logs/newsletter/click", auth=True,
                             json={"news_letter_id": news_letter_id})
        return int(resp.json().get("log_id", -1))

    def detail(self, news_letter_id: int) -> dict:
        return self._request("detail", "GET", f"/newsletters/{news_letter_id}").json()


def _detail(resp) -> str:
    try:
        body = resp.json()
        return str(body.get("detail", ""))[:200] if isinstance(body, dict) else ""
    except Exception:
        return ""


@dataclass
class OnboardingRecord:
    user: int
    categories: Tuple[int, ...]
    newsletter_ids: Tuple[int, ...]
    t: datetime


@dataclass
class ViewEvent:
    user: int
    archetype: str
    day: int
    t: datetime
    session: int
    view_in_session: int
    request_seq: int
    item_ids: List[int]
    source: Optional[str]
    ok: bool
    clicked_ids: List[int] = field(default_factory=list)
    clicked_ranks: List[int] = field(default_factory=list)
    clicks_acked: int = 0
    after_click: bool = False
    is_first_view: bool = False
    drifted: bool = False


class SimAgent:
    """One synthetic user's decisions; transport-agnostic so Locust reuses it."""

    def __init__(self, user: SimUser, api: ApiClient, model: ClickModel, seed: int = 0,
                 fetch_detail: bool = True, items: Optional[Dict[int, Item]] = None):
        self.user = user
        self.api = api
        self.model = model
        self.rng = np.random.default_rng([seed, 3, user.index])
        self.hist = ExposureHistory()
        self.fetch_detail = fetch_detail
        self.items = items if items is not None else {}
        self.request_seq = 0
        self.onboarding: Optional[OnboardingRecord] = None
        self.last_feed: List[Item] = []

    def ensure_account(self) -> None:
        self.api.signup(self.user)
        self.api.login(self.user.email, self.user.password)

    def onboard(self, day: int, now: datetime) -> OnboardingRecord:
        profile = self.user.profile_on(day)
        cats = profile.top_categories()
        picked: List[int] = []
        for code in cats:
            shown = self.api.onboarding_news(code, limit=6)
            for it in shown:
                self.items.setdefault(it.news_letter_id, it)
            m = int(self.rng.integers(1, 4))  # frontend allows up to 3 per category
            picks = self.model.choose_top(shown, profile, now, self.hist, self.rng, min(m, len(shown)))
            picked.extend(shown[i].news_letter_id for i in picks)
        self.api.put_newsletters(picked)
        self.api.put_categories(cats)
        self.onboarding = OnboardingRecord(self.user.index, tuple(cats), tuple(picked), now)
        return self.onboarding

    def view(self, day: int, now: datetime, session: int, view_in_session: int, after_click: bool) -> ViewEvent:
        profile = self.user.profile_on(day)
        self.request_seq += 1
        ev = ViewEvent(self.user.index, profile.archetype, day, now, session, view_in_session, self.request_seq,
                       [], None, False, after_click=after_click, is_first_view=self.request_seq == 1,
                       drifted=self.user.is_drifter and profile is self.user.drift_profile)
        try:
            feed = self.api.today()
        except ApiError:
            return ev
        ev.ok, ev.source = True, feed.source
        ev.item_ids = [it.news_letter_id for it in feed.items]
        for it in feed.items:
            self.items[it.news_letter_id] = it
        self.last_feed = feed.items
        clicked = self.model.sample_clicks(feed.items, profile, now, self.hist, self.rng)
        self.hist.record_view(feed.items[: self.model.cfg.view_depth])
        for rank in clicked:
            nid = feed.items[rank].news_letter_id
            ev.clicked_ids.append(nid)
            ev.clicked_ranks.append(rank)
            self.hist.record_click(nid)
            try:
                self.api.click(nid)
                ev.clicks_acked += 1
                if self.fetch_detail:
                    self.api.detail(nid)
            except ApiError:
                pass
        return ev

    def session(self, day: int, now: datetime, session: int, max_views: int) -> List[ViewEvent]:
        """Views continue only while the user clicks (returning to the feed after reading)."""
        out: List[ViewEvent] = []
        after_click = False
        for v in range(max_views):
            ev = self.view(day, now + timedelta(seconds=90 * v), session, v, after_click)
            out.append(ev)
            if not ev.clicked_ids:
                break
            after_click = True
        return out

    def pick_click_for_load(self, now: datetime) -> Optional[int]:
        """Load-test click task: always clicks one item, chosen by the click model."""
        if not self.last_feed:
            return None
        profile = self.user.profile_on(0)
        idx = self.model.choose_top(self.last_feed[: self.model.cfg.view_depth], profile, now, self.hist, self.rng, 1)
        nid = self.last_feed[idx[0]].news_letter_id
        self.hist.record_click(nid)
        return nid


class VirtualClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now


@dataclass(frozen=True)
class SimulationConfig:
    n_days: int = 7
    start: datetime = datetime(2026, 1, 5, tzinfo=timezone.utc)
    max_views_per_session: int = 4
    fetch_detail: bool = True
    seed: int = 0


@dataclass
class SimulationLog:
    users: List[SimUser]
    views: List[ViewEvent]
    calls: List[CallRecord]
    onboarding: Dict[int, OnboardingRecord]
    items: Dict[int, Item]
    config: SimulationConfig
    model_name: str
    drift_starts: Dict[int, datetime] = field(default_factory=dict)


def run_simulation(
    users: Sequence[SimUser],
    make_api: Callable[[SimUser, List[CallRecord]], ApiClient],
    model: ClickModel,
    cfg: SimulationConfig = SimulationConfig(),
    clock: Optional[VirtualClock] = None,
    on_day_end: Optional[Callable[[int], None]] = None,
) -> SimulationLog:
    """Replays n_days of virtual time. Day-0 users onboard before day 0 and get one
    day-end pass (so they are 'existing' users); only mid-simulation joiners are
    cold-start."""
    calls: List[CallRecord] = []
    items: Dict[int, Item] = {}
    agents: Dict[int, SimAgent] = {}
    onboarding: Dict[int, OnboardingRecord] = {}
    views: List[ViewEvent] = []
    drift_starts: Dict[int, datetime] = {}

    def agent_for(u: SimUser) -> SimAgent:
        if u.index not in agents:
            agents[u.index] = SimAgent(u, make_api(u, calls), model, seed=cfg.seed,
                                       fetch_detail=cfg.fetch_detail, items=items)
        return agents[u.index]

    def at(now: datetime) -> datetime:
        if clock is not None:
            clock.set(now)
        return now

    def onboard(u: SimUser, day: int, now: datetime) -> None:
        a = agent_for(u)
        try:
            a.ensure_account()
            onboarding[u.index] = a.onboard(day, now)
        except Exception:  # recorded in calls; the user just stays un-onboarded
            pass

    warm = [u for u in users if u.join_day == 0]
    for u in warm:
        onboard(u, 0, at(cfg.start - timedelta(hours=1)))
    if on_day_end is not None:
        at(cfg.start - timedelta(minutes=1))
        on_day_end(-1)

    for day in range(cfg.n_days):
        day_start = cfg.start + timedelta(days=day)
        events: List[Tuple[float, int, int, str]] = []
        for u in users:
            if u.join_day > day:
                continue
            rng = np.random.default_rng([cfg.seed, 4, u.index, day])
            profile = u.profile_on(day)
            if u.is_drifter and day == u.drift_day:
                drift_starts[u.index] = day_start
            times = sorted(float(x) for x in rng.uniform(0, 86_400 - 600, int(rng.poisson(profile.sessions_per_day))))
            if u.join_day == day and day > 0:
                t_join = float(rng.uniform(0, 43_200))
                events.append((t_join, u.index, 0, "onboard"))
                times = [t_join + 30.0] + [t for t in times if t > t_join + 30.0]
            events.extend((t, u.index, s, "session") for s, t in enumerate(times))
        events.sort()
        by_index = {u.index: u for u in users}
        for t, idx, s, kind in events:
            u = by_index[idx]
            now = at(day_start + timedelta(seconds=t))
            if kind == "onboard":
                onboard(u, day, now)
            elif u.index in agents and agents[u.index].api.token:
                views.extend(agents[u.index].session(day, now, s, cfg.max_views_per_session))
        if on_day_end is not None:
            at(day_start + timedelta(days=1) - timedelta(minutes=1))
            on_day_end(day)

    return SimulationLog(list(users), views, calls, onboarding, items, cfg, model.cfg.name, drift_starts)
