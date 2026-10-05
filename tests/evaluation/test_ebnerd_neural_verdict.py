"""E15 기계 판정(neural/report.py): 손으로 만든 수치로 seed 규칙, 게이트 3분류, Holm, 결과 분류, 주장을 본다.

임계값은 사전 등록 yaml에서 읽는다. 테스트의 기대값은 등록 문장(A3.7)을 손으로 계산한 것이다.
"""
import copy
import math

import pytest

from evaluation.recsys.ebnerd.neural.report import (
    DEMO_GRADE,
    EVIDENCE_GRADE,
    arm_verdict,
    gate3,
    holm_step_down,
    load_prereg,
    neural_verdict,
    prereg_commit,
    render,
    seed_rule,
)

PREREG = load_prereg()
STATS = PREREG["statistics"]


def entry(diff, hw=0.002, seed_diffs=None, seed_los=None, lo90=None, holm=None):
    seed_diffs = [diff] * 3 if seed_diffs is None else seed_diffs
    seed_los = [d - hw for d in seed_diffs] if seed_los is None else seed_los
    lo = diff - hw
    return {"diff": diff, "ci95": [lo, diff + hw], "ci90": [diff - 0.8 * hw if lo90 is None else lo90, diff + 0.8 * hw],
            "holm_lo": holm or {str(m): lo - 0.0005 * (m - 1) for m in (1, 2, 3, 4)},
            "per_seed": [{"seed": s, "diff": d, "ci95": [l, d + hw]} for s, (d, l) in enumerate(zip(seed_diffs, seed_los))]}


def test_seed_rule_needs_every_seed_positive_and_the_combined_bound_above_min_effect():
    r = seed_rule(entry(0.010, seed_diffs=[0.0100, 0.0101, 0.0099]), STATS)
    sd = 0.0001
    assert r["status"] == "pass" and r["sd_seed"] == pytest.approx(sd)
    assert r["combined_lower"] == pytest.approx(0.010 - math.sqrt(0.002 ** 2 + (4.30 * sd / math.sqrt(3)) ** 2))
    # (a) 한 seed의 하한이 0 이하면 불통과
    r = seed_rule(entry(0.010, seed_diffs=[0.0100, 0.0101, 0.0099], seed_los=[0.008, 0.0, 0.008]), STATS)
    assert r["status"] == "fail" and not r["a_each_seed_lo_gt_0"] and r["b_combined_lo_gt_min_effect"]
    # (b) seed 사이 변동이 크면 부트스트랩 CI가 좁아도 불통과: 0.010 − sqrt(0.002² + (4.30·0.006/√3)²) < 0
    r = seed_rule(entry(0.010, seed_diffs=[0.004, 0.010, 0.016]), STATS)
    assert r["status"] == "fail" and r["a_each_seed_lo_gt_0"] and r["combined_lower"] < 0


def test_seed_rule_reports_unmeasured_when_a_seed_is_missing():
    e = entry(0.010)
    e["per_seed"] = e["per_seed"][:2]
    assert seed_rule(e, STATS)["status"] == "unmeasured" and seed_rule(None, STATS)["status"] == "unmeasured"


def test_gate_has_three_classes_and_a_strict_inequality():
    assert gate3(entry(-0.001, lo90=-0.0049), STATS)["status"] == "pass"
    assert gate3(entry(-0.001, lo90=-0.005), STATS)["status"] == "hold"        # 하한이 정확히 −0.005면 통과가 아니다
    assert gate3(entry(-0.001, lo90=-0.007), STATS)["status"] == "hold"        # 점추정은 여유 안, 하한만 밖: 검정력 부족
    assert gate3(entry(-0.005, lo90=-0.007), STATS)["status"] == "fail"
    assert gate3(entry(-0.009, lo90=-0.011), STATS)["status"] == "fail"
    assert gate3(None, STATS)["status"] == "unmeasured"


def test_holm_step_down_loosens_the_level_only_after_a_rejection():
    strong = {"4": 0.006, "3": 0.0061, "2": 0.0062, "1": 0.0063}
    only_at_2 = {"4": 0.004, "3": 0.0045, "2": 0.0051, "1": 0.0055}
    got = holm_step_down({"a": {"holm_lo": strong}, "b": {"holm_lo": strong}, "c": {"holm_lo": only_at_2}, "d": None}, STATS)
    assert got == {"a": True, "b": True, "c": True, "d": False}
    # 가장 엄격한 수준(m=4)을 아무도 못 넘으면, 느슨한 수준에서 넘더라도 하나도 기각되지 않는다
    weak = {"4": 0.0049, "3": 0.006, "2": 0.006, "1": 0.006}
    assert not any(holm_step_down({k: {"holm_lo": weak} for k in "abcd"}, STATS).values())
    with pytest.raises(ValueError):
        holm_step_down({"a": {"holm_lo": strong}}, STATS)


def _report(grade=EVIDENCE_GRADE, families=("nrms", "nrms")):
    """두 과제 모두 sel·D가 모든 조건을 넉넉히 통과하는 리포트."""
    d = {"meta": {"evidence": {"grade": grade, "reasons": [] if grade == EVIDENCE_GRADE else ["seeds"]},
                  "preregistration": {"id": PREREG["id"], "sha256": "0" * 64, "commit": None}, "label": "[EB-NeRD]",
                  "code_sha": "c" * 40},
         "validity": {}, "selection": {}, "tasks": {}}
    for task, fam in zip(("p1", "p2"), families):
        diffs = {}
        for arm in (fam, "D"):
            diffs[f"{arm}-vs-A_star"] = entry(0.012)
            diffs[f"{arm}-vs-A_plus"] = entry(0.010)
        diffs["A_star-vs-A"] = entry(0.001, hw=0.002)
        diffs["A_plus-vs-A_star"] = entry(0.003, hw=0.001)
        cold = {c: {"diffs": {f"{arm}-vs-A_star": entry(0.002) for arm in (fam, "D")}} for c in PREREG["cold"]["conditions"]}
        d["tasks"][task] = {"arms": {}, "diffs": diffs, "cold": cold}
        d["selection"][task] = {"family": fam}
        d["validity"][task] = {"reproduction": {"status": "pass"}, "determinism": {"status": "pass"}}
    return d


def test_all_statistical_conditions_met_is_still_not_shadow_eligibility():
    d = _report()
    v = neural_verdict(d, PREREG)
    assert v["judged"] and v["claim"] == "neural_arm_both_tasks" and v["unmeasured"] == []
    assert {v["by_task"][t][a]["outcome"] for t in ("p1", "p2") for a in ("sel", "D")} == {"pending_serving_cost_gate"}
    assert v["shadow_eligible"] == [] and v["serving_cost_gate"] == "deferred"      # 서빙 비용 게이트를 재지 않았다
    assert sorted(v["statistical_requirements_met"]) == ["p1:D", "p1:sel", "p2:D", "p2:sel"]
    assert v["side"]["p1:A_plus-vs-A_star"]["status"] == "fail"                     # 0.003 − ... < 0.005
    assert v["side"]["p1:sel-vs-A_plus"]["conclusion"]
    text = render({**d, "verdict": v})
    assert "서빙 비용 게이트 대기" in text and "판정용 실행" in text


def test_claim_needs_the_same_neural_family_on_both_tasks():
    v = neural_verdict(_report(families=("nrms", "sasrec")), PREREG)
    assert v["claim"] == "stacking_gain" and v["claim_tasks_stacking"] == ["p1", "p2"]
    d = _report(families=("nrms", "sasrec"))
    for t in ("p1", "p2"):
        d["tasks"][t]["diffs"]["D-vs-A_star"] = entry(0.001, hw=0.002)
    v = neural_verdict(d, PREREG)
    assert v["claim"] == "none" and v["by_task"]["p1"]["D"]["outcome"] == "not_distinguishable"


def test_claim_needs_holm_and_the_information_matched_control():
    d = _report()
    d["tasks"]["p2"]["diffs"]["nrms-vs-A_star"]["holm_lo"] = {"4": 0.004, "3": 0.004, "2": 0.004, "1": 0.004}
    v = neural_verdict(d, PREREG)
    assert v["holm"]["p2:sel"] is False and v["by_task"]["p2"]["sel"]["outcome"] == "pending_serving_cost_gate"
    assert v["claim"] == "stacking_gain"                     # 판정(비보정)은 통과해도 주장은 풀리지 않는다
    d = _report()
    d["tasks"]["p1"]["diffs"]["nrms-vs-A_plus"] = entry(0.004, hw=0.001)      # 게이트(> −0.005)는 통과, seed 규칙은 불통과
    v = neural_verdict(d, PREREG)
    assert v["by_task"]["p1"]["sel"]["gates"]["info_control"]["status"] == "pass" and v["claim"] == "stacking_gain"


@pytest.mark.parametrize("mutate, outcome", [
    (lambda b: b["diffs"].__setitem__("nrms-vs-A_star", entry(0.001, hw=0.002)), "not_distinguishable"),
    (lambda b: b["diffs"].__setitem__("nrms-vs-A_star", entry(0.006, hw=0.002)), "below_min_effect"),
    (lambda b: b["diffs"].__setitem__("nrms-vs-A_star", entry(0.010, seed_diffs=[0.004, 0.010, 0.016])), "seed_rule_failed"),
    (lambda b: b["diffs"].__setitem__("nrms-vs-A_star", entry(-0.010)), "seed_rule_failed"),
    (lambda b: b["cold"]["k0"]["diffs"].__setitem__("nrms-vs-A_star", entry(-0.009, lo90=-0.012)), "gate_failed"),
    (lambda b: b["cold"]["pop0"]["diffs"].__setitem__("nrms-vs-A_star", entry(-0.002, lo90=-0.006)), "gate_hold"),
    (lambda b: b["cold"].pop("k5"), "unmeasured"),
    (lambda b: b["diffs"].pop("nrms-vs-A_star"), "unmeasured"),
    (lambda b: b["diffs"].__setitem__("nrms-vs-A_plus", entry(-0.008, lo90=-0.010)), "gate_failed"),
])
def test_outcome_classes_follow_the_registered_order(mutate, outcome):
    d = _report()
    mutate(d["tasks"]["p1"])
    before = copy.deepcopy(d)
    r = arm_verdict(d, "p1", "sel", PREREG)
    assert r["outcome"] == outcome and r["statistical_requirements_met"] is False
    assert d == before                                                         # 입력을 바꾸지 않는다
    assert arm_verdict(d, "p2", "sel", PREREG)["outcome"] == "pending_serving_cost_gate"


def test_a_failed_gate_outranks_a_hold_and_validity_failure_invalidates_the_run():
    d = _report()
    d["tasks"]["p1"]["cold"]["k1"]["diffs"]["D-vs-A_star"] = entry(-0.002, lo90=-0.006)
    d["tasks"]["p1"]["cold"]["k3"]["diffs"]["D-vs-A_star"] = entry(-0.009, lo90=-0.012)
    assert arm_verdict(d, "p1", "D", PREREG)["outcome"] == "gate_failed"
    d["validity"]["p1"]["determinism"] = {"status": "fail"}
    v = neural_verdict(d, PREREG)
    assert v["by_task"]["p1"]["sel"]["outcome"] == v["by_task"]["p1"]["D"]["outcome"] == "invalid_run"
    assert v["claim"] == "stacking_gain" and v["claim_tasks_stacking"] == ["p2"]
    d["validity"]["p2"].pop("reproduction")
    assert neural_verdict(d, PREREG)["by_task"]["p2"]["sel"]["outcome"] == "unmeasured"


def test_unfinished_run_is_unmeasured_not_rejected_and_demo_runs_are_not_judged():
    d = _report(grade=DEMO_GRADE)
    d["tasks"].pop("p2")
    d["selection"].pop("p2")
    v = neural_verdict(d, PREREG)
    assert v["judged"] is False and v["claim"] == "none" and v["claim_rule_output"] == "stacking_gain"   # demo는 주장을 풀지 않는다
    assert v["by_task"]["p2"]["sel"]["outcome"] == v["by_task"]["p2"]["D"]["outcome"] == "unmeasured"
    assert {"p2:sel", "p2:D"} <= set(v["unmeasured"])
    assert "판정 아님" in render({**d, "verdict": v})


def test_registered_rule_tables_are_internally_consistent():
    assert len(PREREG["judged_comparisons"]) == STATS["holm"]["family_size"] == 4
    assert {c["reference"] for c in PREREG["judged_comparisons"]} == {"A_star"}
    assert PREREG["gates"]["cold"]["conditions"] == PREREG["cold"]["conditions"]
    assert PREREG["gates"]["serving_cost"]["status"] == "deferred"
    assert STATS["seed_rule"]["n_seeds"] == len(PREREG["run"]["seeds"]) == 3


def test_recorded_preregistration_commit_holds_the_same_yaml_bytes():
    """yaml 옆 .commit에 적힌 커밋의 yaml이 지금 파일과 바이트로 같다(등록 뒤 고치지 않았다)."""
    import subprocess
    from evaluation.recsys.ebnerd.neural.report import PREREG_PATH

    sha = prereg_commit()
    assert sha and len(sha) == 40
    repo = PREREG_PATH.parents[4]
    rel = PREREG_PATH.relative_to(repo).as_posix()
    try:
        blob = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=repo, capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("git 이력에서 사전 등록 커밋을 읽을 수 없다(얕은 체크아웃)")
    assert blob == PREREG_PATH.read_bytes()
