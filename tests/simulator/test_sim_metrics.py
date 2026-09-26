from datetime import datetime, timedelta, timezone

import pytest

from sim.catalog import Item
from sim.driver import CallRecord, OnboardingRecord, SimulationConfig, SimulationLog, ViewEvent
from sim.metrics import (
    compute_metrics,
    core_match_share,
    drift_metrics,
    jaccard_ids,
    reactivity_metrics,
    similar_share,
)
from sim.personas import ARCHETYPE_BY_NAME, PopulationConfig, generate_population

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def item(nid, cat, kws=()):
    return Item(nid, "", "", tuple(kws), T0, 1, cat)


ITEMS = {
    1: item(1, 200, ("금리",)), 2: item(2, 200, ("환율",)), 3: item(3, 600, ("야구",)),
    4: item(4, 600, ("축구",)), 5: item(5, 700, ("금리", "미국")), 6: item(6, 500, ("영화",)),
}


def view(user, day, session, v, ids, clicked=(), source=None, ok=True):
    return ViewEvent(user, "x", day, T0 + timedelta(days=day, minutes=session * 60 + v), session, v, 0,
                     list(ids), source, ok, clicked_ids=list(clicked),
                     clicked_ranks=[list(ids).index(c) for c in clicked], clicks_acked=len(clicked))


def make_log(users, views, calls=(), onboarding=None):
    return SimulationLog(users, views, list(calls), onboarding or {}, ITEMS, SimulationConfig(), "default")


def test_jaccard_and_similarity_helpers():
    assert jaccard_ids([1, 2, 3], [2, 3, 4]) == pytest.approx(2 / 4)
    assert jaccard_ids([], []) == 1.0
    # item 5 (world) shares the noun 금리 with item 1 -> similar despite another category
    assert similar_share([1, 2, 3, 5], clicked=[1], items=ITEMS) == pytest.approx(2 / 3)
    assert similar_share([1], clicked=[1], items=ITEMS) is None
    assert core_match_share([3, 4, 6, 1], {600, 500}, ITEMS) == pytest.approx(3 / 4)


def test_reactivity_pairs_only_views_that_followed_a_click():
    views = [
        view(0, 0, 0, 0, [1, 3, 6], clicked=[1]),
        view(0, 0, 0, 1, [2, 5, 3]),          # after click: 1 of 3 ids shared -> J = 1/5
        view(0, 0, 1, 0, [1, 3, 6]),          # new session, no preceding click
        view(0, 0, 1, 1, [1, 3, 6]),          # follows a no-click view -> ignored
    ]
    m = reactivity_metrics(make_log([], views), k=10)
    assert m["n_after_click_pairs"] == 1
    assert m["after_click_jaccard_mean"] == pytest.approx(1 / 5)
    assert m["similar_share_before_click"] == pytest.approx(0.0)   # 3, 6 unrelated to 1
    assert m["similar_share_after_click"] == pytest.approx(2 / 3)  # 2 (same cat), 5 (금리)
    assert m["similar_share_lift"] == pytest.approx(2 / 3)


def test_drift_counts_requests_until_half_of_top_k_matches_new_core():
    users = generate_population(PopulationConfig(n_users=40, seed=0, drift_frac=0.1, late_join_frac=0.0))
    drifter = next(u for u in users if u.is_drifter)
    core = sorted(ARCHETYPE_BY_NAME[drifter.drift_profile.archetype].core_categories)
    match = [i for i, it in ITEMS.items() if it.category_id in core]
    other = [i for i, it in ITEMS.items() if it.category_id not in core]
    d = drifter.drift_day
    views = [
        view(drifter.index, d - 1, 0, 0, other[:4]),
        view(drifter.index, d, 0, 0, other[:4]),
        view(drifter.index, d, 1, 0, other[:2] + match[:1]),
        view(drifter.index, d + 1, 0, 0, match[:2] + other[:1]),
    ]
    m = drift_metrics(make_log([drifter], views), k=10)
    assert m["n_drifters"] == 1
    assert m["pre_drift_new_core_share"] == 0.0
    assert m["requests_to_adapt_median"] == 3
    assert m["adapted_rate"] == 1.0 and m["censored"] == 0


def test_cold_start_serving_errors_and_engagement_summaries():
    users = generate_population(PopulationConfig(n_users=20, seed=0, late_join_frac=0.1, drift_frac=0.0))
    late = [u for u in users if u.is_late_joiner]
    assert len(late) == 2
    a, b = late
    views = [
        view(a.index, a.join_day, 0, 0, [], source="empty"),
        view(a.index, a.join_day + 1, 0, 0, [1, 2], clicked=[1], source="batch"),
        view(b.index, b.join_day, 0, 0, [3, 4, 6], source="fallback"),
        view(b.index, b.join_day, 1, 0, [], ok=False),
    ]
    onboarding = {b.index: OnboardingRecord(b.index, (600,), (), T0)}
    calls = [CallRecord("today", "GET", 200, 5.0, True), CallRecord("today", "GET", 500, 9.0, False),
             CallRecord("click", "POST", 200, 3.0, True)]
    m = compute_metrics(make_log(users, views, calls, onboarding), k=10)

    assert m["cold_start"]["first_view_coverage"] == 0.5
    assert m["cold_start"]["first_view_onboarding_category_share"] == pytest.approx(2 / 3)
    assert m["cold_start"]["views_until_nonempty_median"] == 1.5
    assert m["serving"]["empty_rate"] == pytest.approx(1 / 3)
    assert m["serving"]["fallback_rate"] == pytest.approx(2 / 3)  # "empty" + "fallback"
    assert m["errors"]["error_rate"] == pytest.approx(1 / 3)
    assert m["errors"]["by_endpoint"]["today"]["errors"] == 1
    assert m["engagement"]["ctr_top_k"] == pytest.approx(1 / 5)
    assert m["engagement"]["click_ack_rate"] == 1.0


def test_fallback_rate_is_unknown_without_a_source_header():
    views = [view(0, 0, 0, 0, [1, 2]), view(1, 0, 0, 0, [])]
    m = compute_metrics(make_log([], views))
    assert m["serving"]["fallback_rate"] is None
    assert m["serving"]["empty_rate"] == 0.5


def test_fallback_rate_counts_the_request_time_api_fallback_chain():
    # X-Rec-Source values of the request-time /today (feat/realtime-recommendation): realtime and
    # cold-start paths are answers by design; batch/popular/recent/empty come from
    # the failure chain, but batch is also the normal answer in RECSYS_MODE=batch,
    # so it is reported in source_counts and not counted as fallback.
    sources = ["realtime", "cold_start_onboarding", "batch", "popular", "recent", "empty", "realtime", "realtime"]
    views = [view(i, 0, 0, 0, [1], source=s) for i, s in enumerate(sources)]
    m = compute_metrics(make_log([], views))
    assert m["serving"]["fallback_rate"] == pytest.approx(3 / 8)
    assert m["serving"]["source_counts"]["realtime"] == 3
