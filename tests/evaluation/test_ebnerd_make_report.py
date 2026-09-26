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
