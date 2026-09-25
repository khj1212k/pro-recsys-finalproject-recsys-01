"""In-process fake of the newsletter API for driver tests and offline runs.

Same routes and payload shapes as backend/app/api/*, backed by in-memory state
and a pluggable *toy* recommendation policy. The policies exist to check that
the simulator's metrics can tell known architectures apart - they are not the
production recommender and nothing here measures its quality:

  static_batch           per-user list snapshotted at day end (the current
                         design: /today reads news_letter_today_batch); users
                         without a batch get []
  static_batch_fallback  same, but users without a batch get the popular list
  reactive               request-time re-ranking from onboarding + clicks
  reactive_explore       reactive, plus 3 of the top-10 slots given to
                         categories outside the user's top-2 affinities
  random                 uniformly random candidates

Responses carry an `X-Rec-Source` header (the production API does not - ADR 0019
proposes adding it) so fallback rate can be measured directly.
"""

import itertools
import math
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from sim.catalog import CATEGORIES, Catalog, Item
from sim.driver import REC_SOURCE_HEADER

POLICIES = ("static_batch", "static_batch_fallback", "reactive", "reactive_explore", "random")
EXPLORE_SLOTS = (2, 5, 8)


@dataclass
class _User:
    user_id: int
    email: str
    password: str
    nickname: str
    categories: List[int] = field(default_factory=list)
    onboarding_ids: List[int] = field(default_factory=list)


class FakeBackend:
    def __init__(self, catalog: Catalog, policy: str = "reactive", clock=None, seed: int = 0,
                 today_size: int = 20, expose_press: bool = False):
        if policy not in POLICIES:
            raise ValueError(f"unknown policy {policy!r}; choose from {POLICIES}")
        self.catalog = catalog
        self.policy = policy
        self.clock = clock
        self.seed = seed
        self.today_size = today_size
        self.expose_press = expose_press
        self.lock = threading.RLock()
        self.users: Dict[str, _User] = {}
        self.users_by_id: Dict[int, _User] = {}
        self.tokens: Dict[str, int] = {}
        self.clicks: List[Tuple[int, int, datetime]] = []
        self.user_clicks: Dict[int, List[Tuple[int, datetime]]] = defaultdict(list)
        self.batches: Dict[int, List[int]] = {}
        self._ids = itertools.count(1)
        self._tok = itertools.count(1)
        self._req = itertools.count(1)

    def now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(timezone.utc)

    # -- auth ---------------------------------------------------------------
    def signup(self, email: str, password: str, nickname: str) -> Optional[_User]:
        with self.lock:
            if email in self.users:
                return None
            u = _User(next(self._ids), email, password, nickname)
            self.users[email] = u
            self.users_by_id[u.user_id] = u
            return u

    def login(self, email: str, password: str) -> Optional[str]:
        with self.lock:
            u = self.users.get(email)
            if u is None or u.password != password:
                return None
            tok = f"fake-{u.user_id}-{next(self._tok)}"
            self.tokens[tok] = u.user_id
            return tok

    def user_for(self, authorization: Optional[str]) -> Optional[_User]:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        uid = self.tokens.get(authorization[len("Bearer "):])
        return self.users_by_id.get(uid) if uid is not None else None

    def expire_all_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    # -- recommendation -----------------------------------------------------
    def candidates(self) -> List[Item]:
        return self.catalog.candidates(self.now())

    def popular(self, cands: List[Item], exclude: Set[int] = frozenset()) -> List[int]:
        now = self.now()
        recent = Counter(nid for _, nid, t in self.clicks if now - t <= timedelta(hours=24))
        ranked = sorted(cands, key=lambda it: (-recent.get(it.news_letter_id, 0), -it.created_at.timestamp(),
                                              it.news_letter_id))
        return [it.news_letter_id for it in ranked if it.news_letter_id not in exclude][: self.today_size]

    def _affinity(self, u: _User) -> Optional[Tuple[Dict[int, float], Counter, Set[int]]]:
        now = self.now()
        mine = self.user_clicks.get(u.user_id, [])
        if not u.categories and not mine and not u.onboarding_ids:
            return None
        affinity: Dict[int, float] = defaultdict(float)
        kw: Counter = Counter()
        for c in u.categories:
            affinity[c] += 1.0
        for nid in u.onboarding_ids:
            it = self.catalog.by_id.get(nid)
            if it is not None:
                kw.update(it.keywords)
        for nid, t in mine[-30:]:
            it = self.catalog.by_id.get(nid)
            if it is None:
                continue
            decay = math.exp(-(now - t).total_seconds() / (48 * 3600))
            affinity[it.category_id] += 1.5 * decay
            kw.update({k: decay for k in it.keywords})
        return affinity, kw, {nid for nid, _ in mine}

    def personalized(self, u: _User) -> Optional[List[int]]:
        """Toy content-based scorer; None when the user has no signal at all."""
        signals = self._affinity(u)
        if signals is None:
            return None
        affinity, kw, clicked = signals
        now = self.now()
        top_aff = max(affinity.values()) if affinity else 1.0
        top_kw = max(kw.values()) if kw else 1.0

        def score(it: Item) -> float:
            age_h = max(0.0, (now - it.created_at).total_seconds() / 3600)
            kw_s = sum(kw.get(k, 0.0) for k in it.keywords) / (top_kw * max(len(it.keywords), 1))
            return (affinity.get(it.category_id, 0.0) / top_aff + 0.8 * kw_s
                    + 0.3 * math.exp(-age_h / 24) + 0.05 * math.log1p(it.raw_news_count))

        ranked = sorted((it for it in self.candidates() if it.news_letter_id not in clicked),
                        key=lambda it: (-score(it), it.news_letter_id))
        return [it.news_letter_id for it in ranked[: self.today_size]]

    def with_exploration(self, u: _User, ids: List[int], cands: List[Item]) -> List[int]:
        affinity, _, clicked = self._affinity(u)
        top = set(sorted(affinity, key=lambda c: -affinity[c])[:2])
        head = set(ids[:10])
        pool = [it.news_letter_id for it in cands
                if it.category_id not in top and it.news_letter_id not in head and it.news_letter_id not in clicked]
        if not pool:
            return ids
        rng = np.random.default_rng([self.seed, u.user_id, next(self._req)])
        picks = [pool[int(i)] for i in rng.choice(len(pool), size=min(len(EXPLORE_SLOTS), len(pool)), replace=False)]
        out = [i for i in ids if i not in picks]
        for slot, nid in zip(EXPLORE_SLOTS, picks):
            out.insert(slot, nid)
        return out[: self.today_size]

    def rebuild_batches(self) -> None:
        """Nightly job analogue for the static_batch policies."""
        with self.lock:
            for u in self.users_by_id.values():
                ids = self.personalized(u)
                if ids is not None:
                    self.batches[u.user_id] = ids

    def feed(self, u: _User) -> Tuple[List[int], str]:
        with self.lock:
            cands = self.candidates()
            if self.policy == "random":
                rng = np.random.default_rng([self.seed, u.user_id, next(self._req)])
                pick = rng.choice(len(cands), size=min(self.today_size, len(cands)), replace=False) if cands else []
                return [cands[int(i)].news_letter_id for i in pick], "random"
            if self.policy in ("reactive", "reactive_explore"):
                ids = self.personalized(u)
                if ids is None:
                    return self.popular(cands), "fallback"
                if self.policy == "reactive_explore":
                    ids = self.with_exploration(u, ids, cands)
                return ids, "personalized"
            batch = self.batches.get(u.user_id)
            if batch:
                return list(batch), "batch"
            if self.policy == "static_batch_fallback":
                return self.popular(cands), "fallback"
            return [], "none"

    def record_click(self, u: _User, nid: int) -> int:
        with self.lock:
            now = self.now()
            self.clicks.append((u.user_id, nid, now))
            self.user_clicks[u.user_id].append((nid, now))
            return len(self.clicks)


def create_fake_app(backend: FakeBackend):
    from fastapi import FastAPI, Header, HTTPException, Query, Response
    from pydantic import BaseModel, Field

    class SignupBody(BaseModel):
        email: str
        password: str = Field(min_length=8)
        nickname: str
        gender: Optional[str] = None
        birth_year: Optional[int] = None

    class LoginBody(BaseModel):
        email: str
        password: str

    class CategoriesBody(BaseModel):
        categories: List[int]

    class NewslettersBody(BaseModel):
        news_letter_ids: List[int]

    class ClickBody(BaseModel):
        news_letter_id: int

    app = FastAPI(title="newsletter API fake (simulator)")
    app.state.backend = backend

    def require_user(authorization: Optional[str]) -> _User:
        u = backend.user_for(authorization)
        if u is None:
            raise HTTPException(status_code=401, detail="Could not validate credentials")
        return u

    @app.post("/auth/signup", status_code=201)
    async def signup(body: SignupBody):
        u = backend.signup(body.email, body.password, body.nickname)
        if u is None:
            raise HTTPException(status_code=400, detail="Email already registered")
        return {"user_id": u.user_id, "user_email": u.email, "user_nickname": u.nickname,
                "user_gender_code": 0, "user_birth_year": body.birth_year}

    @app.post("/auth/login")
    async def login(body: LoginBody):
        tok = backend.login(body.email, body.password)
        if tok is None:
            raise HTTPException(status_code=401, detail="Incorrect email or password")
        u = backend.users[body.email]
        return {"access_token": tok, "token_type": "bearer", "user_id": u.user_id,
                "user_email": u.email, "user_nickname": u.nickname}

    @app.get("/onboarding/news")
    async def onboarding_news(category: int = Query(...), limit: int = Query(6)):
        if category not in CATEGORIES:
            raise HTTPException(status_code=404, detail="Category code not found")
        cands = [it for it in backend.candidates() if it.category_id == category]
        by_id = backend.catalog.by_id
        out = []
        for nid in backend.popular(cands)[:limit]:
            p = by_id[nid].to_api()
            out.append({k: p[k] for k in ("news_letter_id", "news_letter_title", "news_letter_sentence",
                                          "news_letter_keywords", "news_letter_created_at")})
        return out

    @app.put("/users/me/categories")
    async def put_categories(body: CategoriesBody, authorization: Optional[str] = Header(None)):
        u = require_user(authorization)
        with backend.lock:
            u.categories = [c for c in body.categories if c in CATEGORIES]
        return {"message": "Categories updated successfully", "updated_categories": body.categories}

    @app.put("/users/me/newsletters")
    async def put_newsletters(body: NewslettersBody, authorization: Optional[str] = Header(None)):
        u = require_user(authorization)
        with backend.lock:
            u.onboarding_ids = list(body.news_letter_ids)
        return {"message": "Newsletters updated successfully", "updated_newsletters": body.news_letter_ids}

    @app.get("/newsletters/today")
    async def today(response: Response, authorization: Optional[str] = Header(None)):
        u = require_user(authorization)
        ids, source = backend.feed(u)
        response.headers[REC_SOURCE_HEADER] = source
        return [backend.catalog.by_id[i].to_api(include_press=backend.expose_press) for i in ids]

    @app.get("/newsletters/{news_letter_id}")
    async def detail(news_letter_id: int):
        it = backend.catalog.by_id.get(news_letter_id)
        if it is None:
            raise HTTPException(status_code=404, detail="Newsletter not found")
        return {**it.to_api(), "news_letter_content": it.sentence}

    @app.post("/logs/newsletter/click")
    async def click(body: ClickBody, authorization: Optional[str] = Header(None)):
        u = require_user(authorization)
        if body.news_letter_id not in backend.catalog.by_id:
            # the real table has an FK to news_letter; an unknown id is a 500 there
            raise HTTPException(status_code=500, detail="foreign key violation")
        return {"status": "success", "log_id": backend.record_click(u, body.news_letter_id)}

    @app.post("/__sim__/day_end")
    async def day_end():
        backend.rebuild_batches()
        return {"batches": len(backend.batches)}

    return app
