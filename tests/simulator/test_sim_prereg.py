import copy
import json

from sim.prereg import load_reports, main, run_checks

IDEAL = {
    # policy: (coverage, onboarding_share, jaccard, after, lift, adapted, rta, empty, fallback, ctr)
    "static_batch": (0.0, None, 1.0, 0.5, 0.0, 0.6, 9.0, 0.02, 0.02, 0.030),
    "static_batch_fallback": (1.0, 0.30, 0.99, 0.5, 0.0, 0.6, 9.0, 0.0, 0.01, 0.031),
    "reactive": (1.0, 0.80, 0.70, 0.8, 0.10, 0.9, 3.0, 0.0, 0.0, 0.050),
    "reactive_explore": (1.0, 0.70, 0.50, 0.6, 0.05, 0.9, 4.0, 0.0, 0.0, 0.045),
    "random": (1.0, 0.28, 0.05, 0.4, 0.0, 0.7, 6.0, 0.0, 0.0, 0.020),
}


def report(policy, preset, seed, catalog="synthetic", ctr_scale=1.0):
    cov, ob, jac, after, lift, ad, rta, emp, fb, ctr = IDEAL[policy]
    if preset == "category_only" and policy == "reactive":
        ctr = 0.035  # smaller advantage without the keyword feature (H8)
    return {
        "args": {"catalog": catalog, "preset": preset, "policy": policy, "seed": seed},
        "click_model": {"bias": -5.0},
        "metrics": {
            "cold_start": {"first_view_coverage": cov, "first_view_onboarding_category_share": ob},
            "reactivity": {"after_click_jaccard_mean": jac, "similar_share_after_click": after,
                           "similar_share_lift": lift},
            "drift": {"adapted_rate": ad, "requests_to_adapt_median": rta},
            "serving": {"empty_rate": emp, "fallback_rate": fb},
            "errors": {"error_rate": 0.0},
            "engagement": {"ctr_top_k": ctr * ctr_scale, "click_ack_rate": 1.0},
        },
    }


def grid():
    return [report(pol, pre, s) for pol in IDEAL for pre in ("default", "category_only") for s in (0, 1, 2)]


def by_id(checks, cid, preset="default"):
    return next(c for c in checks if c.id == cid and (c.preset == preset or cid == "H8"))


def test_policies_behaving_as_designed_pass_every_registered_check():
    checks = run_checks(grid())
    assert {c.id for c in checks} == {"P1", "P2", "P3", "P4", "P5", "H1", "H2", "H3", "H4", "H5", "H6", "H7",
                                      "H8", "X1", "X2"}
    assert [c.id for c in checks if c.passed is False] == []
    assert all(c.passed is None for c in checks if c.kind == "X")
    assert len([c for c in checks if c.id == "P1"]) == 2  # one per preset


def test_a_single_seed_breaks_a_p_check_but_only_the_mean_counts_for_h():
    reps = grid()
    bad = copy.deepcopy(reps)
    for r in bad:
        a = r["args"]
        if a["policy"] == "static_batch" and a["seed"] == 1 and a["preset"] == "default":
            r["metrics"]["cold_start"]["first_view_coverage"] = 0.1
        if a["policy"] == "random" and a["seed"] == 2 and a["preset"] == "default":
            r["metrics"]["reactivity"]["after_click_jaccard_mean"] = 0.6  # mean 0.2333 still < explore 0.5
    checks = run_checks(bad)
    assert by_id(checks, "P1").passed is False
    assert by_id(checks, "P1", "category_only").passed is True
    assert by_id(checks, "H2").passed is True


def test_missing_values_make_a_hypothesis_undetermined_not_passed():
    reps = grid()
    for r in reps:
        if r["args"]["policy"] == "reactive":
            r["metrics"]["drift"]["requests_to_adapt_median"] = None
    h5 = by_id(run_checks(reps), "H5")
    assert h5.passed is None


def test_h8_detects_when_keywords_do_not_widen_the_gap():
    reps = [r for r in grid()]
    for r in reps:
        a = r["args"]
        if a["policy"] == "reactive" and a["preset"] == "category_only":
            r["metrics"]["engagement"]["ctr_top_k"] = 0.06
    assert by_id(run_checks(reps), "H8").passed is False


def test_cli_writes_json_and_markdown_with_the_claim_scope_header(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    for r in grid():
        a = r["args"]
        (runs / f"{a['catalog']}__{a['policy']}__{a['preset']}__s{a['seed']}.json").write_text(json.dumps(r))
    (runs / "summary.json").write_text("{}")
    assert len(load_reports([runs])) == 30
    main(["--runs-dir", str(runs), "--out", str(tmp_path / "grid"), "--git-sha", "abc"])
    md = (tmp_path / "grid.md").read_text(encoding="utf-8")
    assert md.splitlines()[2].startswith("> [SIM]")
    payload = json.loads((tmp_path / "grid.json").read_text(encoding="utf-8"))
    assert payload["meta"]["runs"] == 30 and payload["meta"]["seeds"] == [0, 1, 2]
    assert sum(1 for c in payload["checks"] if c["passed"] is False) == 0
