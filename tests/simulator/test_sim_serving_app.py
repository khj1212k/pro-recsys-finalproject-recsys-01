"""The simulator's app with the real request-time recommender behind /newsletters/today (ADR 0025 A1).

What these tests pin down is the harness, not the recommender: that the answers really come out of
backend/app/recsys with exploration and logging v2, that the logs join, that the target policies are
ranking functions over the logged eligible set, and that the instruments added for the experiments
(target slates, click-model probe) do not change what the simulated users see or do.
"""

import inspect
from datetime import timedelta

import numpy as np
import pytest

pytest.importorskip("fastapi")  # not installed in the integration CI job

from sim.serving_app import (  # noqa: E402,I001  (first: it puts backend/ on sys.path for the app.* imports)
    SHADOW_VERSION,
    TARGET_POLICIES,
    PolicyRecommender,
    RankContext,
    SimRecsysRepository,
)

from app.recsys.deadline import Deadline  # noqa: E402
from app.recsys.exploration import POLICY_DETERMINISTIC, POLICY_EPS_UNIFORM, det_propensity  # noqa: E402
from app.recsys.pipeline import RealtimeRecommender  # noqa: E402
from app.recsys.repository import RecsysRepository  # noqa: E402
from app.recsys.types import ProfileState  # noqa: E402
from recsys_core.profile import rebuild, same_state  # noqa: E402
from recsys_core.serving import epoch_seconds  # noqa: E402

from sim.catalog import Item  # noqa: E402
from sim.reference import REFERENCE_START  # noqa: E402
from sim.serving_runs import (  # noqa: E402
    SLATE,
    policy_a,
    policy_b,
    policy_requests,
    repeat_flags,
    run_world,
    simulate,
)
from sim.sim_embeddings import DIM, item_embedding, token_vector  # noqa: E402

N_USERS, N_DAYS = 30, 3


@pytest.fixture(scope="module")
def world_a():
    return simulate(policy_a(0, N_USERS, N_DAYS))


@pytest.fixture(scope="module")
def run_a():
    return run_world(policy_a(0, N_USERS, N_DAYS))


# ------------------------------------------------------------------------------ embeddings


def test_stand_in_embeddings_are_deterministic_unit_vectors_built_from_keywords_and_category():
    a = item_embedding(("금리", "환율", "주식"), 200)
    assert a.shape == (DIM,) and a.dtype == np.float32
    assert np.linalg.norm(a) == pytest.approx(1.0, abs=1e-6)
    assert np.array_equal(a, item_embedding(("금리", "환율", "주식"), 200))  # nothing but the strings decides it
    assert np.array_equal(token_vector("kw:금리"), token_vector("kw:금리"))

    shares_two = item_embedding(("금리", "환율", "수출"), 200)
    same_category_only = item_embedding(("부동산", "아파트", "전세"), 200)
    unrelated = item_embedding(("야구", "축구", "감독"), 600)
    assert float(a @ shares_two) > float(a @ same_category_only) > float(a @ unrelated)


# ---------------------------------------------------------------------- policy A and its logs


def test_every_answer_comes_from_the_exploration_policy_and_is_logged_as_planned(world_a):
    b = world_a.backend
    assert len(b.request_rows) == len(b.response_order) == len({r["request_id"] for r in b.request_rows}) > 100
    assert b.counters.get("requests.logged") == len(b.request_rows)
    assert not [k for k in b.counters.snapshot() if k.startswith("fallback")]

    slots_of = {}
    for row in b.slots.rows():
        slots_of.setdefault(row["request_id"], []).append(row)
    for req in b.request_rows:
        assert req["policy_version"] == POLICY_EPS_UNIFORM and req["fatigue_mode"] == "log"
        assert req["slate_size"] == req["shown_count"] == SLATE
        m, pool = len(req["explore_positions"]), req["explore_pool_size"]
        assert m == 2  # every simulated user has onboarded, so nobody is on the no-signal (4-slot) path
        assert pool == req["eligible_count"] - (SLATE - m) == len(req["candidate_ids"]) - (SLATE - m)
        rows = sorted(slots_of[req["request_id"]], key=lambda r: r["position"])
        assert [r["position"] for r in rows] == list(range(SLATE))
        assert [r["position"] for r in rows if r["explored"]] == sorted(req["explore_positions"])
        assert {r["news_letter_id"] for r in rows} <= set(req["candidate_ids"])
        assert len({r["news_letter_id"] for r in rows}) == SLATE
        det_ranks = [r["det_rank"] for r in rows if not r["explored"]]
        assert det_ranks == list(range(SLATE - m))  # deterministic items keep their order around the explore slots
        for r in rows:
            expected = (m / SLATE) / pool if r["explored"] else det_propensity(r["det_rank"], r["position"], SLATE, m)
            assert r["propensity"] == pytest.approx(expected, rel=1e-12)


def test_exploration_draws_follow_the_registered_stream_and_differ_between_requests(world_a):
    b = world_a.backend
    drawn = [tuple(r["explore_positions"]) for r in b.request_rows]
    assert len(set(drawn)) > 20
    # ADR 0025 A1.1: default_rng([seed, 11, request counter]); positions first, sorted (exploration.draw_exploration)
    for k, req in enumerate(b.request_rows[:5]):
        rng = np.random.default_rng([0, 11, k])
        assert tuple(np.sort(rng.choice(SLATE, size=2, replace=False))) == tuple(req["explore_positions"])


def test_shadow_scores_and_heuristic_features_are_logged_for_every_slot(world_a):
    b = world_a.backend
    assert all(req["shadow_versions"] == [SHADOW_VERSION] for req in b.request_rows)
    assert all(req["model_version"] == "heuristic-v1" and req["feature_schema_version"] == 1 for req in b.request_rows)
    assert all(score is not None for score in b.slots.shadow_score)  # explored slots included
    assert set(b.slots.n_features) == {4}
    assert b.counters.get("shadow.scored") == b.counters.get("cache.miss") and b.counters.get("shadow.error") == 0


def test_each_click_joins_the_slot_it_was_made_on(world_a):
    b, log = world_a.backend, world_a.log
    sent = sorted((v.request_id, nid, rank) for v in log.views for nid, rank in zip(v.clicked_ids, v.clicked_ranks))
    stored = sorted((c.request_id, c.news_letter_id, c.position) for c in b.click_rows)
    assert stored == sent and len(sent) > 30
    slot_at = {(rid, pos): nid for rid, pos, nid in zip(b.slots.request_id, b.slots.position, b.slots.news_letter_id)}
    assert all(slot_at[(rid, pos)] == nid for rid, nid, pos in stored)


def test_reduced_tables_agree_with_the_logs(world_a, run_a):
    """run_world() reduces a world to arrays; the same seed gives the same world, so they must match."""
    b, log = world_a.backend, world_a.log
    assert run_a.meta["n_requests"] == len(b.request_rows) and len(run_a["slot_req"]) == len(b.slots)
    assert run_a.meta["n_clicks_unjoined"] == 0
    assert int(run_a["slot_click"].sum()) == len(b.click_rows) == sum(len(v.clicked_ids) for v in log.views)
    assert policy_requests(run_a, "eps-uniform-v1").all()
    assert np.array_equal(run_a["slot_item"], np.asarray(b.slots.news_letter_id))
    # clicked slots are the ones the driver clicked, at the rank it clicked
    clicked = {(int(r), int(p)) for r, p, c in zip(run_a["slot_req"], run_a["slot_pos"], run_a["slot_click"]) if c}
    order = {rid: i for i, rid in enumerate(b.response_order)}
    assert clicked == {(order[v.request_id], rank) for v in log.views for rank in v.clicked_ranks}
    assert run_a.meta["driver"]["error_rate"] == 0.0 and run_a.meta["driver"]["click_ack_rate"] == 1.0
    assert run_a.meta["driver"]["n_onboarding_failures"] == 0


def test_the_same_spec_reproduces_the_same_tables(run_a):
    again = run_world(policy_a(0, N_USERS, N_DAYS))
    assert set(again.arrays) == set(run_a.arrays)
    for key, values in run_a.arrays.items():
        assert np.array_equal(values, again[key], equal_nan=True), key


def test_policy_a_is_the_production_ranking_untouched(world_a):
    """With no ranking override the deterministic list is exactly what RealtimeRecommender.rank returns."""
    b = world_a.backend
    user_id = b.request_rows[-1]["user_id"]
    kept = b.last_rank[user_id]  # the world is shared with other tests: ranking again must not leave a trace
    try:
        plain = RealtimeRecommender.rank(b.recommender, b.repo, user_id, b.now(), Deadline(60.0))
        wrapped = b.recommender.rank(b.repo, user_id, b.now(), Deadline(60.0))
    finally:
        b.last_rank[user_id] = kept
    assert wrapped.ranked_ids == plain.ranked_ids and len(plain.ranked_ids) == SLATE
    assert np.array_equal(wrapped.eligible_ids, plain.eligible_ids)


# ------------------------------------------------------------------------- target policies


def test_target_slates_are_full_rankings_inside_the_logged_eligible_set(world_a):
    b = world_a.backend
    eligible = {r["request_id"]: set(r["candidate_ids"]) for r in b.request_rows}
    for rid in b.response_order:
        targets = b.responses[rid].targets
        assert set(targets) == set(TARGET_POLICIES)
        for slate in targets.values():
            assert len(slate) == len(set(slate)) == SLATE and set(slate) <= eligible[rid]
    # what is kept with an answer is the ranking as the policy made it, in its order
    last_answer = {b.responses[rid].user_id: rid for rid in b.response_order}
    for user_id, rid in last_answer.items():
        assert b.responses[rid].targets == b.last_rank[user_id][1]


def test_target_policies_are_the_registered_ranking_functions(world_a):
    b = world_a.backend
    user_id = b.request_rows[-1]["user_id"]
    det, _ = b.last_rank[user_id]
    ctx = RankContext(b.users_by_id[user_id], b.now(), det, seq=7)
    rec: PolicyRecommender = b.recommender
    eligible = det.eligible_ids.tolist()

    # random: 20 of E, uniformly, from default_rng([seed, 23, ranking counter])
    picks = np.random.default_rng([0, 23, 7]).choice(len(eligible), size=SLATE, replace=False)
    assert rec.slate("random", ctx) == [eligible[int(i)] for i in picks]

    # reactive: the fake app's toy content score over E, best first, no MMR
    score, _ = b.toy_scorer(ctx.user)
    by_score = sorted(eligible, key=lambda nid: (-score(b.catalog.by_id[nid]), nid))
    assert rec.slate("reactive", ctx) == by_score[:SLATE]

    # shadow_recency: the shadow scorer's scores through the same MMR as the active path
    scores = det.extra_scores[SHADOW_VERSION]
    embeddings = np.stack([b.embeddings[nid] for nid in eligible])
    mmr = rec.reranker.rerank_for_user(scores, embeddings, SLATE, len(ctx.user.categories))
    assert rec.slate("shadow_recency", ctx) == [eligible[int(i)] for i, _ in mmr]
    # ...which is not the active ranking and not the plain score order either
    assert rec.slate("shadow_recency", ctx) != det.ranked_ids


@pytest.mark.parametrize("name", TARGET_POLICIES)
def test_running_a_target_policy_serves_its_ranking_without_exploration(name):
    world = simulate(policy_b(name, 1, 20, 2))
    b = world.backend
    assert not [k for k in b.counters.snapshot() if k.startswith("fallback")]
    assert all(r["policy_version"] == POLICY_DETERMINISTIC and r["explore_positions"] == [] for r in b.request_rows)
    assert set(b.slots.propensity) == {1.0} and not any(b.slots.explored)
    # the last answer each user got is the ranking the policy made for it
    last_answer = {}
    for rid in b.response_order:
        last_answer[b.responses[rid].user_id] = b.responses[rid].shown
    for user_id, shown in last_answer.items():
        det, _ = b.last_rank[user_id]
        assert shown == det.ranked_ids[:SLATE] and set(shown) <= set(det.eligible_ids.tolist())
    slates = [tuple(b.responses[rid].shown) for rid in b.response_order]
    assert len(set(slates)) > len(slates) // 2


def test_a_failure_inside_the_serving_path_stops_the_run_instead_of_reporting_fallbacks(monkeypatch):
    """The service turns an exception into a popular-list fallback. In this world that can only be a bug
    of the harness, and a broken harness must not produce a report."""

    def boom(self, ctx):
        raise RuntimeError("bug in a target policy")

    monkeypatch.setattr(PolicyRecommender, "_slate_random", boom)
    with pytest.raises(RuntimeError, match="fell back on errors"):
        run_world(policy_b("random", 0, 5, 1))


# ----------------------------------------------------------------------------- click probe


def test_the_probe_reads_click_probabilities_without_changing_what_users_do():
    spec = policy_a(2, 20, 2)
    probed, plain = simulate(spec), simulate(spec, probe=False)

    def trace(world):
        return [(v.user, v.item_ids, v.clicked_ranks) for v in world.log.views]

    assert trace(probed) == trace(plain) and len(trace(probed)) > 50

    b = probed.backend
    assert b.probe_mismatches == 0
    for rid in b.response_order:
        rec = b.responses[rid]
        assert len(rec.expected_shown) == SLATE and all(0.0 < p < 1.0 for p in rec.expected_shown)
        assert set(rec.expected_targets) == set(TARGET_POLICIES)
    # the plain run recorded nothing
    assert all(plain.backend.responses[rid].expected_shown is None for rid in plain.backend.response_order)


def test_computing_target_slates_next_to_the_answers_does_not_change_the_answers():
    """E10's log arm is E9's policy-A run (targets computed) and its enforce arm has none: the two arms
    may differ in the fatigue mode only, so the target computation must leave no trace in what is served."""
    with_targets = simulate(policy_a(4, 20, 2, with_targets=True))
    without = simulate(policy_a(4, 20, 2, with_targets=False))

    def trace(world):
        return [(v.user, v.item_ids, v.clicked_ranks) for v in world.log.views]

    assert trace(with_targets) == trace(without) and len(trace(without)) > 50
    assert with_targets.backend.slots.propensity == without.backend.slots.propensity
    assert all(not without.backend.responses[rid].targets for rid in without.backend.response_order)


def test_probe_items_are_the_items_the_driver_builds_from_the_payload(world_a):
    b = world_a.backend
    nid = b.catalog.items[0].news_letter_id
    assert b.driver_item(nid) == Item.from_api(b.catalog.by_id[nid].to_api(include_press=False))
    assert b.driver_item(nid).press_names == ()  # the API does not expose press, so the click model sees none


# ------------------------------------------------------------------------------ repository


@pytest.fixture
def mid_week(world_a):
    """The world with its clock put back to the middle of the simulated period."""
    b = world_a.backend
    end = b.clock.now()
    b.clock.set(REFERENCE_START + timedelta(days=1, hours=12))
    yield b
    b.clock.set(end)


def test_repository_hides_newsletters_published_after_the_virtual_now_and_keeps_sql_orders(mid_week):
    b = mid_week
    repo, now = b.repo, b.now()
    visible = [it for it in b.catalog.items if it.created_at <= now]
    assert 0 < len(visible) < len(b.catalog.items)
    recent = repo.recent_ids(10)
    assert recent == [it.news_letter_id for it in sorted(visible, key=lambda it: it.created_at, reverse=True)[:10]]
    future = next(it for it in b.catalog.items if it.created_at > now)
    assert repo.items([future.news_letter_id]) == {} and repo.displayable_among([future.news_letter_id]) == set()

    since = now - (now - visible[len(visible) // 2].created_at)
    query = b.embeddings[visible[-1].news_letter_id]
    knn = repo.knn_ids(query, since, 5)
    window = [it for it in visible if it.created_at >= since]
    by_cos = sorted(window, key=lambda it: (-float(b.embeddings[it.news_letter_id] @ query), it.news_letter_id))
    assert knn == [it.news_letter_id for it in by_cos[:5]] and knn[0] == visible[-1].news_letter_id
    assert {m.news_letter_id for m in repo.window_meta(since)} == {it.news_letter_id for it in window}


def test_repository_implements_every_method_of_the_serving_contract():
    """The serving code calls the repository through app.recsys.repository.RecsysRepository. A method the
    protocol gains (or renames) must show up here as a failure, not as a fallback inside a run."""
    declared = {name: fn for name, fn in vars(RecsysRepository).items()
                if not name.startswith("_") and inspect.isfunction(fn)}
    assert {"profile_state", "recent_clicks", "item_window_counts", "fatigued_among", "rollback"} <= set(declared)
    for name, fn in declared.items():
        implemented = getattr(SimRecsysRepository, name, None)
        assert implemented is not None, f"SimRecsysRepository.{name} is missing"
        assert list(inspect.signature(implemented).parameters) == list(inspect.signature(fn).parameters), name


def test_long_term_profile_is_the_incremental_state_of_the_click_log(world_a):
    """ADR 0033: the click API updates the profile state with each click. What is stored must be what the
    rebuild job computes from the click log (recsys_core.profile.rebuild)."""
    b = world_a.backend
    user_id = max(b.clicks_by_user, key=lambda uid: len(b.clicks_by_user[uid]))
    clicks = b.clicks_by_user[user_id]
    assert len(clicks) >= 3
    state, categories = b.repo.profile_state(user_id)
    rebuilt = rebuild((epoch_seconds(at), b.embeddings[nid], b.catalog.by_id[nid].category_id)
                      for _, nid, at in clicks)
    assert same_state(state.hist, rebuilt) and state.hist.hist_len == len(clicks)
    assert state.last_event_at == max(at for _, _, at in clicks)
    assert categories == sorted(b.users_by_id[user_id].categories)
    # a user who never clicked has an empty state: the pipeline answers from the onboarding picks
    silent = next(uid for uid in b.users_by_id if uid not in b.clicks_by_user)
    assert b.repo.profile_state(silent)[0].hist.empty
    assert np.allclose(b.repo.onboarding_vector(silent),
                       np.mean([b.embeddings[i] for i in b.users_by_id[silent].onboarding_ids], axis=0))
    assert b.repo.profile_state(10**9) == (ProfileState(), [])


def test_recent_clicks_are_strictly_before_until_in_the_sql_order(world_a):
    b = world_a.backend
    user_id = max(b.clicks_by_user, key=lambda uid: len(b.clicks_by_user[uid]))
    clicks = b.clicks_by_user[user_id]
    first, last = clicks[0][2], clicks[-1][2]
    got = b.repo.recent_clicks(user_id, first, last, 1000)
    inside = [(log_id, nid, at) for log_id, nid, at in clicks if first <= at < last]
    assert 0 < len(inside) < len(clicks)  # the click made at `until` itself is not in
    assert [(c.news_letter_id, c.at) for c in got] == [
        (nid, at) for _, nid, at in sorted(inside, key=lambda c: (epoch_seconds(c[2]), c[1], c[0]), reverse=True)]
    assert all(np.array_equal(c.embedding, b.embeddings[c.news_letter_id]) for c in got)
    assert b.repo.recent_clicks(user_id, first, last, 2) == got[:2]
    assert b.repo.recent_clicks(user_id, last + timedelta(seconds=1), last + timedelta(days=1), 10) == []


def test_item_window_counts_count_clicks_and_impressions_of_all_users_inside_each_window(world_a):
    b = world_a.backend
    nid = max(b.clicks_by_item, key=lambda i: len(b.clicks_by_item[i]))
    clicked_at, shown_at = sorted(b.clicks_by_item[nid]), sorted(b.impressions_by_item[nid])
    assert len(clicked_at) >= 2 and len(shown_at) > len(clicked_at)
    end = clicked_at[-1]  # exclusive: the last click is outside every window
    starts = (clicked_at[-1] - timedelta(hours=6), clicked_at[0])
    unknown = 10**9
    counts = b.repo.item_window_counts([nid, unknown], starts, shown_at[1], end)
    assert set(counts) == {nid}  # an item without a row in any window is left out
    assert counts[nid].clicks == tuple(sum(1 for at in clicked_at if s <= at < end) for s in starts)
    assert counts[nid].clicks[1] == len(clicked_at) - sum(1 for at in clicked_at if at == end)
    assert counts[nid].inviews == sum(1 for at in shown_at if shown_at[1] <= at < end)
    assert b.repo.item_window_counts([nid], starts, shown_at[0], shown_at[0]) == {}


def test_events_of_one_virtual_instant_are_stamped_in_arrival_order(world_a):
    """The driver keeps the clock at the session start, and the serving path reads the clicks strictly
    before a request. Without distinct stamps the click that leads to the next view of a session would
    not be in that view's user state."""
    b, log = world_a.backend, world_a.log
    asked_at = {r["request_id"]: r["created_at"] for r in b.request_rows}
    followed = 0
    for before, after in zip(log.views, log.views[1:]):
        if (before.user, before.session, before.day) != (after.user, after.session, after.day):
            continue
        # same session: the driver's clock did not move, the stamps did
        made = [c.at for c in b.click_rows if c.request_id == before.request_id]
        assert made and asked_at[before.request_id] < min(made) and max(made) < asked_at[after.request_id]
        assert asked_at[after.request_id] - asked_at[before.request_id] < timedelta(milliseconds=1)
        user_id = b.responses[after.request_id].user_id
        seen = b.repo.recent_clicks(user_id, asked_at[after.request_id] - timedelta(hours=24),
                                    asked_at[after.request_id], 1000)
        assert {c.news_letter_id for c in seen} >= set(before.clicked_ids)
        followed += 1
    assert followed > 20
    # every event has its own stamp and the stamps follow the order of arrival
    stamps = sorted([*asked_at.values(), *(c.at for c in b.click_rows)])
    assert len(set(stamps)) == len(stamps)
    assert [r["created_at"] for r in b.request_rows] == sorted(asked_at.values())
    assert [c.at for c in b.click_rows] == sorted(c.at for c in b.click_rows)


def test_the_clock_is_the_driver_s_whenever_no_event_was_stamped_at_its_instant(world_a):
    b = world_a.backend
    kept = (b.clock.now(), b._stamp_base, b._stamp)
    try:
        t0 = REFERENCE_START + timedelta(days=30)
        b.clock.set(t0)
        assert b.now() == t0
        assert [b.stamp_event(), b.stamp_event(), b.now()] == [t0, t0 + timedelta(microseconds=1),
                                                               t0 + timedelta(microseconds=1)]
        b.clock.set(t0 + timedelta(microseconds=1))  # the next session starts inside the stamps already used
        assert b.now() == t0 + timedelta(microseconds=1) and b.stamp_event() == t0 + timedelta(microseconds=2)
        b.clock.set(t0 - timedelta(days=1))  # a clock put back (tests do it) is read as it is
        assert b.now() == t0 - timedelta(days=1) and b.stamp_event() == t0 - timedelta(days=1)
    finally:
        b.clock.set(kept[0])
        b._stamp_base, b._stamp = kept[1], kept[2]


# --------------------------------------------------------------------------- fatigue rule


def test_enforce_keeps_out_what_was_shown_three_times_in_48h_and_log_only_counts():
    log_run = run_world(policy_a(3, 20, 3, with_targets=False))
    enforce_run = run_world(policy_a(3, 20, 3, fatigue_mode="enforce", with_targets=False))
    for run, mode in ((log_run, "log"), (enforce_run, "enforce")):
        assert (run["req_fatigued"][policy_requests(run, "eps-uniform-v1", full_slate_only=False)] >= 0).all(), mode

    _, blocked_in_log = repeat_flags(log_run)
    assert blocked_in_log.mean() > 0.05  # the log arm does show such newsletters again - the test has teeth

    # enforce: no planned slate holds one, except when the slate came from the result cache (the rule is
    # evaluated when the deterministic list is computed; a cache hit reuses that list)
    _, blocked = repeat_flags(enforce_run)
    planned = policy_requests(enforce_run, "eps-uniform-v1", full_slate_only=False)
    fresh = planned & ~enforce_run["req_cache_hit"]
    assert not blocked[fresh[enforce_run["slot_req"]]].any()
    # and the eligible set it logs is the reduced one
    assert np.nanmean(enforce_run["req_eligible"][planned]) < np.nanmean(log_run["req_eligible"])
