"""Behavior metrics over a SimulationLog.

Every metric is a property of the *system's responses to synthetic behavior*
(did it answer, did it change, did it follow the drift). None of them is an
accuracy estimate for real users - see ADR 0019.
"""

from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence

import numpy as np

from sim.catalog import Item
from sim.driver import SimulationLog, ViewEvent
from sim.personas import ARCHETYPE_BY_NAME
from sim.text import jaccard

# X-Rec-Source values that mean "the personalized path did not answer": the fake
# app's "fallback" plus the request-time API's failure chain (popular -> recent ->
# empty). "batch" is ambiguous there (fallback in realtime mode, the normal answer
# in batch mode), so it is left to source_counts.
FALLBACK_SOURCES = frozenset({"fallback", "popular", "recent", "empty"})
SIMILAR_NOUN_JACCARD = 0.2


def jaccard_ids(a: Sequence[int], b: Sequence[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def is_similar(a: Item, b: Item) -> bool:
    if a.category_id is not None and a.category_id == b.category_id:
        return True
    return jaccard(a.noun_set, b.noun_set) >= SIMILAR_NOUN_JACCARD


def similar_share(ids: Sequence[int], clicked: Sequence[int], items: Dict[int, Item]) -> Optional[float]:
    """Share of `ids` (clicked ones excluded) similar to at least one clicked item."""
    anchors = [items[c] for c in clicked if c in items]
    rest = [items[i] for i in ids if i not in set(clicked) and i in items]
    if not anchors or not rest:
        return None
    return float(np.mean([any(is_similar(it, a) for a in anchors) for it in rest]))


def core_match_share(ids: Sequence[int], core: Iterable[int], items: Dict[int, Item]) -> Optional[float]:
    core = set(core)
    cats = [items[i].category_id for i in ids if i in items]
    if not cats:
        return None
    return float(np.mean([c in core for c in cats]))


def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else None


def _quantile(xs: List[float], q: float) -> Optional[float]:
    return float(np.quantile(xs, q)) if xs else None


def cold_start_metrics(log: SimulationLog, k: int) -> dict:
    by_user: Dict[int, List[ViewEvent]] = defaultdict(list)
    for v in log.views:
        by_user[v.user].append(v)
    late = [u for u in log.users if u.is_late_joiner]
    first_nonempty, onboarding_match, views_to_nonempty = [], [], []
    for u in late:
        vs = by_user.get(u.index, [])
        if not vs:
            continue
        first = vs[0]
        first_nonempty.append(bool(first.item_ids))
        ob = log.onboarding.get(u.index)
        if ob and first.item_ids:
            onboarding_match.append(core_match_share(first.item_ids[:k], ob.categories, log.items))
        n = next((i + 1 for i, v in enumerate(vs) if v.item_ids), None)
        if n is not None:
            views_to_nonempty.append(n)
    return {
        "n_late_joiners": len(late),
        "n_late_joiners_with_views": len(first_nonempty),
        "first_view_coverage": _mean([float(x) for x in first_nonempty]),
        "first_view_onboarding_category_share": _mean(onboarding_match),
        "views_until_nonempty_median": _quantile(views_to_nonempty, 0.5),
        "never_nonempty": len(first_nonempty) - len(views_to_nonempty),
    }


def reactivity_metrics(log: SimulationLog, k: int) -> dict:
    by_session: Dict[tuple, List[ViewEvent]] = defaultdict(list)
    for v in log.views:
        by_session[(v.user, v.day, v.session)].append(v)
    jac, identical, before, after = [], [], [], []
    for vs in by_session.values():
        vs.sort(key=lambda v: v.view_in_session)
        for prev, nxt in zip(vs, vs[1:]):
            if not prev.clicked_ids or not prev.ok or not nxt.ok:
                continue
            a, b = prev.item_ids[:k], nxt.item_ids[:k]
            jac.append(jaccard_ids(a, b))
            identical.append(float(a == b))
            before.append(similar_share(a, prev.clicked_ids, log.items))
            after.append(similar_share(b, prev.clicked_ids, log.items))
    b_mean, a_mean = _mean(before), _mean(after)
    return {
        "n_after_click_pairs": len(jac),
        "after_click_jaccard_mean": _mean(jac),
        "after_click_identical_rate": _mean(identical),
        "similar_share_before_click": b_mean,
        "similar_share_after_click": a_mean,
        "similar_share_lift": (a_mean - b_mean) if a_mean is not None and b_mean is not None else None,
    }


def drift_metrics(log: SimulationLog, k: int, threshold: float = 0.5) -> dict:
    by_user: Dict[int, List[ViewEvent]] = defaultdict(list)
    for v in log.views:
        by_user[v.user].append(v)
    requests_to_adapt, pre_share, censored, no_views = [], [], 0, 0
    for u in log.users:
        if not u.is_drifter:
            continue
        core = ARCHETYPE_BY_NAME[u.drift_profile.archetype].core_categories
        vs = [v for v in by_user.get(u.index, []) if v.ok]
        pre = [v for v in vs if v.day < u.drift_day]
        post = [v for v in vs if v.day >= u.drift_day]
        if pre:
            pre_share.append(core_match_share(pre[-1].item_ids[:k], core, log.items))
        if not post:
            no_views += 1
            continue
        n = next((i + 1 for i, v in enumerate(post)
                  if (core_match_share(v.item_ids[:k], core, log.items) or 0.0) >= threshold), None)
        if n is None:
            censored += 1
        else:
            requests_to_adapt.append(n)
    n_drift = sum(1 for u in log.users if u.is_drifter)
    measured = n_drift - no_views
    return {
        "n_drifters": n_drift,
        "n_drifters_with_post_drift_views": measured,
        "threshold": threshold,
        "pre_drift_new_core_share": _mean(pre_share),
        "adapted_rate": (len(requests_to_adapt) / measured) if measured else None,
        "requests_to_adapt_median": _quantile(requests_to_adapt, 0.5),
        "requests_to_adapt_p90": _quantile(requests_to_adapt, 0.9),
        "censored": censored,
    }


def serving_metrics(log: SimulationLog) -> dict:
    ok_views = [v for v in log.views if v.ok]
    sources = Counter(v.source for v in ok_views)
    has_header = any(s is not None for s in sources)
    return {
        "n_views": len(log.views),
        "empty_rate": _mean([float(not v.item_ids) for v in ok_views]),
        "source_counts": {str(k): n for k, n in sources.items()},
        "fallback_rate": (sum(n for s, n in sources.items() if s in FALLBACK_SOURCES) / len(ok_views))
        if has_header and ok_views else None,
    }


def error_metrics(log: SimulationLog) -> dict:
    by_ep: Dict[str, dict] = {}
    for c in log.calls:
        d = by_ep.setdefault(c.endpoint, {"calls": 0, "errors": 0, "statuses": Counter(), "latency_ms": []})
        d["calls"] += 1
        d["errors"] += int(not c.ok)
        d["statuses"][str(c.status)] += 1
        d["latency_ms"].append(c.latency_ms)
    out = {}
    for ep, d in sorted(by_ep.items()):
        lat = d.pop("latency_ms")
        out[ep] = {**d, "statuses": dict(d["statuses"]), "p50_ms": _quantile(lat, 0.5), "p95_ms": _quantile(lat, 0.95)}
    n = len(log.calls)
    return {
        "n_calls": n,
        "error_rate": (sum(1 for c in log.calls if not c.ok) / n) if n else None,
        "by_endpoint": out,
    }


def engagement_metrics(log: SimulationLog, k: int) -> dict:
    """Realized click rates - a calibration sanity check, not a quality score."""
    shown = clicks = 0
    by_arch: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    sessions: Dict[tuple, bool] = {}
    attempted = acked = 0
    for v in log.views:
        if not v.ok:
            continue
        n = min(k, len(v.item_ids))
        c = sum(1 for r in v.clicked_ranks if r < k)
        shown += n
        clicks += c
        by_arch[v.archetype][0] += n
        by_arch[v.archetype][1] += c
        key = (v.user, v.day, v.session)
        sessions[key] = sessions.get(key, False) or bool(v.clicked_ids)
        attempted += len(v.clicked_ids)
        acked += v.clicks_acked
    return {
        "ctr_top_k": (clicks / shown) if shown else None,
        "ctr_top_k_by_archetype": {a: (c / n if n else None) for a, (n, c) in sorted(by_arch.items())},
        "sessions_with_click_rate": _mean([float(x) for x in sessions.values()]),
        "clicks_attempted": attempted,
        "clicks_acked": acked,
        "click_ack_rate": (acked / attempted) if attempted else None,
    }


def compute_metrics(log: SimulationLog, k: int = 10) -> dict:
    return {
        "k": k,
        "model": log.model_name,
        "n_users": len(log.users),
        "cold_start": cold_start_metrics(log, k),
        "reactivity": reactivity_metrics(log, k),
        "drift": drift_metrics(log, k),
        "serving": serving_metrics(log),
        "errors": error_metrics(log),
        "engagement": engagement_metrics(log, k),
    }
