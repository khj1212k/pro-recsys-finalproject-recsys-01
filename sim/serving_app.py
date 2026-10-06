"""The simulator's in-process app with the REAL request-time recommender behind /newsletters/today.

[SIM] - system-response evidence only, no accuracy claims (ADR 0019). Setup fixed in ADR 0025 A1.

What is production code here: backend/app/recsys - `RecommendationService` (result cache, time
budget, fallback chain), `RealtimeRecommender.rank` (user state, candidate union, exclusion, fatigue
rule, scorer stack with a shadow, MMR), `exploration.plan_slate` (the ε-uniform slots and their
closed-form propensities) and `log_impressions` (the request row and the slot rows of logging v2).

What is not: the repository (an in-memory one that answers the same questions as the SQL one, from
the simulator's state), the HTTP routes (sim.fake_app), the clock (virtual), and the embeddings
(sim.sim_embeddings - a keyword/category hash, not BGE-M3).

Which serving path this is. The registered E9/E10 results (reports/sim/, CI run 37400003072) were
produced by this harness against the serving code before ADR 0033: a nightly long-term vector and a
short-term mean vector read through `long_term_and_categories` / `short_term_vector`. The repository
below follows the contract after ADR 0033 instead: the long-term profile is the incremental state
updated at every click (`profile_state`), the short-term vector is built by the pipeline from
`recent_clicks` (clicks strictly before the request), and `item_window_counts` exists for the
feature adapter. That is a different world from the one ADR 0025 A1.1 registered, so a run of this
file does not reproduce those reports and is not a re-run of them (ADR 0025 A1.6, 2026-10-06).
Not wired in here: the serving feature adapter (`feature_fn` is off, slot logs keep the heuristic's
four terms) and the out-of-path shadow thread (the shadow scores in the request path, which keeps a
run a pure function of its spec).

Three things are added around the service for the off-policy experiments, none of which changes
what policy A serves:
- target slates: every time a ranking is computed, the slates of the target policies are computed
  from the same user state and the same eligible set E and kept next to the answer;
- a ranking override: when a target policy is run for real, its slate replaces the deterministic
  ranking (candidates, exclusion, cache and logging stay the production code);
- a click-model probe: when the simulated user looks at an answer, the click probabilities of the
  shown slate and of the target slates for that same user at that same moment are recorded.
"""

import itertools
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1] / "backend")
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.recsys.config import RecsysConfig  # noqa: E402
from app.recsys.metrics import RecsysCounters  # noqa: E402
from app.recsys.pipeline import RealtimeRecommender  # noqa: E402
from app.recsys.scoring import HeuristicScorer, HeuristicWeights, ScorerStack  # noqa: E402
from app.recsys.service import RecommendationService  # noqa: E402
from app.recsys.types import POLICY_NONE, DeterministicList, NewsletterMeta, ProfileState  # noqa: E402
from app.recsys.types import Item as RecsysItem  # noqa: E402
from recsys_core.profile import NO_CATEGORY, apply_event  # noqa: E402
from recsys_core.serving import ClickEvent, WindowCounts, epoch_seconds  # noqa: E402

from sim.catalog import Catalog, Item  # noqa: E402
from sim.click_model import ClickModel, ClickModelConfig  # noqa: E402
from sim.fake_app import FakeBackend, TodayAnswer, _User  # noqa: E402
from sim.sim_embeddings import catalog_embeddings  # noqa: E402

TARGET_POLICIES = ("random", "reactive", "shadow_recency")
SHADOW_VERSION = "heuristic-recency-sim"
# ADR 0025 A1.2: the shadow-ranker stand-in. Same four terms as the active heuristic, other weights.
SHADOW_WEIGHTS = HeuristicWeights(long_term=0.10, short_term=0.10, recency=0.60, popularity=0.20,
                                  recency_tau_hours=48.0)
EXPLORE_STREAM = 11  # default_rng([seed, 11, request counter]) draws the exploration slots
RANDOM_POLICY_STREAM = 23  # default_rng([seed, 23, ranking counter]) draws the random policy's slate
# The wall-clock budget is not under test: generous, so a slow runner cannot turn answers into fallbacks.
TIME_BUDGET_MS = 600_000


class ShadowRecencyScorer(HeuristicScorer):
    version = SHADOW_VERSION

    def __init__(self):
        super().__init__(SHADOW_WEIGHTS)


def serving_config(explore_slots: int = 2, fatigue_mode: str = "log") -> RecsysConfig:
    """Policy A's configuration: RecsysConfig defaults, exploration and fatigue mode as given.
    explore_slots = 0 turns exploration off (the target policies run without it)."""
    return RecsysConfig(
        mode="realtime",
        time_budget_ms=TIME_BUDGET_MS,
        explore_enabled=explore_slots > 0,
        explore_slots=max(explore_slots, 0),
        fatigue_mode=fatigue_mode,
        feature_fn=None,  # the serving feature adapter is not part of this harness (see the module docstring)
    )


# ------------------------------------------------------------------------------- repository


class SimRecsysRepository:
    """app.recsys.repository.RecsysRepository over the simulator's state.

    Mirrors SqlRecsysRepository: the same windows, orders and aggregates, read at the virtual now.
    The whole virtual week is in the catalog up front, so newsletters "published" after now are
    hidden - they would not exist yet. Category ids are the category codes."""

    def __init__(self, world: "ServingBackend"):
        self.w = world

    def rollback(self) -> None:
        pass

    # -- item windows: catalog rows are sorted by created_at
    def _visible(self) -> int:
        return int(np.searchsorted(self.w.item_ts, self.w.now().timestamp(), side="right"))

    def _window(self, since: datetime) -> Tuple[int, int]:
        hi = self._visible()
        lo = int(np.searchsorted(self.w.item_ts, since.timestamp(), side="left"))
        return min(lo, hi), hi

    def _is_visible(self, nid: int) -> bool:
        row = self.w.item_row.get(int(nid))
        return row is not None and row < self._visible()

    # -- user state
    def last_click_id(self, user_id: int) -> Optional[int]:
        clicks = self.w.clicks_by_user.get(user_id)
        return clicks[-1][0] if clicks else None

    def profile_state(self, user_id: int) -> Tuple[ProfileState, List[int]]:
        u = self.w.users_by_id.get(user_id)
        if u is None:
            return ProfileState(), []
        return self.w.profile_states.get(user_id, ProfileState()), sorted(u.categories)

    def recent_clicks(self, user_id: int, since: datetime, until: datetime, limit: int) -> List[ClickEvent]:
        """Clicks in [since, until), the latest `limit` by (whole second, newsletter id, log id) - the SQL order."""
        clicks = sorted(
            ((epoch_seconds(at), nid, log_id, at) for log_id, nid, at in self.w.clicks_by_user.get(user_id, ())
             if since <= at < until),
            reverse=True,
        )[:limit]
        return [ClickEvent(at, self.w.embeddings[nid], nid) for _, nid, _, at in clicks]

    def item_window_counts(self, news_letter_ids: Sequence[int], click_starts: Sequence[datetime],
                           inview_start: datetime, end: datetime) -> Dict[int, WindowCounts]:
        """Clicks in [click_starts[j], end) and impressions in [inview_start, end), all users. Items without a
        row in any window are left out. Nothing in this harness reads it (the feature adapter is off)."""
        out: Dict[int, WindowCounts] = {}
        for nid in news_letter_ids:
            clicked_at = self.w.clicks_by_item.get(int(nid), ())
            clicks = tuple(sum(1 for at in clicked_at if start <= at < end) for start in click_starts)
            inviews = sum(1 for at in self.w.impressions_by_item.get(int(nid), ()) if inview_start <= at < end)
            if any(clicks) or inviews:
                out[int(nid)] = WindowCounts(clicks, inviews)
        return out

    def onboarding_vector(self, user_id: int) -> Optional[np.ndarray]:
        u = self.w.users_by_id.get(user_id)
        return None if u is None else self.w.mean_embedding(u.onboarding_ids)

    def category_centroid(self, category_ids: Sequence[int], since: datetime) -> Optional[np.ndarray]:
        lo, hi = self._window(since)
        wanted = set(category_ids)
        rows = [r for r in range(lo, hi) if self.w.item_cat[r] in wanted]
        return self.w.item_emb[rows].mean(axis=0) if rows else None

    # -- candidates
    def knn_ids(self, query: np.ndarray, since: datetime, k: int) -> List[int]:
        lo, hi = self._window(since)
        if hi <= lo:
            return []
        q = np.asarray(query, dtype=np.float64)
        norm = float(np.linalg.norm(q))
        sims = self.w.item_emb[lo:hi] @ (q / norm if norm > 0 else q)
        ids = self.w.item_ids[lo:hi]
        order = np.lexsort((ids, -sims))  # cosine distance ascending, ties by id
        return [int(i) for i in ids[order[:k]]]

    def recent_ids(self, n: int) -> List[int]:
        hi = self._visible()
        return [int(i) for i in self.w.item_ids[max(0, hi - n):hi][::-1]]

    def window_meta(self, since: datetime) -> List[NewsletterMeta]:
        lo, hi = self._window(since)
        return self.w.item_meta[lo:hi]

    def category_recent_ids(self, category_ids: Sequence[int], since: datetime, n: int) -> List[int]:
        lo, hi = self._window(since)
        wanted = set(category_ids)
        out: List[int] = []
        for r in range(hi - 1, lo - 1, -1):
            if self.w.item_cat[r] in wanted:
                out.append(int(self.w.item_ids[r]))
                if len(out) >= n:
                    break
        return out

    # -- exclusion and display
    def clicked_among(self, user_id: int, news_letter_ids: Sequence[int]) -> Set[int]:
        return self.w.clicked_by_user.get(user_id, set()) & set(news_letter_ids)

    def fatigued_among(self, user_id: int, news_letter_ids: Sequence[int], since: datetime,
                       min_impressions: int) -> Set[int]:
        wanted = set(news_letter_ids)
        seen: Dict[int, int] = {}
        for at, nid in reversed(self.w.impressions_by_user.get(user_id, ())):
            if at < since:
                break  # rows are appended in time order
            if nid in wanted:
                seen[nid] = seen.get(nid, 0) + 1
        return {nid for nid, n in seen.items() if n >= min_impressions}

    def displayable_among(self, news_letter_ids: Sequence[int]) -> Set[int]:
        return {int(i) for i in news_letter_ids if self._is_visible(i)}

    def items(self, news_letter_ids: Sequence[int]) -> Dict[int, RecsysItem]:
        out: Dict[int, RecsysItem] = {}
        for nid in news_letter_ids:
            if self._is_visible(nid):
                it = self.w.catalog.by_id[int(nid)]
                out[int(nid)] = RecsysItem(int(nid), self.w.embeddings[int(nid)], it.created_at, it.raw_news_count,
                                           it.category_id)
        return out

    def latest_batch(self, user_id: int):
        return None  # no nightly batch rows in this world (realtime mode does not read them)


# ------------------------------------------------------------------------- target policies


@dataclass
class RankContext:
    """What a ranking function over E may read: the same state the deterministic ranking was made from."""

    user: _User
    now: datetime
    det: DeterministicList
    seq: int  # counter of ranking computations in this run

    @property
    def eligible(self) -> List[int]:
        return self.det.eligible_ids.tolist()


class PolicyRecommender(RealtimeRecommender):
    """RealtimeRecommender whose deterministic ranking can be replaced by a target policy's, and which
    keeps the target policies' slates for every ranking it computes (ADR 0025 A1.2).

    With ranking=None the answer is exactly the production ranking (policy A)."""

    def __init__(self, cfg: RecsysConfig, scorer, counters: RecsysCounters, world: "ServingBackend",
                 ranking: Optional[str] = None, targets: Sequence[str] = ()):
        super().__init__(cfg, scorer=scorer, counters=counters)
        for name in (*targets, *([ranking] if ranking else [])):
            if name not in TARGET_POLICIES:
                raise ValueError(f"unknown target policy {name!r}; choose from {TARGET_POLICIES}")
        self.world = world
        self.ranking = ranking
        self.targets = tuple(targets)
        self._seq = itertools.count()

    def rank(self, repo, user_id, now, deadline) -> DeterministicList:
        det = super().rank(repo, user_id, now, deadline)
        ctx = RankContext(self.world.users_by_id[user_id], now, det, next(self._seq))
        slates = {name: self.slate(name, ctx) for name in self.targets}
        if self.ranking is not None:
            det = replace(det, ranked_ids=self.slate(self.ranking, ctx))
        self.world.last_rank[user_id] = (det, slates)
        return det

    # A target policy is a ranking function over E; its top_k is the slate. No exploration.
    def slate(self, name: str, ctx: RankContext) -> List[int]:
        return getattr(self, f"_slate_{name}")(ctx)[: self.cfg.top_k]

    def _slate_random(self, ctx: RankContext) -> List[int]:
        eligible = ctx.eligible
        rng = np.random.default_rng([self.world.seed, RANDOM_POLICY_STREAM, ctx.seq])
        picks = rng.choice(len(eligible), size=min(self.cfg.top_k, len(eligible)), replace=False)
        return [eligible[int(i)] for i in picks]

    def _slate_reactive(self, ctx: RankContext) -> List[int]:
        scorer = self.world.toy_scorer(ctx.user)
        if scorer is None:  # no onboarding, no clicks: nothing to rank by
            return list(ctx.det.ranked_ids)
        score, _ = scorer  # E already excludes what the user clicked
        by_id = self.world.catalog.by_id
        return sorted(ctx.eligible, key=lambda nid: (-score(by_id[nid]), nid))

    def _slate_shadow_recency(self, ctx: RankContext) -> List[int]:
        scores = ctx.det.extra_scores.get(SHADOW_VERSION)
        if scores is None:  # the no-signal path does not run the scorer stack
            return list(ctx.det.ranked_ids)
        eligible = ctx.eligible
        embeddings = np.stack([self.world.embeddings[nid] for nid in eligible])
        picked = self.reranker.rerank_for_user(scores, embeddings, self.cfg.top_k, len(ctx.user.categories))
        return [eligible[int(idx)] for idx, _ in picked]


# ---------------------------------------------------------------------------------- backend


@dataclass
class ResponseRecord:
    """One /newsletters/today answer as the experiment sees it (the logs hold the rest)."""

    request_id: str
    user_id: int
    at: datetime
    shown: List[int]
    planned: bool  # False for a fallback answer: no exploration policy made this slate
    targets: Dict[str, List[int]] = field(default_factory=dict)
    # filled by the click-model probe when the simulated user looks at the answer
    expected_shown: Optional[List[float]] = None
    expected_targets: Dict[str, float] = field(default_factory=dict)  # sum over the target slate


class SlotColumns:
    """The slot rows of logging v2, column-wise (a 7-day run writes about 150k of them).

    Kept per row: what the analyses read. Of `scores_shadow` only the stand-in shadow's score is
    kept and of `features` only its length - enough to check that the service logged both."""

    FIELDS = ("request_id", "user_id", "news_letter_id", "position", "explored", "propensity", "det_rank",
              "score", "shadow_score", "n_features", "created_at")

    def __init__(self):
        for name in self.FIELDS:
            setattr(self, name, [])

    def append(self, row: dict, created_at: datetime) -> None:
        shadow = row.get("scores_shadow") or {}
        features = row.get("features")
        self.request_id.append(row["request_id"])
        self.user_id.append(row["user_id"])
        self.news_letter_id.append(row["news_letter_id"])
        self.position.append(row["position"])
        self.explored.append(bool(row["explored"]))
        self.propensity.append(row["propensity"])
        self.det_rank.append(row["det_rank"])
        self.score.append(row["score"])
        self.shadow_score.append(shadow.get(SHADOW_VERSION))
        self.n_features.append(0 if features is None else len(features) // 4)  # float32 bytes
        self.created_at.append(created_at)

    def __len__(self) -> int:
        return len(self.request_id)

    def rows(self) -> List[dict]:
        return [dict(zip(self.FIELDS, values)) for values in zip(*(getattr(self, f) for f in self.FIELDS))]


@dataclass
class ClickRow:
    log_id: int
    user_id: int
    news_letter_id: int
    at: datetime
    request_id: Optional[str]
    position: Optional[int]


class ServingBackend(FakeBackend):
    """sim.fake_app's backend with the production recommendation service behind /newsletters/today."""

    policies = ("serving",)

    def __init__(self, catalog: Catalog, clock, seed: int = 0, cfg: Optional[RecsysConfig] = None,
                 ranking: Optional[str] = None, targets: Sequence[str] = ()):
        super().__init__(catalog, policy="serving", clock=clock, seed=seed)
        self.cfg = cfg or serving_config()

        # catalog as arrays, in created_at order (sim.catalog sorts it; checked here because the windows rely on it)
        self.embeddings: Dict[int, np.ndarray] = catalog_embeddings(catalog)
        self.item_ts = np.asarray([it.created_at.timestamp() for it in catalog.items], dtype=np.float64)
        if np.any(np.diff(self.item_ts) < 0):
            raise ValueError("catalog items must be sorted by created_at")
        self.item_ids = np.asarray([it.news_letter_id for it in catalog.items], dtype=np.int64)
        self.item_cat = [it.category_id for it in catalog.items]
        self.item_emb = np.stack([self.embeddings[int(i)] for i in self.item_ids]).astype(np.float64)
        self.item_row = {int(nid): r for r, nid in enumerate(self.item_ids)}
        self.item_meta = [NewsletterMeta(it.news_letter_id, it.created_at, it.raw_news_count) for it in catalog.items]

        # state the repository reads
        self.profile_states: Dict[int, ProfileState] = {}  # user_profile_state: updated at every click
        self.clicks_by_user: Dict[int, List[Tuple[int, int, datetime]]] = {}  # (log_id, newsletter, at)
        self.clicked_by_user: Dict[int, Set[int]] = {}
        self.clicks_by_item: Dict[int, List[datetime]] = {}
        self.impressions_by_user: Dict[int, List[Tuple[datetime, int]]] = {}
        self.impressions_by_item: Dict[int, List[datetime]] = {}
        # event timestamps (see now()): the driver's instant the last event was stamped at, and its stamp
        self._stamp_base: Optional[datetime] = None
        self._stamp: Optional[datetime] = None

        # logs, as the service wrote them
        self.request_rows: List[dict] = []
        self.slots = SlotColumns()
        self.click_rows: List[ClickRow] = []

        # experiment bookkeeping
        self.responses: Dict[str, ResponseRecord] = {}
        self.response_order: List[str] = []
        self.last_rank: Dict[int, Tuple[DeterministicList, Dict[str, List[int]]]] = {}
        self.pending: Optional[ResponseRecord] = None
        self.probe_mismatches = 0
        self._driver_items: Dict[int, Item] = {}

        self.repo = SimRecsysRepository(self)
        self.counters = RecsysCounters()
        stack = ScorerStack(HeuristicScorer(), [ShadowRecencyScorer()],
                            deadline_fraction=self.cfg.shadow_deadline_fraction, counters=self.counters)
        self.recommender = PolicyRecommender(self.cfg, stack, self.counters, self, ranking=ranking, targets=targets)
        explore_draws = itertools.count()

        @contextmanager
        def repo_scope():
            yield self.repo

        self.service = RecommendationService(
            self.cfg,
            repo_factory=repo_scope,
            recommender=self.recommender,
            counters=self.counters,
            impression_writer=self.write_logs,
            now_fn=self.now,  # the popularity window and freshness follow the virtual clock
            clock=lambda: self.now().timestamp(),  # ...and so does the result cache's TTL
            rng_factory=lambda request_id: np.random.default_rng([seed, EXPLORE_STREAM, next(explore_draws)]),
        )

    def close(self) -> None:
        self.service.shutdown()

    # -- time
    def now(self) -> datetime:
        """The virtual clock, with ties broken in arrival order.

        The driver sets the clock once per session, so every /today request and click of a session
        would carry the same instant. The serving path reads "clicks strictly before the request"
        (RecsysRepository.recent_clicks), and a click that is not strictly earlier than the next
        request would be missing from that request's state - which no real clock does. So each
        request and each click is stamped one microsecond after the previous event of the same
        instant (stamp_event). Only this backend does it: sim.driver and the toy backends of
        ADR 0019 keep their clock, and so do their numbers."""
        base = super().now()
        return self._stamp if base == self._stamp_base and self._stamp is not None else base

    def stamp_event(self) -> datetime:
        """Called once when a /today request or a click arrives; now() returns this stamp until the next event."""
        base = super().now()
        if base == self._stamp_base:
            self._stamp += timedelta(microseconds=1)
        else:
            # A new instant. If the previous instant's events already ran past it (two sessions a few
            # microseconds apart), keep going from the last stamp so the stamps never go back.
            ran_past = self._stamp is not None and self._stamp_base < base <= self._stamp
            self._stamp = self._stamp + timedelta(microseconds=1) if ran_past else base
            self._stamp_base = base
        return self._stamp

    # -- helpers
    def mean_embedding(self, ids: Sequence[int]) -> Optional[np.ndarray]:
        vecs = [self.embeddings[i] for i in ids if i in self.embeddings]
        return np.mean(vecs, axis=0) if vecs else None

    def driver_item(self, nid: int) -> Item:
        """The Item the driver builds from this newsletter's API payload (no press unless exposed)."""
        it = self._driver_items.get(nid)
        if it is None:
            it = Item.from_api(self.catalog.by_id[nid].to_api(include_press=self.expose_press))
            self._driver_items[nid] = it
        return it

    # -- /newsletters/today: the real route's three steps (recommend, display, log after the response)
    def respond_today(self, u: _User) -> TodayAnswer:
        self.stamp_event()
        rec = self.service.recommend(u.user_id, fallback_repo=self.repo)
        # Display stage: every id the repository returns is displayable, so only the 20-item cap applies.
        shown = list(rec.news_letter_ids)[: self.today_size]
        planned = rec.policy_version != POLICY_NONE
        _, slates = self.last_rank.get(u.user_id, (None, {}))
        record = ResponseRecord(rec.request_id, u.user_id, self.now(), shown, planned,
                                targets=dict(slates) if planned else {})
        self.responses[rec.request_id] = record
        self.response_order.append(rec.request_id)
        self.pending = record
        return TodayAnswer(shown, rec.source, rec.request_id,
                           after_response=lambda: self.service.log_impressions(u.user_id, rec, shown))

    def write_logs(self, rows: List[dict], request_row: Optional[dict] = None) -> None:
        """The impression writer: what SqlImpressionWriter would insert, with created_at = now."""
        now = self.now()
        if request_row is not None:
            self.request_rows.append(dict(request_row, created_at=now))
        for row in rows:
            self.slots.append(row, now)
            self.impressions_by_user.setdefault(row["user_id"], []).append((now, row["news_letter_id"]))
            self.impressions_by_item.setdefault(row["news_letter_id"], []).append(now)

    def record_click(self, u: _User, nid: int, request_id: Optional[str] = None,
                     position: Optional[int] = None) -> int:
        with self.lock:
            now = self.stamp_event()
            log_id = super().record_click(u, nid, request_id, position)
            self.click_rows.append(ClickRow(log_id, u.user_id, nid, now, request_id, position))
            self.clicks_by_user.setdefault(u.user_id, []).append((log_id, nid, now))
            self.clicked_by_user.setdefault(u.user_id, set()).add(nid)
            self.clicks_by_item.setdefault(nid, []).append(now)
            self.apply_click(u.user_id, nid, now)
            return log_id

    def apply_click(self, user_id: int, nid: int, clicked_at: datetime) -> None:
        """The click API's profile update (app.recsys.profile_store.apply_click): the long-term state takes
        the click in the same step that writes the click row. The arithmetic is recsys_core.profile's."""
        state = self.profile_states.get(user_id, ProfileState())
        category = self.catalog.by_id[nid].category_id
        hist = apply_event(state.hist, epoch_seconds(clicked_at), self.embeddings[nid],
                           NO_CATEGORY if category is None else int(category))
        last = clicked_at if state.last_event_at is None else max(state.last_event_at, clicked_at)
        self.profile_states[user_id] = ProfileState(hist, last)

    def day_end(self) -> None:
        """Nothing runs at night for the request path: the long-term profile is kept up to date click by
        click (ADR 0033). Before that, this was the nightly long-term vector of ADR 0025 A1.1."""

    # -- click-model probe
    def probe_view(self, model: ClickModel, items: Sequence[Item], profile, now: datetime, hist) -> None:
        """Called when a simulated user looks at an answer, before any click is drawn: the click
        probabilities of the shown slate, and of each target slate had this user seen it instead."""
        record, self.pending = self.pending, None
        if record is None or [it.news_letter_id for it in items] != record.shown:
            self.probe_mismatches += 1
            return
        record.expected_shown = model.list_probs(items, profile, now, hist)
        for name, slate in record.targets.items():
            probs = model.list_probs([self.driver_item(nid) for nid in slate], profile, now, hist)
            record.expected_targets[name] = float(sum(probs))


class ProbedClickModel(ClickModel):
    """ClickModel that lets the backend record click probabilities at view time. The probe reads the
    model and draws nothing, so the clicks are the ones the plain model would have drawn."""

    def __init__(self, cfg: ClickModelConfig, world: ServingBackend):
        super().__init__(cfg)
        self.world = world

    def sample_clicks(self, items, profile, now, hist, rng):
        self.world.probe_view(self, items, profile, now, hist)
        return super().sample_clicks(items, profile, now, hist, rng)
