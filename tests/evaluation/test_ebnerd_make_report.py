from evaluation.recsys.ebnerd.make_report import pareto_front


def test_pareto_front_keeps_only_non_dominated_points():
    pts = [(0.30, 0.50, 0.2), (0.29, 0.60, 0.2), (0.28, 0.55, 0.1), (0.30, 0.50, 0.2)]
    # 3번째는 2번째에 모든 축에서 지배된다. 1·4번째는 같은 점이라 서로 지배하지 않는다.
    assert pareto_front(pts) == [True, True, False, True]


def test_pareto_front_single_objective_tie_is_kept():
    assert pareto_front([(1.0,), (1.0,), (0.5,)]) == [True, True, False]


def _d(r1=(0.03, 0.01), r2_lo=0.05, r3_lo=0.01, iters=(40, 50, 60), chosen="ranker_v2_poolneg"):
    return {
        "p1_vs_baselines": {
            "ranker_v2-vs-popularity_24h": {"ndcg@10": {"diff": r1[0], "ci95": [r1[1], r1[0] + 0.01]}},
            "ranker_v2-vs-team_binary": {"ndcg@10": {"diff": 0.1, "ci95": [r2_lo, 0.2]}},
        },
        "p2_selection": {"chosen": chosen},
        "p2_vs": {f"{chosen}-vs-cosine_history": {"ndcg@10": {"diff": 0.05, "ci95": [r3_lo, 0.09]}}},
        "p1_models": {n: [{"best_iteration": i} for i in iters] for n in ("ranker_v2", chosen)},
    }


def test_promotion_passes_only_when_every_rule_passes():
    from evaluation.recsys.ebnerd.make_report import promotion_verdict
    v = promotion_verdict(_d())
    assert v["passed"] and all(r["pass"] for r in v["rules"].values())


def test_promotion_r1_needs_both_margin_and_ci():
    from evaluation.recsys.ebnerd.make_report import promotion_verdict
    # 차이는 +0.02 이상이지만 CI 하한이 0 이하 -> 실패, CI는 양수지만 차이가 +0.02 미만 -> 실패
    assert not promotion_verdict(_d(r1=(0.03, -0.001)))["rules"]["R1"]["pass"]
    assert not promotion_verdict(_d(r1=(0.015, 0.005)))["rules"]["R1"]["pass"]


def test_promotion_fails_on_any_degenerate_seed_or_missing_comparison():
    from evaluation.recsys.ebnerd.make_report import promotion_verdict
    v = promotion_verdict(_d(iters=(40, 5, 60)))
    assert not v["rules"]["R4"]["pass"] and not v["passed"]
    d = _d()
    del d["p2_vs"]
    v = promotion_verdict(d)
    assert not v["rules"]["R3"]["pass"] and not v["passed"]


def test_recommended_mmr_lambda_is_smallest_within_relative_loss():
    from evaluation.recsys.ebnerd.make_report import recommended_mmr_lambda
    sweep = {"0.5": 0.20, "0.6": 0.246, "0.7": 0.247, "0.8": 0.249, "0.9": 0.25, "1.0": 0.25}
    assert recommended_mmr_lambda(sweep, max_rel_loss=0.02) == "0.6"
    assert recommended_mmr_lambda({"0.9": 0.1, "1.0": 0.25}, max_rel_loss=0.02) == "1.0"


def test_promotion_r4_lists_models_in_fixed_order():
    from evaluation.recsys.ebnerd.make_report import promotion_verdict
    iters = promotion_verdict(_d())["rules"]["R4"]["best_iterations"]
    assert list(iters) == ["ranker_v2", "ranker_v2_poolneg"]
    # 선택 모델이 ranker_v2 자신이면 한 번만
    assert list(promotion_verdict(_d(chosen="ranker_v2"))["rules"]["R4"]["best_iterations"]) == ["ranker_v2"]


def test_render_skips_verdict_for_only_models_partial_run():
    from evaluation.recsys.ebnerd.make_report import _partial_run

    assert _partial_run({"meta": {"argv": ["--only-models", "ranker_v2_poolneg"]}})
    assert not _partial_run({"meta": {"argv": ["--dataset", "ebnerd_small"]}})
    assert not _partial_run({})


def test_click_time_tables_render_gap_rows_and_diffs():
    from evaluation.recsys.ebnerd.make_report import click_time_tables

    m = {"mean": 0.5, "ci95": [0.49, 0.51]}
    dd = {"diff": -0.001, "ci95": [-0.002, 0.0], "n": 10}
    sec = lambda cols: {"results": {f"popularity_6h|gap{g}": {c: m for c in cols} for g in (0, 300)},
                        "diffs_vs_gap0": {"popularity_6h: gap300 - gap0": {c: dd for c in cols}}}
    d = {"meta": {"gaps_seconds": [0, 300]},
         "read_time": {"n_impressions_with_click": 1000, "n_missing": 3,
                       "quantiles_seconds": {"p50": 12.0, "p90": 80.0}, "share_over_seconds": {"300": 0.04}},
         "p1": sec(("auc", "ndcg@10")), "p2": sec(("ndcg@10", "recall@10")),
         "reproduces_reference": {"reference": "ebnerd_v1.json", "p1": {"max_abs_diff_of_means": 0.0},
                                  "p2": {"max_abs_diff_of_means": 0.0}}}
    out = click_time_tables(d)
    assert "| 1,000 | 3 | 12 | 80 | 0.040 |" in out
    assert "| popularity_6h|gap300 | 0.5000 [0.4900, 0.5100] | 0.5000 [0.4900, 0.5100] |" in out
    assert "| popularity_6h: gap300 - gap0 | -0.0010 [-0.0020, +0.0000] | -0.0010 [-0.0020, +0.0000] |" in out
    assert "최대 절대 차이: P1 0.0e+00, P2 0.0e+00" in out
