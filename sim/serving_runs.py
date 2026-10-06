"""One simulated week against the real serving path, reduced to the tables the analyses read.

[SIM] - system-response evidence only, no accuracy claims (ADR 0019). Setup: ADR 0025 A1.

    RunSpec  -> run_world() -> RunData  (arrays + a small meta dict; saved as <name>.npz)

A run is fully determined by its spec (seed, population, policy, exploration slots, fatigue mode),
so finished runs are cached on disk by name and the two experiments (E9 sim.ope_validation, E10
sim.fatigue_comparison) share the policy-A run instead of repeating it.
"""

import json
import os
import platform
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from sim.catalog import CATEGORY_CODES, Catalog, synthetic_catalog
from sim.click_model import ClickModel, preset
from sim.driver import ApiClient, SimulationConfig, SimulationLog, VirtualClock, run_simulation
from sim.metrics import after_click_pairs, compute_metrics
from sim.personas import PopulationConfig, SimUser, generate_population
from sim.reference import REFERENCE_START
from sim.serving_app import TARGET_POLICIES, ProbedClickModel, ServingBackend, serving_config

SLATE = 20
REACTIVITY_K = 10  # ADR 0019's k for after_click_jaccard and similar_share_lift
POLICY_CODES = {"none": 0, "deterministic": 1, "eps-uniform-v1": 2}
FATIGUE_HOURS = 48
FATIGUE_MIN_IMPRESSIONS = 3


@dataclass(frozen=True)
class RunSpec:
    """ranking=None is policy A (active heuristic + MMR); otherwise the named target policy is served.
    explore_slots=0 turns exploration off. with_targets computes the target slates next to each answer."""

    seed: int
    n_users: int = 300
    n_days: int = 7
    explore_slots: int = 2
    fatigue_mode: str = "log"
    ranking: Optional[str] = None
    with_targets: bool = False
    preset: str = "default"

    @property
    def name(self) -> str:
        policy = self.ranking or "A"
        targets = "_targets" if self.with_targets else ""
        return (f"{policy}_m{self.explore_slots}_{self.fatigue_mode}{targets}_{self.preset}"
                f"_u{self.n_users}_d{self.n_days}_s{self.seed}")


def policy_a(seed: int, n_users: int, n_days: int, explore_slots: int = 2, fatigue_mode: str = "log",
             with_targets: bool = True) -> RunSpec:
    return RunSpec(seed, n_users, n_days, explore_slots, fatigue_mode, None, with_targets)


def policy_b(name: str, seed: int, n_users: int, n_days: int) -> RunSpec:
    """A target policy run for real: its ranking, no exploration, everything else as policy A."""
    return RunSpec(seed, n_users, n_days, 0, "log", name, False)


@dataclass
class RunData:
    spec: dict
    meta: dict
    arrays: Dict[str, np.ndarray]

    def __getitem__(self, key: str) -> np.ndarray:
        return self.arrays[key]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, __spec__=json.dumps(self.spec), __meta__=json.dumps(self.meta), **self.arrays)

    @classmethod
    def load(cls, path: Path) -> "RunData":
        with np.load(path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files if not k.startswith("__")}
            return cls(json.loads(str(z["__spec__"])), json.loads(str(z["__meta__"])), arrays)


def _nan(value) -> float:
    return float("nan") if value is None else float(value)


def slate_ild(embeddings: np.ndarray) -> float:
    """Mean pairwise (1 - cosine) of a slate; rows are unit vectors."""
    n = len(embeddings)
    if n < 2:
        return float("nan")
    sims = embeddings @ embeddings.T
    return float(1.0 - (sims.sum() - np.trace(sims)) / (n * (n - 1)))


def category_entropy(categories: Sequence[Optional[int]]) -> float:
    """Shannon entropy (nats) of the slate's category distribution."""
    counts = np.asarray(list(Counter(categories).values()), dtype=np.float64)
    if counts.sum() <= 0:
        return float("nan")
    p = counts / counts.sum()
    return float(-(p * np.log(p)).sum())


@dataclass
class World:
    """A finished simulation: the backend with its logs and the driver's own record of what it did."""

    spec: RunSpec
    backend: ServingBackend
    log: SimulationLog
    users: List[SimUser]
    catalog: Catalog


def simulate(spec: RunSpec, probe: bool = True) -> World:
    """One simulated week of the ADR 0019 population against the real serving path (ADR 0025 A1.1).
    probe=False uses the plain click model (the probe must not change what users do; a test compares)."""
    from fastapi.testclient import TestClient

    from sim.fake_app import create_fake_app

    users = generate_population(PopulationConfig(n_users=spec.n_users, seed=spec.seed, n_days=spec.n_days))
    catalog = synthetic_catalog(n_days=spec.n_days, seed=spec.seed, start=REFERENCE_START)
    clock = VirtualClock(REFERENCE_START)
    cfg = serving_config(explore_slots=spec.explore_slots, fatigue_mode=spec.fatigue_mode)
    backend = ServingBackend(catalog, clock, seed=spec.seed, cfg=cfg, ranking=spec.ranking,
                             targets=TARGET_POLICIES if spec.with_targets else ())
    model = ProbedClickModel(preset(spec.preset), backend) if probe else ClickModel(preset(spec.preset))
    try:
        with TestClient(create_fake_app(backend)) as client:
            log = run_simulation(
                users,
                make_api=lambda u, calls: ApiClient(client, calls=calls, user_index=u.index),
                model=model,
                cfg=SimulationConfig(n_days=spec.n_days, start=REFERENCE_START, seed=spec.seed),
                clock=clock,
                on_day_end=lambda day: backend.day_end(),
            )
    finally:
        backend.close()
    return World(spec, backend, log, users, catalog)


def run_world(spec: RunSpec) -> RunData:
    started = time.perf_counter()
    world = simulate(spec)
    backend, log, users, catalog = world.backend, world.log, world.users, world.catalog

    counters = backend.counters.snapshot()
    # In this world nothing should fail or time out: such a fallback means the harness is broken,
    # and a broken harness must not produce a report.
    broken = {k: v for k, v in counters.items() if k in ("fallback.error", "fallback.timeout") and v}
    if broken:
        raise RuntimeError(f"{spec.name}: the serving path fell back on errors {broken}")
    if backend.probe_mismatches:
        raise RuntimeError(f"{spec.name}: {backend.probe_mismatches} views did not match the answer they came from")
    if len(backend.request_rows) != len(backend.response_order):
        raise RuntimeError(f"{spec.name}: {len(backend.response_order)} answers but "
                           f"{len(backend.request_rows)} request log rows")

    index_of_user = {backend.users[u.email].user_id: u.index for u in users if u.email in backend.users}
    by_index = {u.index: u for u in users}
    sources = sorted({r["source"] for r in backend.request_rows})
    names = TARGET_POLICIES if spec.with_targets else ()

    # ---- requests, in the order they were answered (= the order the request rows were written)
    req_index = {rid: i for i, rid in enumerate(backend.response_order)}
    rows = sorted(backend.request_rows, key=lambda r: req_index[r["request_id"]])
    n_req = len(rows)
    a: Dict[str, np.ndarray] = {
        "req_user": np.asarray([index_of_user[r["user_id"]] for r in rows], dtype=np.int32),
        "req_t": np.asarray([r["created_at"].timestamp() for r in rows], dtype=np.float64),
        "req_policy": np.asarray([POLICY_CODES[r["policy_version"]] for r in rows], dtype=np.int8),
        "req_source": np.asarray([sources.index(r["source"]) for r in rows], dtype=np.int8),
        "req_slate_size": np.asarray([r["slate_size"] for r in rows], dtype=np.int32),
        "req_shown": np.asarray([r["shown_count"] for r in rows], dtype=np.int32),
        "req_n_explore": np.asarray([len(r["explore_positions"] or ()) for r in rows], dtype=np.int32),
        "req_pool": np.asarray([_nan(r["explore_pool_size"]) for r in rows], dtype=np.float64),
        "req_eligible": np.asarray([_nan(r["eligible_count"]) for r in rows], dtype=np.float64),
        "req_candidates": np.asarray([_nan(r["candidate_count"]) for r in rows], dtype=np.float64),
        "req_fatigued": np.asarray([_nan(r["fatigued_count"]) for r in rows], dtype=np.float64),
        "req_cache_hit": np.asarray([bool(r["cache_hit"]) for r in rows], dtype=bool),
        "req_shadow_logged": np.asarray([bool(r["shadow_versions"]) for r in rows], dtype=bool),
    }
    records = [backend.responses[rid] for rid in backend.response_order]
    a["req_expected_shown"] = np.asarray(
        [float("nan") if rec.expected_shown is None else float(sum(rec.expected_shown)) for rec in records])
    a["req_ild"] = np.asarray(
        [slate_ild(np.stack([backend.embeddings[i] for i in rec.shown])) if rec.shown else float("nan")
         for rec in records])
    a["req_cat_entropy"] = np.asarray(
        [category_entropy([catalog.by_id[i].category_id for i in rec.shown]) if rec.shown else float("nan")
         for rec in records])
    for name in names:
        slates = np.full((n_req, SLATE), -1, dtype=np.int32)
        expected = np.full(n_req, np.nan)
        for i, rec in enumerate(records):
            slate = rec.targets.get(name)
            if slate is not None:
                slates[i, : len(slate)] = slate
                # every target slate must lie inside the eligible set the service logged for the request
                if not set(slate) <= set(rows[i]["candidate_ids"]):
                    raise RuntimeError(f"{spec.name}: target {name} left the eligible set of request {i}")
            if name in rec.expected_targets:
                expected[i] = rec.expected_targets[name]
        a[f"tgt_{name}"] = slates
        a[f"tgt_expected_{name}"] = expected

    # ---- slots
    s = backend.slots
    slot_req = np.asarray([req_index[rid] for rid in s.request_id], dtype=np.int32)
    slot_of = {(int(r), int(nid)): k for k, (r, nid) in enumerate(zip(slot_req, s.news_letter_id))}
    clicked = np.zeros(len(s), dtype=np.float64)
    unjoined = 0
    for c in backend.click_rows:
        k = slot_of.get((req_index.get(c.request_id, -1), c.news_letter_id))
        if k is None:
            unjoined += 1
        else:
            clicked[k] = 1.0  # a repeated click on the same slot counts once
    expected_slot = np.full(len(s), np.nan)
    for k, (r, pos) in enumerate(zip(slot_req, s.position)):
        probs = records[int(r)].expected_shown
        if probs is not None and pos < len(probs):
            expected_slot[k] = probs[pos]
    a.update({
        "slot_req": slot_req,
        "slot_user": np.asarray([index_of_user[u] for u in s.user_id], dtype=np.int32),
        "slot_t": np.asarray([t.timestamp() for t in s.created_at], dtype=np.float64),
        "slot_pos": np.asarray(s.position, dtype=np.int32),
        "slot_item": np.asarray(s.news_letter_id, dtype=np.int32),
        "slot_explored": np.asarray(s.explored, dtype=bool),
        "slot_prop": np.asarray([_nan(p) for p in s.propensity], dtype=np.float64),
        "slot_det_rank": np.asarray([-1 if r is None else r for r in s.det_rank], dtype=np.int32),
        "slot_click": clicked,
        "slot_expected": expected_slot,
        "slot_shadow_scored": np.asarray([v is not None for v in s.shadow_score], dtype=bool),
        "slot_n_features": np.asarray(s.n_features, dtype=np.int32),
    })

    # ---- users
    a["user_eta"] = np.asarray([by_index[i].profile.eta for i in range(len(users))], dtype=np.float64)
    a["user_late"] = np.asarray([by_index[i].is_late_joiner for i in range(len(users))], dtype=bool)
    a["user_drifter"] = np.asarray([by_index[i].is_drifter for i in range(len(users))], dtype=bool)

    # ---- after-click pairs (ADR 0019's reactivity definitions, per pair so they can be resampled by user)
    pairs = after_click_pairs(log, REACTIVITY_K)
    a["pair_user"] = np.asarray([p.user for p in pairs], dtype=np.int32)
    a["pair_jaccard"] = np.asarray([p.jaccard for p in pairs], dtype=np.float64)
    a["pair_before"] = np.asarray([_nan(p.similar_before) for p in pairs], dtype=np.float64)
    a["pair_after"] = np.asarray([_nan(p.similar_after) for p in pairs], dtype=np.float64)

    metrics = compute_metrics(log, k=REACTIVITY_K)
    meta = {
        "name": spec.name,
        "sources": sources,
        "counters": dict(sorted(counters.items())),
        "n_requests": n_req,
        "n_slots": len(s),
        "n_clicks_logged": len(backend.click_rows),
        "n_clicks_unjoined": unjoined,
        "n_views": len(log.views),
        "driver": {
            "error_rate": metrics["errors"]["error_rate"],
            "click_ack_rate": metrics["engagement"]["click_ack_rate"],
            "n_users_onboarded": metrics["onboarding"]["n_users_onboarded"],
            "n_onboarding_failures": metrics["onboarding"]["n_onboarding_failures"],
            "reactivity": metrics["reactivity"],
            "serving": metrics["serving"],
            "cold_start": metrics["cold_start"],
        },
        "catalog_items": len(catalog.items),
        "category_codes": list(CATEGORY_CODES),
        "wall_seconds": round(time.perf_counter() - started, 1),
    }
    return RunData(asdict(spec), meta, a)


def _run_cached(job) -> str:
    spec, runs_dir = job
    path = Path(runs_dir) / f"{spec.name}.npz"
    if not path.exists():
        run_world(spec).save(path)
    return str(path)


def run_all(specs: Sequence[RunSpec], runs_dir: Path, workers: int = 1) -> Dict[str, RunData]:
    """Runs what is not on disk yet (in `workers` processes) and loads everything by spec name."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    jobs = [(spec, str(runs_dir)) for spec in specs]
    if workers <= 1:
        paths = [_run_cached(job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            paths = list(ex.map(_run_cached, jobs))
    return {spec.name: RunData.load(Path(p)) for spec, p in zip(specs, paths)}


def experiment_meta(args, experiment: str, module: str, preregistration: str, registered: bool) -> dict:
    """What every report says about how it was produced. In GitHub Actions the run id and the commit
    come from the runner's environment; elsewhere the commit is whatever --git-sha says."""
    import kiwipiepy

    return {
        "experiment": experiment,
        "preregistration": preregistration,
        "preregistration_commit": args.prereg_commit,
        "registered_config": registered,
        "config": {"n_users": args.users, "n_days": args.days, "seeds": list(args.seeds), "preset": "default",
                   "catalog": "synthetic", "embeddings": "keyword/category hash, 64d (sim.sim_embeddings)"},
        "git_sha": os.environ.get("GITHUB_SHA") or args.git_sha,
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_repository": os.environ.get("GITHUB_REPOSITORY"),
        "github_ref": os.environ.get("GITHUB_REF_NAME"),
        "runner": {"python": sys.version.split()[0], "numpy": np.__version__, "kiwipiepy": kiwipiepy.__version__,
                   "platform": platform.platform()},
        "command": f"python -m {module} " + " ".join(sys.argv[1:]),
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run_summary(run: "RunData") -> dict:
    counters = run.meta["counters"]
    return {
        "wall_seconds": run.meta["wall_seconds"],
        "requests": run.meta["n_requests"],
        "clicks": run.meta["n_clicks_logged"],
        "clicks_unjoined": run.meta["n_clicks_unjoined"],
        "sources": {k[len("source."):]: v for k, v in counters.items() if k.startswith("source.")},
        "fallbacks": {k: v for k, v in counters.items() if k.startswith("fallback")},
        "driver_error_rate": run.meta["driver"]["error_rate"],
        "click_ack_rate": run.meta["driver"]["click_ack_rate"],
        "onboarding_failures": run.meta["driver"]["n_onboarding_failures"],
    }


# ------------------------------------------------------------------ shared reductions


def policy_requests(run: RunData, policy: str, full_slate_only: bool = True) -> np.ndarray:
    """Requests whose slate the named policy made and that went out as planned. E9 reads the 20-slot
    slates only (ADR 0025 A1.3); E10 reads every slate the policy made, whatever its size (A1.6)."""
    planned = (run["req_policy"] == POLICY_CODES[policy]) & (run["req_shown"] == run["req_slate_size"])
    return planned & (run["req_slate_size"] == SLATE) if full_slate_only else planned


def bootstrap_ratio(num: np.ndarray, den: np.ndarray, n_boot: int = 1000, seed: int = 0, alpha: float = 0.05):
    """Percentile CI of sum(num) / sum(den) resampling clusters (rows)."""
    if len(num) == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(num), size=(n_boot, len(num)))
    d = den[idx].sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        values = np.where(d > 0, num[idx].sum(axis=1) / d, np.nan)
    lo, hi = np.nanquantile(values, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi)


def per_user_sums(users: np.ndarray, values: np.ndarray, n_users: int) -> np.ndarray:
    return np.bincount(users, weights=values, minlength=n_users)[:n_users]


def repeat_flags(run: RunData, mask: Optional[np.ndarray] = None):
    """Per slot (time order): was this newsletter already in an earlier answer to this user, and had it
    been shown at least FATIGUE_MIN_IMPRESSIONS times in the FATIGUE_HOURS before this answer (what the
    fatigue rule blocks when enforced). Slots of the same answer never count as each other's repeat."""
    order = np.lexsort((run["slot_pos"], run["slot_req"]))
    seen: Dict[tuple, List[float]] = {}
    repeat = np.zeros(len(order), dtype=bool)
    rule = np.zeros(len(order), dtype=bool)
    window = timedelta(hours=FATIGUE_HOURS).total_seconds()
    users, items, times = run["slot_user"], run["slot_item"], run["slot_t"]
    pending: List[tuple] = []
    current = None
    for k in order:
        req = int(run["slot_req"][k])
        if req != current:
            for key, t in pending:
                seen.setdefault(key, []).append(t)
            pending, current = [], req
        key = (int(users[k]), int(items[k]))
        earlier = seen.get(key)
        if earlier:
            repeat[k] = True
            rule[k] = sum(1 for t in earlier if t >= times[k] - window) >= FATIGUE_MIN_IMPRESSIONS
        pending.append((key, float(times[k])))
    if mask is not None:
        repeat, rule = repeat & mask, rule & mask
    return repeat, rule
