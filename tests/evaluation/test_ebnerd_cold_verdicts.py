"""콜드 regime 사슬의 기계 판정 함수: 손으로 만든 수치에 사전 등록 규칙(ADR 0013 A2.5)이 그대로 적용되는지."""
import copy

import pytest

from evaluation.recsys.ebnerd.cold_verdicts import (
    cold_verdicts,
    e1_verdict,
    e2_kstar,
    e3_verdict,
    e6_verdict,
    e7_verdict,
    e8_verdict,
    load_prereg,
    prereg_sha256,
    reproduction_gate,
)

P = load_prereg()


def D(diff, lo, hi=None):
    return {"ndcg@10": {"diff": diff, "ci95": [lo, diff + (diff - lo) if hi is None else hi], "n": 1000}}


def M(mean):
    return {"ndcg@10": {"mean": mean, "ci95": [mean - 0.004, mean + 0.004], "n": 1000}}


def report(cells=None, **sections):
    return {"e1": {"cells": cells or {}}, **sections}


# --- 사전 등록 파일 ---------------------------------------------------------------------------

def test_prereg_constants_are_read_from_the_registered_file():
    assert P["id"] == "ebnerd-cold-v1.2" and P["statistics"]["min_effect"] == 0.005
    assert P["run"]["stages"] == ["fit", "e1", "e2p2", "e8", "e4", "e2p1", "assemble"]
    assert len(prereg_sha256()) == 64


def test_code_constants_that_duplicate_the_registration_have_not_drifted():
    """콜드 정의(neural/cold.py)는 다른 실험과 공유하느라 yaml을 읽지 않고 상수를 따로 갖는다. 두 곳이 같아야 한다."""
    from evaluation.recsys.ebnerd import models
    from evaluation.recsys.ebnerd.cold_transforms import SHRUNK_COLUMN
    from evaluation.recsys.ebnerd.heuristic_fit import TERMS
    from evaluation.recsys.ebnerd.neural.cold import COLD_TRUNCATE_KS, POP_RAW_COLUMNS

    assert P["features"]["pop_raw_columns"] == list(POP_RAW_COLUMNS)
    assert P["features"]["shrunk_column"] == SHRUNK_COLUMN
    assert set(COLD_TRUNCATE_KS) <= set(P["conditions"]["truncate_ks"])       # E15 게이트의 k는 E2 격자의 부분집합
    assert set(P["features"]["rank_columns"]) <= set(models.V2_FEATURES)
    assert (P["lightgbm"]["num_boost_round"], P["lightgbm"]["early_stopping"]) == (models.NUM_BOOST_ROUND,
                                                                                 models.EARLY_STOPPING)
    assert not set(P["lightgbm"]["extra_params"]) & set(models.TEAM_PARAMS)   # 실행 옵션일 뿐 하이퍼파라미터가 아니다
    assert len(P["heuristics"]["fit_terms"]) == len(TERMS) == 4
    assert set(P["e6"]["alphas"]) == {c["shrunk_alpha"] for c in P["arms"].values() if "shrunk_alpha" in c}
    assert {P["e1"]["a"]["compare"][0], P["e3"]["compare"][0], P["e8"]["serving"]["model"],
            P["e2"]["p1_descriptive"]["model"], P["reproduction_gate"]["arm"]} <= set(P["arms"])


def test_registered_yaml_is_byte_identical_to_the_commit_that_registered_it():
    """사전 등록 값은 등록 커밋 뒤에 바뀌면 안 된다. 그 커밋의 yaml과 지금 파일을 바이트로 비교한다.
    얕은 체크아웃(CI)처럼 그 커밋이 없는 곳에서는 건너뛴다."""
    import subprocess

    from evaluation.recsys.ebnerd.cold_verdicts import PREREG_PATH, prereg_commit

    sha = prereg_commit()
    assert sha and len(sha) == 40
    repo = PREREG_PATH.parents[4]
    rel = PREREG_PATH.relative_to(repo).as_posix()
    have = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo, capture_output=True)
    if have.returncode != 0:
        pytest.skip("사전 등록 커밋이 이 체크아웃에 없음(얕은 클론)")
    registered = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=repo, capture_output=True, check=True).stdout
    assert registered == PREREG_PATH.read_bytes()
    parent = subprocess.run(["git", "cat-file", "-e", f"{sha}^:{rel}"], cwd=repo, capture_output=True)
    assert parent.returncode != 0          # 그 커밋이 yaml을 처음 담은 커밋이다(부모에는 없다)


# --- 재현 게이트 -------------------------------------------------------------------------------

@pytest.mark.parametrize("mean,status", [(0.2686, "pass"), (0.2640, "pass"), (0.2728, "pass"), (0.2639, "fail"),
                                         (0.2729, "fail")])
def test_reproduction_gate_requires_poolneg_inside_the_v1_interval(mean, status):
    d = report({"orig|full": {"methods": {"poolneg": M(mean)}, "diffs": {}}})
    assert reproduction_gate(d, P)["status"] == status


def test_reproduction_gate_is_unmeasured_without_the_cell():
    assert reproduction_gate(report(), P)["status"] == "unmeasured"


# --- E1 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("lo,passed", [(0.0051, True), (0.005, False), (0.0, False), (-0.01, False)])
def test_e1a_needs_ci_lower_bound_strictly_above_min_effect(lo, passed):
    d = report({"pop0|full": {"methods": {}, "diffs": {"poolneg_mask0-vs-poolneg": D(lo + 0.004, lo)}}})
    v = e1_verdict(d, P)["a"]
    assert v["status"] == "measured" and v["pass"] is passed
    assert v["decision"] == (P["e1"]["a"]["on_pass"] if passed else P["e1"]["a"]["on_fail"])


def test_e1b_keeps_heuristic_unless_a_ranker_beats_it_by_ci_and_min_effect():
    def run(pool_diff, mask_diff):
        return e1_verdict(report({"sub1|60": {"methods": {}, "diffs": {
            "poolneg-vs-heuristic_cold": pool_diff, "poolneg_mask0-vs-heuristic_cold": mask_diff}}}), P)["b"]

    none = run(D(0.02, -0.001), D(0.004, 0.001))   # 첫째는 CI가 0 포함, 둘째는 점추정이 최소 효과 미만
    assert none["status"] == "measured" and not none["any_beats"]
    assert none["decision"] == P["e1"]["b"]["on_none_beats"]
    one = run(D(0.02, -0.001), D(0.03, 0.002))
    assert one["any_beats"] and one["beats"] == {"poolneg": False, "poolneg_mask0": True}
    assert one["decision"] == P["e1"]["b"]["on_any_beats"]
    edge = run(D(0.005, 0.0001), D(0.0049, 0.0001))  # 점추정 0.005는 넘음, 0.0049는 못 넘음
    assert edge["beats"] == {"poolneg": True, "poolneg_mask0": False}


def test_e1_reports_unmeasured_when_a_comparison_is_missing():
    v = e1_verdict(report({"sub1|60": {"methods": {}, "diffs": {"poolneg-vs-heuristic_cold": D(0.1, 0.05)}}}), P)
    assert v["a"]["status"] == "unmeasured" and v["b"]["status"] == "unmeasured"
    assert "pass" not in v["a"]


# --- E2 ----------------------------------------------------------------------------------------

KS = [0, 1, 3, 5, 10, "all"]


def _e2(lows, gains=None):
    p2 = {}
    for i, (k, lo) in enumerate(zip(KS, lows)):
        if lo is None:
            continue
        cell = {"methods": {}, "diffs": {"poolneg-vs-popularity_6h": D(lo + 0.01, lo)}}
        if gains is not None and k != 0:
            cell["gain_vs_k0"] = D(gains[i] + 0.002, gains[i])
        p2[f"orig|k{k}"] = cell
    return {"e2": {"p2": p2}}


@pytest.mark.parametrize("lows,k_star,value", [
    ([0.1, 0.1, 0.1, 0.1, 0.1, 0.1], 0, 0),
    ([-0.01, 0.01, 0.02, 0.02, 0.03, 0.03], 1, 1),
    ([0.01, -0.01, 0.02, 0.02, 0.03, 0.03], 3, 3),        # k=0만 통과한 것은 인정하지 않는다(더 큰 k에서 실패)
    ([-0.01, -0.01, -0.01, -0.01, 0.0, 0.03], "all", None),  # 하한 0은 통과가 아니다. 유한한 k에서는 성립하지 않음
    ([-0.01] * 6, None, None),
])
def test_e2_kstar_is_the_smallest_k_passing_together_with_every_larger_k(lows, k_star, value):
    v = e2_kstar(_e2(lows), P)
    assert v["status"] == "measured" and v["k_star"] == k_star and v["min_personal_events"] == value


def test_e2_is_unmeasured_if_any_grid_point_is_missing_and_reports_k_gain():
    assert e2_kstar(_e2([0.1, 0.1, None, 0.1, 0.1, 0.1]), P)["status"] == "unmeasured"
    v = e2_kstar(_e2([0.1] * 6, gains=[None, 0.001, 0.004, 0.0051, 0.02, 0.03]), P)
    assert v["k_gain"] == 5   # CI 하한이 0.005를 넘는 최소 k (서술용)


# --- E3 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("diff,lo,status", [(-0.004, -0.009, "met"), (0.002, -0.001, "met"),
                                            (-0.015, -0.02, "not_met"), (-0.004, -0.012, "inconclusive"),
                                            (-0.01, -0.015, "not_met"), (-0.005, -0.01, "inconclusive")])
def test_e3_three_way_classification(diff, lo, status):
    d = report({"orig|full": {"methods": {}, "diffs": {"poolneg_rank-vs-poolneg": D(diff, lo)}}})
    assert e3_verdict(d, P)["status"] == status


def test_e3_unmeasured():
    assert e3_verdict(report(), P)["status"] == "unmeasured"


# --- E6 ----------------------------------------------------------------------------------------

def _e6(table):
    cells = {}
    for traffic in ("sub1", "sub5"):
        cells[f"{traffic}|full"] = {"methods": {}, "diffs": {
            f"poolneg_shrunk_a{a}-vs-poolneg": D(*table[(traffic, a)]) for a in (5, 20, 50) if (traffic, a) in table}}
    return report(cells)


def test_e6_adopts_when_any_of_the_six_comparisons_clears_min_effect():
    base = {(t, a): (0.001, -0.004) for t in ("sub1", "sub5") for a in (5, 20, 50)}
    assert e6_verdict(_e6(base), P)["adopted_alpha"] is None
    one = dict(base)
    one[("sub5", 20)] = (0.012, 0.006)
    v = e6_verdict(_e6(one), P)
    assert v["status"] == "measured" and v["adopted_alpha"] == 20 and v["passing"] == [["sub5", 20]]
    # 여러 α가 통과하면 두 서브샘플 점추정 중 작은 쪽이 가장 큰 α
    two = dict(one)
    two[("sub1", 50)] = (0.03, 0.006)
    two[("sub5", 50)] = (0.004, -0.002)     # α=50의 작은 쪽 = 0.004
    two[("sub1", 20)] = (0.006, -0.002)     # α=20의 작은 쪽 = 0.006 -> α=20
    assert e6_verdict(_e6(two), P)["adopted_alpha"] == 20
    boundary = dict(base)
    boundary[("sub1", 5)] = (0.02, 0.005)   # 하한이 정확히 0.005면 통과가 아니다
    assert e6_verdict(_e6(boundary), P)["adopted_alpha"] is None


def test_e6_unmeasured_when_a_comparison_is_missing():
    table = {(t, a): (0.02, 0.01) for t in ("sub1", "sub5") for a in (5, 20, 50)}
    del table[("sub1", 50)]
    assert e6_verdict(_e6(table), P)["status"] == "unmeasured"


# --- E7 ----------------------------------------------------------------------------------------

def _e7(a_prior, a_pop, b_prior, b_pop):
    return report({
        "orig|full": {"methods": {}, "diffs": {"heuristic_fit_a-vs-heuristic_prior4": a_prior,
                                                "heuristic_fit_a-vs-popularity_6h": a_pop}},
        "sub1|full": {"methods": {}, "diffs": {"heuristic_fit_b-vs-heuristic_prior4": b_prior,
                                                "heuristic_fit_b-vs-popularity_6h": b_pop}},
    })


def test_e7_labels_each_set_and_picks_the_serving_default():
    v = e7_verdict(_e7(D(0.02, 0.006), D(0.01, 0.001), D(0.004, -0.01), D(-0.002, -0.02)), P)
    assert v["status"] == "measured"
    assert v["sets"]["a"] == {**v["sets"]["a"], "beats_prior": True, "role": "scorer"}
    assert v["sets"]["b"]["beats_prior"] is False and v["sets"]["b"]["role"] == "tie_breaker"
    assert v["serving_default"] == "b"      # (b)가 사전값보다 CI로 나쁘지 않으면 기본값은 (b)
    worse = e7_verdict(_e7(D(0.02, 0.006), D(0.01, 0.001), D(-0.02, -0.03, -0.001), D(0.0, -0.02)), P)
    assert worse["serving_default"] == "prior"   # (b) - 사전값의 CI 상한 < 0
    edge = e7_verdict(_e7(D(0.02, 0.005), D(0.01, 0.0), D(-0.02, -0.03, 0.0), D(0.0, -0.02)), P)
    assert edge["sets"]["a"]["beats_prior"] is False and edge["sets"]["a"]["role"] == "tie_breaker"
    assert edge["serving_default"] == "b"        # 상한이 정확히 0이면 "나쁨"이 아니다


def test_e7_unmeasured():
    assert e7_verdict(report({"orig|full": {"methods": {}, "diffs": {}}}), P)["status"] == "unmeasured"


# --- E8 ----------------------------------------------------------------------------------------

def _cfg(recall, lo):
    return {"union_recall": recall, "two_stage": {"paired_vs_full_pool": D(lo + 0.002, lo)}}


@pytest.mark.parametrize("serving,harness,decision", [
    ((0.93, -0.003), (0.91, -0.001), "keep_serving"),
    ((0.90, -0.0049), None, "keep_serving"),                  # 서빙이 통과하면 하네스 수치는 필요 없다
    ((0.899, -0.001), (0.91, -0.001), "switch_to_harness"),   # 재현율 미달
    ((0.95, -0.005), (0.91, -0.001), "switch_to_harness"),    # 하한이 정확히 -0.005면 통과가 아니다
    ((0.85, -0.02), (0.89, -0.001), "redesign"),
    ((0.85, -0.02), None, "unmeasured"),
])
def test_e8_decision(serving, harness, decision):
    e8 = {"serving": _cfg(*serving)}
    if harness:
        e8["harness"] = _cfg(*harness)
    v = e8_verdict({"e8": e8}, P)
    assert v["decision"] == decision
    assert v["status"] == ("unmeasured" if decision == "unmeasured" else "measured")


def test_e8_unmeasured_without_serving_numbers():
    assert e8_verdict({"e8": {}}, P)["status"] == "unmeasured"


# --- 묶음 --------------------------------------------------------------------------------------

def _full_report(grade="preregistered-run", poolneg=0.2686):
    cells = {
        "orig|full": {"methods": {"poolneg": M(poolneg)}, "diffs": {
            "poolneg_rank-vs-poolneg": D(-0.002, -0.006), "heuristic_fit_a-vs-heuristic_prior4": D(0.02, 0.01),
            "heuristic_fit_a-vs-popularity_6h": D(0.02, 0.01)}},
        "pop0|full": {"methods": {}, "diffs": {"poolneg_mask0-vs-poolneg": D(0.02, 0.01)}},
        "sub1|60": {"methods": {}, "diffs": {"poolneg-vs-heuristic_cold": D(0.05, 0.01),
                                              "poolneg_mask0-vs-heuristic_cold": D(0.05, 0.01)}},
        "sub1|full": {"methods": {}, "diffs": {"heuristic_fit_b-vs-heuristic_prior4": D(0.02, 0.01),
                                                "heuristic_fit_b-vs-popularity_6h": D(0.02, 0.01)}},
        "sub5|full": {"methods": {}, "diffs": {}},
    }
    return {"meta": {"evidence": {"grade": grade, "reasons": []}}, "e1": {"cells": cells},
            **_e2([0.1] * 6), "e8": {"serving": _cfg(0.95, -0.001)}}


def test_cold_verdicts_lists_unmeasured_items_and_judges_only_valid_registered_runs():
    v = cold_verdicts(_full_report(), P)
    assert v["judgeable"] is True and v["gate"]["status"] == "pass"
    assert v["unmeasured"] == ["e6"]                       # E6 비교가 없으면 실패가 아니라 미측정
    assert v["verdicts"]["e1"]["a"]["pass"] and v["verdicts"]["e2"]["k_star"] == 0
    demo = cold_verdicts(_full_report(grade="demo, not evidence"), P)
    assert demo["judgeable"] is False and "demo" in demo["not_judgeable_reason"]
    gate_fail = cold_verdicts(_full_report(poolneg=0.20), P)
    assert gate_fail["judgeable"] is False and gate_fail["gate"]["status"] == "fail"
    # 판정 함수는 입력을 바꾸지 않는다
    d = _full_report()
    snapshot = copy.deepcopy(d)
    cold_verdicts(d, P)
    assert d == snapshot
