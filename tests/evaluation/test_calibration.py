import math
import random

import numpy as np
import pytest
from sklearn.metrics import cohen_kappa_score

from evaluation.llm import calibration as cal


class TestCohenKappa:
    def test_hand_computed_binary_example(self):
        # 20건: 둘 다 Y 7, 둘 다 N 8, (Y,N) 3, (N,Y) 2
        # p_o = 15/20 = 0.75, p_e = (10/20)(9/20) + (10/20)(11/20) = 0.5
        a = ["Y"] * 7 + ["N"] * 8 + ["Y"] * 3 + ["N"] * 2
        b = ["Y"] * 7 + ["N"] * 8 + ["N"] * 3 + ["Y"] * 2
        assert cal.cohen_kappa(a, b) == pytest.approx(0.5)

    @pytest.mark.parametrize("weights", [None, "linear", "quadratic"])
    def test_matches_sklearn_on_random_ordinal_labels(self, weights):
        rng = random.Random(7)
        a = [rng.randint(1, 5) for _ in range(200)]
        b = [min(5, max(1, x + rng.choice([-1, 0, 0, 1]))) for x in a]
        assert cal.cohen_kappa(a, b, weights=weights) == pytest.approx(
            cohen_kappa_score(a, b, weights=weights)
        )

    def test_perfect_agreement_on_a_single_category_is_undefined_not_one(self):
        assert math.isnan(cal.cohen_kappa([True] * 5, [True] * 5))

    def test_length_mismatch_and_empty_input_raise(self):
        with pytest.raises(ValueError):
            cal.cohen_kappa([1, 0], [1])
        with pytest.raises(ValueError):
            cal.cohen_kappa([], [])


def test_spearman_is_rank_based():
    x = [1, 2, 3, 4, 5]
    assert cal.spearman(x, [v ** 3 for v in x]) == pytest.approx(1.0)
    assert cal.spearman(x, [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    assert math.isnan(cal.spearman(x, [3, 3, 3, 3, 3]))


def test_group_folds_keep_each_cluster_in_one_fold_and_are_seeded():
    groups = [f"c{i // 3}" for i in range(30)]  # 클러스터당 3행(후보 3개)
    f1 = cal.group_folds(groups, k=2, seed=1)
    f2 = cal.group_folds(groups, k=2, seed=1)
    assert f1 == f2
    for g in set(groups):
        assert len({f1[i] for i, gg in enumerate(groups) if gg == g}) == 1
    assert sorted(set(f1)) == [0, 1]
    assert abs(f1.count(0) - f1.count(1)) <= 3


def _v2_record(min_score, claims):
    return {"criteria": {"faithfulness": min_score, "coverage": 5, "coherence": 5, "style": 5},
            "unsupported_claims": [{"claim": "x"}] * claims}


class TestTwoFoldThreshold:
    def test_recovers_the_rule_that_generated_the_labels_and_reports_oof_kappa(self):
        rng = random.Random(3)
        records, human, groups = [], [], []
        for i in range(80):
            s = rng.randint(1, 5)
            c = rng.choice([0, 0, 1, 2])
            records.append(_v2_record(s, c))
            human.append(s >= 4 and c == 0)
            groups.append(f"c{i}")

        out = cal.two_fold_select(records, human, groups, cal.v2_rule_grid(), seed=20260925)

        assert out["oof_kappa"] == pytest.approx(1.0)
        assert out["full_fit"]["params"] == {"min_criterion_score": 4, "max_unsupported_claims": 0}
        assert len(out["folds"]) == 2
        assert all(f["params"] == out["full_fit"]["params"] for f in out["folds"])

    def test_oof_kappa_is_near_zero_when_judge_is_noise(self):
        rng = random.Random(11)
        records = [_v2_record(rng.randint(1, 5), rng.choice([0, 1])) for _ in range(400)]
        human = [rng.random() < 0.5 for _ in range(400)]
        groups = [f"c{i}" for i in range(400)]

        out = cal.two_fold_select(records, human, groups, cal.v2_rule_grid(), seed=1)

        # 학습 폴드에서는 우연히 맞는 임계값을 고를 수 있지만 OOF에서는 0 근처여야 한다
        assert abs(out["oof_kappa"]) < 0.15
        assert max(f["train_kappa"] for f in out["folds"]) > out["oof_kappa"]

    def test_failed_judge_call_counts_as_fail(self):
        rule = cal.v2_rule_grid()[0]
        assert rule.predict({"criteria": None, "unsupported_claims": []}) is False

    def test_v1_grid_thresholds_the_0_to_10_score(self):
        rules = {r.params["min_score"]: r for r in cal.v1_rule_grid()}
        assert rules[5].predict({"score": 5}) is True
        assert rules[5].predict({"score": 4}) is False


class TestSelfPreference:
    def _rows(self, bias):
        """judge 'g'(gemini 계열)가 gemini 출력에만 +bias만큼 후하다. 기준 judge 'r'은 공정."""
        rng = np.random.default_rng(0)
        rows = []
        for c in range(60):
            for fam in ("gemini", "openai", "upstage"):
                human = int(rng.random() < 0.5)
                for judge, jfam in (("g", "gemini"), ("r", "anthropic")):
                    score = 1 + 4 * human + rng.normal(0, 0.3)
                    if judge == "g" and fam == "gemini":
                        score += bias
                    rows.append({"cluster": f"c{c}", "generator_family": fam, "judge": judge,
                                 "judge_family": jfam, "judge_score": score, "human_score": human})
        return rows

    def test_did_detects_injected_same_family_bias(self):
        out = cal.self_preference_did(self._rows(bias=1.5), reference_judge="r", n_boot=300, seed=0)
        est = out["g"]
        assert est["family"] == "gemini"
        assert est["did"] > 0.3
        assert est["ci"][0] > 0

    def test_did_ci_covers_zero_without_bias(self):
        out = cal.self_preference_did(self._rows(bias=0.0), reference_judge="r", n_boot=300, seed=0)
        lo, hi = out["g"]["ci"]
        assert lo < 0 < hi

    def test_judges_whose_family_generated_nothing_are_skipped(self):
        rows = [r for r in self._rows(0.0) if r["generator_family"] != "gemini"]
        assert "g" not in cal.self_preference_did(rows, reference_judge="r", n_boot=50, seed=0)


class TestClusterConfidence:
    def test_score_is_confidence_in_pass(self):
        assert cal.cluster_confidence_score("PASS", 0.9) == pytest.approx(0.9)
        assert cal.cluster_confidence_score("FAIL", 0.9) == pytest.approx(0.1)

    def test_gate_recommended_only_when_auc_and_oof_gain_clear_the_bar(self):
        rng = random.Random(5)
        decisions, confs, truth, groups = [], [], [], []
        for i in range(200):
            single = rng.random() < 0.6
            # 결정은 항상 PASS(쓸모없음), confidence만 정보가 있다
            decisions.append("PASS")
            confs.append(min(1.0, max(0.0, (0.8 if single else 0.3) + rng.gauss(0, 0.1))))
            truth.append(single)
            groups.append(f"c{i}")

        out = cal.cluster_confidence_analysis(decisions, confs, truth, groups, seed=1,
                                              min_auc=0.75, min_gain=0.05)

        assert out["auc"] > 0.9
        assert out["baseline_balanced_accuracy"] == pytest.approx(0.5)
        assert out["oof_balanced_accuracy"] > 0.85
        assert out["gate_recommended"] is True

    def test_no_gate_when_confidence_adds_nothing_over_the_decision(self):
        rng = random.Random(9)
        decisions, confs, truth, groups = [], [], [], []
        for i in range(200):
            single = rng.random() < 0.6
            decisions.append("PASS" if single else "FAIL")
            confs.append(0.9)
            truth.append(single)
            groups.append(f"c{i}")

        out = cal.cluster_confidence_analysis(decisions, confs, truth, groups, seed=1,
                                              min_auc=0.75, min_gain=0.05)

        assert out["baseline_balanced_accuracy"] == pytest.approx(1.0)
        assert out["gate_recommended"] is False


def test_intra_rater_kappa_uses_only_items_labeled_twice():
    first = {"a": True, "b": False, "c": True, "d": False}
    second = {"a": True, "b": False, "c": False}
    out = cal.intra_rater_kappa(first, second)
    assert out["n"] == 3
    assert out["kappa"] == pytest.approx(cal.cohen_kappa([True, False, True], [True, False, False]))
