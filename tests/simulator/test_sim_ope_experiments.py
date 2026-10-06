"""E9 (sim.ope_validation) and E10 (sim.fatigue_comparison): the reductions and the verdict rules.

The rules are pre-registered in ADR 0025 (사전 등록 2·4, 보완 A1). These tests fix the code to that
text: which estimator judges a target, how seeds are combined, what the single retry does, what
"repeat impression" and "not worse" mean. They run tiny worlds only - none of the numbers below is
a result.
"""

import hashlib
import json

import numpy as np
import pytest

pytest.importorskip("fastapi")  # not installed in the integration CI job

from evaluation.recsys.ope import (  # noqa: E402
    SlotLog,
    fit_position_bias_eta,
    position_based_slate,
    position_ctr,
    power_law_theta,
    replay_position_based,
)
from sim import fatigue_comparison as e10  # noqa: E402
from sim import ope_report  # noqa: E402
from sim import ope_validation as e9  # noqa: E402
from sim.serving_app import TARGET_POLICIES  # noqa: E402
from sim.serving_runs import (  # noqa: E402
    POLICY_CODES,
    SLATE,
    RunData,
    RunSpec,
    bootstrap_ratio,
    category_entropy,
    policy_a,
    policy_b,
    policy_requests,
    repeat_flags,
    run_world,
    simulate,
    slate_ild,
)

USERS, DAYS = 25, 2


@pytest.fixture(scope="module")
def tiny_runs(tmp_path_factory):
    """One seed of everything E9 and E10 read, at a size that runs in seconds."""
    runs = {"a": run_world(policy_a(0, USERS, DAYS)),
            "enforce": run_world(policy_a(0, USERS, DAYS, fatigue_mode="enforce", with_targets=False))}
    runs.update({name: run_world(policy_b(name, 0, USERS, DAYS)) for name in TARGET_POLICIES})
    return runs


# ------------------------------------------------------------------------------ run names


def test_run_names_separate_every_dimension_of_a_spec_and_the_arms_share_policy_a():
    a = policy_a(0, 300, 7)
    assert a.name == "A_m2_log_targets_default_u300_d7_s0"
    names = {a.name, policy_a(1, 300, 7).name, policy_a(0, 300, 7, explore_slots=4).name,
             policy_a(0, 300, 7, fatigue_mode="enforce", with_targets=False).name, policy_a(0, 20, 2).name,
             policy_b("random", 0, 300, 7).name, policy_b("reactive", 0, 300, 7).name}
    assert len(names) == 7
    # E10's log arm is E9's policy-A run: same spec, so the cached run is shared instead of repeated
    assert e10.policy_a(0, 300, 7, e10.EXPLORE_SLOTS, "log", with_targets=True) == a
    b = policy_b("shadow_recency", 2, 300, 7)
    assert (b.explore_slots, b.fatigue_mode, b.ranking, b.with_targets) == (0, "log", "shadow_recency", False)


def test_run_data_round_trips_through_disk(tiny_runs, tmp_path):
    run = tiny_runs["a"]
    run.save(tmp_path / "run.npz")
    loaded = RunData.load(tmp_path / "run.npz")
    assert loaded.spec == run.spec and loaded.meta == run.meta
    assert all(np.array_equal(v, loaded[k], equal_nan=True) for k, v in run.arrays.items())
    assert RunSpec(**loaded.spec) == policy_a(0, USERS, DAYS)


# ----------------------------------------------------------------------- E9: the reductions


def test_measured_value_is_clicks_per_slot_of_the_full_slates_the_policy_made(tiny_runs):
    run = tiny_runs["random"]
    m = e9.measured_value(run)
    assert m["requests"] == run.meta["n_requests"] and m["requests_excluded"] == 0
    assert m["slots"] == SLATE * m["requests"] and m["clicks"] == int(run["slot_click"].sum())
    assert m["ctr"] == pytest.approx(m["clicks"] / m["slots"])
    assert m["ci95"][0] <= m["ctr"] <= m["ci95"][1]

    # a fallback answer and a short slate are not the policy's 20-slot answers: both drop out
    arrays = dict(run.arrays)
    arrays["req_policy"] = run["req_policy"].copy()
    arrays["req_policy"][0] = POLICY_CODES["none"]
    arrays["req_slate_size"] = run["req_slate_size"].copy()
    arrays["req_slate_size"][1] = SLATE - 1
    cut = e9.measured_value(RunData(run.spec, run.meta, arrays))
    kept = ~np.isin(run["slot_req"], [0, 1])
    assert cut["requests"] == m["requests"] - 2 and cut["requests_excluded"] == 2
    assert cut["clicks"] == int(run["slot_click"][kept].sum()) and cut["slots"] == int(kept.sum())


def test_estimates_are_the_ope_estimators_applied_to_the_logged_slots(tiny_runs):
    """The stage output must be what evaluation/recsys/ope.py returns for a SlotLog rebuilt here from the
    raw world - the reduction (request numbering, click join, target slates) is what is under test."""
    world = simulate(policy_a(0, USERS, DAYS))
    b = world.backend
    order = {rid: i for i, rid in enumerate(b.response_order)}
    clicked = {(c.request_id, c.news_letter_id) for c in b.click_rows}
    log = SlotLog.from_columns(
        request=[order[rid] for rid in b.slots.request_id],
        position=b.slots.position,
        item=b.slots.news_letter_id,
        explored=b.slots.explored,
        propensity=b.slots.propensity,
        reward=[float((rid, nid) in clicked) for rid, nid in zip(b.slots.request_id, b.slots.news_letter_id)],
    )
    eta = fit_position_bias_eta(position_ctr(log, SLATE))
    theta = power_law_theta(SLATE, eta if np.isfinite(eta) else 0.0)

    measured = {name: {0: e9.measured_value(tiny_runs[name])} for name in TARGET_POLICIES}
    stage = e9.analyze_stage({0: tiny_runs["a"]}, measured, explore_slots=2)
    assert stage["explore_slots"] == 2 and set(stage["targets"]) == set(TARGET_POLICIES)
    a = stage["policy_a"]["0"]
    assert a["slots"] == len(log) and a["explore_slots"] == int(np.sum(b.slots.explored))
    assert a["clicks"] == len(b.click_rows) and a["requests_excluded"] == 0
    assert a["ctr"] == pytest.approx(log.reward.mean())

    for name in TARGET_POLICIES:
        target = {order[rid]: b.responses[rid].targets[name] for rid in b.response_order}
        row = stage["targets"][name]["per_seed"][0]
        for estimator, fn in ((e9.PRIMARY, replay_position_based), (e9.FALLBACK, position_based_slate)):
            expected = fn(log, target, theta)
            got = row["estimators"][estimator]
            assert got["n_slots"] == expected.n_slots and got["n_matched"] == expected.n_matched
            assert got["coverage"] == pytest.approx(expected.coverage, rel=1e-12)
            assert got["ess_ratio"] == pytest.approx(expected.ess_ratio, rel=1e-12)
            if np.isfinite(expected.value):
                assert got["value"] == pytest.approx(expected.value, rel=1e-12)
                assert got["rel_err"] == pytest.approx(abs(expected.value - measured[name][0]["ctr"])
                                                       / measured[name][0]["ctr"], rel=1e-9)
        # the primary estimator reads exploration slots only; the slate estimator reads every slot
        assert row["estimators"][e9.PRIMARY]["n_slots"] == a["explore_slots"]
        assert row["estimators"][e9.FALLBACK]["n_slots"] == a["slots"]
        # item-level slate matching supports every target inside E
        assert row["estimators"][e9.FALLBACK]["coverage"] == pytest.approx(1.0, abs=0.35)
        # same-context expectation = click-model probabilities of the target slates in policy A's contexts
        probed = [b.responses[rid].expected_targets[name] for rid in b.response_order]
        assert row["same_context_expected_ctr"] == pytest.approx(sum(probed) / (SLATE * len(probed)), rel=1e-12)
        in_pool = row["target_items_in_explore_pool_mean"]
        assert 0 <= in_pool <= SLATE and row["target_items_in_deterministic_slots_mean"] == pytest.approx(SLATE - in_pool)


def test_expected_click_rate_of_the_shown_slates_matches_the_probe(tiny_runs):
    run = tiny_runs["a"]
    per_request = np.bincount(run["slot_req"], weights=run["slot_expected"])
    assert np.allclose(per_request, run["req_expected_shown"])
    assert 0.0 < np.nanmean(run["slot_expected"]) < 0.2


# -------------------------------------------------------------------- E9: the verdict rule


def _seed_row(primary_cov, values):
    """values: estimator -> (rel_err, ess_ratio)."""
    def est(name):
        rel, ess = values.get(name, (0.5, 0.5))
        return {"rel_err": rel, "ess_ratio": ess, "coverage": primary_cov if name == e9.PRIMARY else 1.0,
                "rel_err_vs_same_context": rel}
    return {"estimators": {name: est(name) for name in e9.ESTIMATORS}, "state_shift_rel": 0.1}


def test_target_passes_on_the_primary_estimator_when_exploration_supports_it():
    rows = [_seed_row(0.95, {e9.PRIMARY: (0.10, 0.20)}), _seed_row(0.92, {e9.PRIMARY: (0.20, 0.06)}),
            _seed_row(0.91, {e9.PRIMARY: (0.14, 0.30)})]
    v = e9.judge_target(rows)
    assert v["verdict_estimator"] == e9.PRIMARY and not v["explore_support_short"]
    assert v["rel_err_mean"] == pytest.approx((0.10 + 0.20 + 0.14) / 3)  # a single seed above 15% is allowed
    assert v["ess_ratio_min"] == pytest.approx(0.06) and v["passed"]


def test_relative_error_is_judged_on_the_seed_mean_and_ess_on_every_seed():
    over = [_seed_row(0.95, {e9.PRIMARY: (e, 0.2)}) for e in (0.16, 0.15, 0.15)]
    v = e9.judge_target(over)
    assert not v["rel_err_ok"] and v["ess_ok"] and not v["passed"]

    thin = [_seed_row(0.95, {e9.PRIMARY: (0.01, ess)}) for ess in (0.30, 0.049, 0.30)]
    v = e9.judge_target(thin)
    assert v["rel_err_ok"] and not v["ess_ok"] and not v["passed"]  # the mean ESS ratio would have passed

    exactly = [_seed_row(0.95, {e9.PRIMARY: (0.15, 0.05)}) for _ in range(3)]
    assert e9.judge_target(exactly)["passed"]  # both thresholds are inclusive


def test_low_exploration_coverage_hands_the_verdict_to_the_slate_estimator_with_its_own_ess():
    # mean primary coverage 0.89 < 0.9 although two seeds are above it
    rows = [_seed_row(cov, {e9.PRIMARY: (0.01, 0.50), e9.FALLBACK: (0.10, 0.06)}) for cov in (0.95, 0.93, 0.79)]
    v = e9.judge_target(rows)
    assert v["primary_coverage_mean"] == pytest.approx(0.89) and v["explore_support_short"]
    assert v["verdict_estimator"] == e9.FALLBACK and v["passed"]

    # ...and the slate estimator is held to the same two thresholds on its own numbers
    rows = [_seed_row(0.5, {e9.PRIMARY: (0.01, 0.50), e9.FALLBACK: (0.10, 0.03)}) for _ in range(3)]
    v = e9.judge_target(rows)
    assert v["verdict_estimator"] == e9.FALLBACK and v["rel_err_ok"] and not v["ess_ok"] and not v["passed"]


def test_an_undefined_estimate_fails_the_target():
    rows = [_seed_row(0.95, {e9.PRIMARY: (None, 0.2)}), _seed_row(0.95, {e9.PRIMARY: (0.1, 0.2)})]
    v = e9.judge_target(rows)
    assert v["rel_err_mean"] is None and not v["passed"]


def _stage(passed_by_target, explore_slots):
    return {"explore_slots": explore_slots, "passed": all(passed_by_target.values()),
            "targets": {n: {"verdict": {"passed": p}} for n, p in passed_by_target.items()}}


def test_e9_needs_every_target_and_allows_exactly_one_retry_at_four_slots():
    ok = dict.fromkeys(TARGET_POLICIES, True)
    one_bad = {**ok, "random": False}

    assert e9.final_verdict(_stage(ok, 2), None) == {
        "e9": "pass", "passed_at_explore_slots": 2, "retry_run": False, "adr_status": "채택됨",
        "text": "탐색 2칸(운영 기본값)에서 세 타깃 모두 사전 등록 기준을 통과했다."}
    on_retry = e9.final_verdict(_stage(one_bad, 2), _stage(ok, 4))
    assert (on_retry["e9"], on_retry["passed_at_explore_slots"], on_retry["adr_status"]) == ("pass_on_retry", 4, "채택됨")
    assert "운영 기본값 2칸에서는 실패" in on_retry["text"]
    failed = e9.final_verdict(_stage(one_bad, 2), _stage(one_bad, 4))
    assert (failed["e9"], failed["adr_status"], failed["retry_run"]) == ("fail", "제안됨", True)
    with pytest.raises(RuntimeError, match="registered retry"):
        e9.final_verdict(_stage(one_bad, 2), None)
    assert (e9.FIRST_EXPLORE_SLOTS, e9.RETRY_EXPLORE_SLOTS) == (2, 4)
    assert (e9.REL_ERR_MAX, e9.ESS_RATIO_MIN, e9.COVERAGE_MIN, e9.N_BOOT, e9.BOOT_SEED) == (0.15, 0.05, 0.9, 1000, 0)
    assert e9.REGISTERED == e10.REGISTERED == {"n_users": 300, "n_days": 7, "seeds": [0, 1, 2]}


# --------------------------------------------------------------------- E10: the reductions


def _hand_run(requests):
    """requests: list of (user, hours, [items]) answered by the exploration policy."""
    slot_req, slot_user, slot_t, slot_item, slot_pos = [], [], [], [], []
    for r, (user, hours, items) in enumerate(requests):
        for pos, item in enumerate(items):
            slot_req.append(r), slot_user.append(user), slot_t.append(hours * 3600.0)
            slot_item.append(item), slot_pos.append(pos)
    arrays = {"slot_req": np.asarray(slot_req), "slot_user": np.asarray(slot_user), "slot_t": np.asarray(slot_t),
              "slot_item": np.asarray(slot_item), "slot_pos": np.asarray(slot_pos)}
    return RunData({}, {}, arrays)


def test_repeat_flags_count_earlier_answers_only_and_apply_the_48h_three_times_rule():
    run = _hand_run([
        (0, 0, [7, 8]),      # first time for both
        (0, 1, [7, 9]),      # 7 seen once before
        (0, 2, [7, 7]),      # 7 seen twice before; the second 7 of the same answer is not its own repeat
        (0, 3, [7]),         # 7 shown 4 times in the last 48h -> what the rule blocks
        (1, 4, [7]),         # another user: never seen
        (0, 60, [7, 8]),     # 60h later: still a repeat, but nothing of it lies inside the 48h window
    ])
    repeat, rule = repeat_flags(run)
    assert repeat.tolist() == [False, False, True, False, True, True, True, False, True, True]
    assert rule.tolist() == [False, False, False, False, False, False, True, False, False, False]
    assert (e10.FATIGUE_HOURS, e10.FATIGUE_MIN_IMPRESSIONS) == (48, 3)


def test_user_components_reproduce_the_driver_metrics_and_count_every_policy_slate(tiny_runs):
    for arm in ("a", "enforce"):
        run = tiny_runs[arm]
        totals = e10.user_components(run).sum(axis=0)
        value = {name: float(fn(totals)) for name, fn in e10.METRICS.items()}
        driver = run.meta["driver"]["reactivity"]
        # ADR 0019's definitions (sim.metrics.reactivity_metrics), now resampleable by user
        assert value["after_click_jaccard_mean"] == pytest.approx(driver["after_click_jaccard_mean"], rel=1e-12)
        assert value["similar_share_lift"] == pytest.approx(driver["similar_share_lift"], rel=1e-9)
        assert totals[e10.C["pairs"]] == driver["n_after_click_pairs"]

        planned = policy_requests(run, "eps-uniform-v1", full_slate_only=False)
        assert totals[e10.C["slots"]] == run["req_slate_size"][planned].sum()  # short slates included
        assert totals[e10.C["answers"]] == run.meta["n_requests"] and totals[e10.C["answer_slots"]] == run.meta["n_slots"]
        assert totals[e10.C["clicks"]] == run["slot_click"][planned[run["slot_req"]]].sum()
        repeat, rule = repeat_flags(run)
        assert value["repeat_impression_rate_all_answers"] == pytest.approx(repeat.mean())
        assert 0.0 < value["repeat_impression_rate"] < 1.0
    # the rule only bites in the enforce arm
    blocked = {arm: float(e10.METRICS["rule_blocked_impression_rate"](e10.user_components(tiny_runs[arm]).sum(axis=0)))
               for arm in ("a", "enforce")}
    assert blocked["a"] > 0.05 > blocked["enforce"]


def test_slate_diversity_measures():
    same = np.tile(np.eye(3)[0], (4, 1))
    assert slate_ild(same) == pytest.approx(0.0)
    assert slate_ild(np.eye(3)) == pytest.approx(1.0)  # orthogonal unit vectors
    assert np.isnan(slate_ild(np.eye(3)[:1]))
    assert category_entropy([100] * 5) == pytest.approx(0.0)
    assert category_entropy([100, 200, 300, 400]) == pytest.approx(np.log(4))


# ------------------------------------------------------------------- E10: the verdict rule


def _components(n, **per_cluster):
    out = np.zeros((n, len(e10.COMPONENTS)))
    for name, values in per_cluster.items():
        out[:, e10.C[name]] = values
    return out


def _arms(n=200, seed=0, repeat=(0.8, 0.6), jaccard=(0.5, 0.5), lift=(0.03, 0.03), noise=0.02):
    rng = np.random.default_rng(seed)
    arms = {}
    for arm, i in (("log", 0), ("enforce", 1)):
        before = 0.5 + rng.normal(0, noise, n)
        arms[arm] = _components(
            n, slots=100.0, repeats=100.0 * (repeat[i] + rng.normal(0, noise, n)),
            pairs=10.0, jaccard_sum=10.0 * (jaccard[i] + rng.normal(0, noise, n)),
            before_n=10.0, before_sum=10.0 * before, after_n=10.0, after_sum=10.0 * (before + lift[i]))
    return arms


def test_enforce_condition_needs_fewer_repeats_and_no_detected_loss_of_reactivity():
    met = e10.judge(e10.compare(_arms()))
    assert met["repeat_rate_reduced"] and not met["after_click_jaccard_worse"] and not met["similar_share_lift_worse"]
    assert met["enforce_condition_met"] and met["log_minus_enforce_repeat_rate_ci95"][0] > 0.15

    same_repeats = e10.judge(e10.compare(_arms(repeat=(0.8, 0.8))))
    assert not same_repeats["repeat_rate_reduced"] and not same_repeats["enforce_condition_met"]

    # the list changes less after a click under enforce: Jaccard up, CI above 0
    stickier = e10.judge(e10.compare(_arms(jaccard=(0.5, 0.6))))
    assert stickier["repeat_rate_reduced"] and stickier["after_click_jaccard_worse"]
    assert not stickier["enforce_condition_met"]
    # a lower Jaccard is not "worse" (and is not counted as an improvement either)
    assert e10.judge(e10.compare(_arms(jaccard=(0.5, 0.4))))["enforce_condition_met"]

    less_lift = e10.judge(e10.compare(_arms(lift=(0.03, -0.03))))
    assert less_lift["similar_share_lift_worse"] and not less_lift["enforce_condition_met"]


def test_the_bootstrap_is_paired_over_the_same_clusters_in_both_arms():
    arms = _arms(repeat=(0.8, 0.8), noise=0.2)
    arms["enforce"] = arms["log"].copy()  # identical arms: every resample must give exactly no difference
    metrics = e10.compare(arms)
    for name in e10.JUDGED:
        assert metrics[name]["enforce_minus_log"] == 0.0 and metrics[name]["enforce_minus_log_ci95"] == [0.0, 0.0]
    assert metrics["repeat_impression_rate"]["log_ci95"][1] - metrics["repeat_impression_rate"]["log_ci95"][0] > 0.02
    verdict = e10.judge(metrics)
    assert not verdict["repeat_rate_reduced"] and not verdict["enforce_condition_met"]


def test_a_metric_without_data_leaves_the_verdict_undetermined_not_met():
    arms = _arms()
    for arm in arms:
        arms[arm][:, e10.C["pairs"]] = 0.0  # no after-click pair at all
    verdict = e10.judge(e10.compare(arms))
    assert verdict["undetermined"] and not verdict["enforce_condition_met"]


def test_bootstrap_ratio_resamples_clusters():
    num, den = np.asarray([1.0, 0.0, 3.0, 0.0]), np.asarray([10.0, 10.0, 10.0, 10.0])
    lo, hi = bootstrap_ratio(num, den, n_boot=500, seed=0)
    assert 0.0 <= lo < 0.1 < hi <= 0.3
    assert bootstrap_ratio(num, den, 500, 0) == (lo, hi)  # seeded


# ----------------------------------------------------------------- end to end, at smoke size


def test_both_experiments_run_end_to_end_and_report_from_their_json_alone(tmp_path, capsys):
    common = ["--runs-dir", str(tmp_path / "runs"), "--users", "12", "--days", "1", "--seeds", "0", "1",
              "--git-sha", "testsha", "--prereg-commit", "preregsha"]
    assert e9.main([*common, "--out", str(tmp_path / "ope_validation.json")]) == 0
    n_runs_after_e9 = len(list((tmp_path / "runs").glob("*.npz")))
    assert e10.main([*common, "--out", str(tmp_path / "fatigue_v1.json")]) == 0
    # E10 added only its enforce arm: the log arm is E9's policy-A run
    assert len(list((tmp_path / "runs").glob("*.npz"))) == n_runs_after_e9 + 2

    ope = json.loads((tmp_path / "ope_validation.json").read_text(encoding="utf-8"))
    assert ope["meta"]["registered_config"] is False and ope["meta"]["git_sha"] == "testsha"
    assert ope["meta"]["preregistration_commit"] == "preregsha"
    assert ope["verdict"]["e9"] in {"pass", "pass_on_retry", "fail"}
    # the retry ran exactly when the first stage failed, on the same measured values
    assert (ope["explore_slots_4_retry"] is not None) == (not ope["explore_slots_2"]["passed"])
    if ope["explore_slots_4_retry"]:
        first, retry = ope["explore_slots_2"], ope["explore_slots_4_retry"]
        assert retry["explore_slots"] == 4 and n_runs_after_e9 == 2 * (1 + len(TARGET_POLICIES)) + 2
        for name in TARGET_POLICIES:
            assert [r["measured"] for r in retry["targets"][name]["per_seed"]] == \
                   [r["measured"] for r in first["targets"][name]["per_seed"]]
        assert all(a["requests_by_explore_slots"] == {"4": a["requests_analyzed"]} for a in retry["policy_a"].values())
    assert all(a["requests_by_explore_slots"] == {"2": a["requests_analyzed"]}
               for a in ope["explore_slots_2"]["policy_a"].values())
    json.dumps(ope, allow_nan=False)  # no NaN leaks into the report

    fatigue = json.loads((tmp_path / "fatigue_v1.json").read_text(encoding="utf-8"))
    assert fatigue["clusters"] == 24 and fatigue["judged_metrics"] == list(e10.JUDGED)
    assert set(fatigue["arms"]) == {"log", "enforce"} and isinstance(fatigue["verdict"]["enforce_condition_met"], bool)
    json.dumps(fatigue, allow_nan=False)

    assert ope_report.main(["--ope", str(tmp_path / "ope_validation.json"), "--fatigue",
                            str(tmp_path / "fatigue_v1.json"), "--out-dir", str(tmp_path / "reports")]) == 0
    for name in ("ope_validation", "fatigue_v1"):
        source, copy = tmp_path / f"{name}.json", tmp_path / "reports" / f"{name}.json"
        assert hashlib.sha256(source.read_bytes()).digest() == hashlib.sha256(copy.read_bytes()).digest()
        md = (tmp_path / "reports" / f"{name}.md").read_text(encoding="utf-8")
        assert "[SIM]" in md and "사전 등록한 구성이 아니다(스모크)" in md and "`testsha`" in md and "`preregsha`" in md
    ope_md = (tmp_path / "reports" / "ope_validation.md").read_text(encoding="utf-8")
    assert ope["verdict"]["text"] in ope_md and all(name in ope_md for name in TARGET_POLICIES)
    assert fatigue["verdict"]["text"] in (tmp_path / "reports" / "fatigue_v1.md").read_text(encoding="utf-8")
    assert "E9 [SIM] verdict" in capsys.readouterr().out


def test_registered_reports_carry_no_smoke_banner():
    meta = {"label": "[SIM]", "registered_config": True, "preregistration": "p", "preregistration_commit": "c",
            "git_sha": "s", "github_run_id": "123", "github_run_attempt": "1", "github_repository": "o/r",
            "github_ref": "b", "started_utc": "t0", "finished_utc": "t1", "command": "cmd",
            "config": {"catalog": "synthetic", "preset": "default", "embeddings": "e", "n_users": 300, "n_days": 7,
                       "seeds": [0, 1, 2]},
            "runner": {"python": "3", "numpy": "2", "kiwipiepy": "0", "platform": "linux"}}
    lines = "\n".join(ope_report.header(meta, "title", "u"))
    assert "스모크" not in lines and "GitHub Actions 실행 `123`(시도 1)" in lines and "`o/r`" in lines
    assert "스모크" in "\n".join(ope_report.header({**meta, "registered_config": False}, "title", "u"))
