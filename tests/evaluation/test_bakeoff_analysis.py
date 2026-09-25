# 사전 등록 규칙(ADR 0009)을 실제 bakeoff-v1.yaml 상수로 적용하는 분석기. 합성 결과로 규칙의
# 각 분기(게이트 탈락, 명확한 승자, 동률 -> 비용, 전원 탈락, 라벨 신뢰도 부족, judge 선정)를 검증한다.
import math
import random

import pytest

from evaluation.llm import bakeoff_analysis as ba
from evaluation.llm.bakeoff import load_prereg

PREREG = load_prereg()
A, B, C = (c["name"] for c in PREREG["candidates"])  # gemini-3.5-flash-lite, solar-pro3, gpt-4.1-mini
ITEMS = [f"r1-c{i}" for i in range(40)]


def _call(first_try=True, responded=True):
    return {"purpose": "x", "responded": responded, "first_try_schema_pass": first_try and responded}


def gen(cand, item, *, draft=True, unsupported=False, drift=False, blocked=None, latency=10.0,
        gen_cost=0.01, tone_cost=0.002, first_try=True):
    return {
        "candidate": cand, "item_id": item,
        "generator": next(c["generator"] for c in PREREG["candidates"] if c["name"] == cand),
        "draft": {"title": "t", "sentence": "s", "content": "삼성전자가 3조 원을 투자한다."} if draft else None,
        "flags": {"generation_failed": not draft, "unsupported_number": unsupported,
                  "ops_gate_blocked": unsupported if blocked is None else blocked, "tone_drift": drift},
        "fallback": {"content": not draft, "meta": False, "tone": False},
        "calls": {"generation": [_call(first_try), _call()], "tone": [_call()] if draft else []},
        "timing": {"total_s": latency},
        "cost_usd": {"generation": gen_cost, "tone": tone_cost if draft else 0.0},
    }


class World:
    """생성 행 + 블라인드 키 + 사람 라벨을 함께 만든다."""

    def __init__(self):
        self.gens, self.key, self.out1, self.out2 = [], {}, {}, {}
        self.clusters = {i: {"single_event": True, "outlier_ids": [],
                             "key_facts": [{"text": "f", "source_ids": [1]}] * 3} for i in ITEMS}

    def add(self, cand, n_publishable, **kw):
        for k, item in enumerate(ITEMS):
            g = gen(cand, item, **kw)
            self.gens.append(g)
            if g["draft"]:
                oid = f"o-{cand}-{item}"
                self.key[oid] = {"candidate": cand, "item_id": item}
                self.out1[oid] = {"publishable": k < n_publishable, "style": 4, "tone_drift": False,
                                  "key_facts_covered": [0, 1], "fact_errors": []}
        return self

    def relabel(self, n=30, flip=0):
        # export-blind처럼 후보를 가리지 않고 무작위로 고른다
        for k, oid in enumerate(random.Random(0).sample(sorted(self.out1), min(n, len(self.out1)))):
            lab = dict(self.out1[oid])
            if k < flip:
                lab["publishable"] = not lab["publishable"]
            self.out2[oid] = lab
        return self

    def labels(self):
        return {"output": {1: self.out1, 2: self.out2}, "cluster": {1: self.clusters, 2: {}}}

    def analyze(self, judgments=()):
        return ba.analyze(PREREG, self.gens, self.labels(), {"outputs": self.key}, judgments=judgments)


def test_cost_formula_matches_the_preregistered_expected_call_counts():
    got = ba.cost_per_100(0.01, 0.002, p=0.2, q=0.1, cost_model=PREREG["decision_rule"]["cost_model"])
    assert got == pytest.approx(100 * (0.01 * (1 + 0.2 + 0.04) + 0.002 * (1 + 0.1)))


def test_gates_and_metrics_are_computed_per_candidate():
    w = World().add(A, 30, latency=10.0).relabel()
    w.gens[0]["timing"]["total_s"] = 500.0  # 한 건만 느려도 p95는 크게 안 움직인다
    w.gens[1]["calls"]["generation"][0] = _call(first_try=False)
    w.gens[2]["calls"]["generation"][0] = _call(responded=False)  # 전송 실패만 - G3 분모에서 제외
    m = ba.candidate_metrics(A, w.gens, ba.human_output_labels(w.labels(), {"outputs": w.key}),
                             w.clusters, PREREG)

    assert m["publishable_rate"] == pytest.approx(0.75)
    assert m["first_try_schema_pass_rate"] == pytest.approx(118 / 119)
    assert m["p95_latency_s"] == pytest.approx(10.0)
    assert m["key_fact_coverage"] == pytest.approx(2 / 3)
    assert m["passes_gates"] is True


def test_a_gate_failure_removes_even_the_most_publishable_candidate():
    w = World()
    w.add(A, 38, unsupported=True)  # 사실성 G1 탈락(100% > 15%)
    w.add(B, 20)
    w.add(C, 10)
    w.relabel()
    report = w.analyze()

    assert report["candidates"][A]["gates"]["G1_unsupported_number"] is False
    assert report["decision"]["winner"] == B
    assert A not in report["decision"]["survivors"]


def test_clear_publishable_gap_beats_a_cheaper_candidate():
    w = World().add(A, 36, gen_cost=0.05).add(B, 12, gen_cost=0.001).add(C, 12, gen_cost=0.001).relabel()
    d = w.analyze()["decision"]

    assert d["winner"] == A and d["tie_set"] == [A]
    assert d["comparisons_vs_top"][B]["ci"][0] > 0


def test_indistinguishable_rates_are_a_tie_broken_by_cost():
    w = World().add(A, 22, gen_cost=0.05).add(B, 20, gen_cost=0.001).add(C, 2, gen_cost=0.0001).relabel()
    d = w.analyze()["decision"]

    assert d["top_publishable"] == A
    assert B in d["tie_set"] and C not in d["tie_set"]
    assert d["winner"] == B


def test_everyone_failing_a_gate_keeps_the_incumbent():
    w = World().add(A, 30, drift=True).add(B, 30, latency=90.0).add(C, 30, first_try=False).relabel()
    d = w.analyze()["decision"]

    assert d["winner"] is None and d["action"] == "keep_incumbent"
    assert d["failed_gates"] == {A: ["G2_tone_drift"], B: ["G4_p95_latency"], C: ["G3_first_try_schema"]}


def test_unreliable_human_labels_fall_back_to_cheapest_passing_candidate():
    w = World().add(A, 36, gen_cost=0.05).add(B, 12, gen_cost=0.001).add(C, 12, gen_cost=0.002)
    w.relabel(n=30, flip=12)  # 30개 중 12개가 뒤집힘 -> κ가 0.6보다 한참 낮다
    report = w.analyze()

    assert report["human_reliability"]["publishable"]["kappa"] < 0.6
    assert report["decision"]["winner"] == B
    assert report["decision"]["primary_metric_reliable"] is False


def test_missing_relabels_make_the_decision_provisional_not_unreliable():
    w = World().add(A, 36).add(B, 12).add(C, 12)
    report = w.analyze()
    assert report["decision"]["winner"] == A and report["decision"]["provisional"] is True


def test_undefined_relabel_kappa_is_reported_not_treated_as_below_threshold():
    w = World().add(A, 36).add(B, 12).add(C, 12)
    for oid in sorted(w.out1)[:30]:  # 모두 같은 범주(Y)만 재라벨 -> κ 정의 불가
        if w.out1[oid]["publishable"]:
            w.out2[oid] = dict(w.out1[oid])
    report = w.analyze()
    assert math.isnan(report["human_reliability"]["publishable"]["kappa"])
    assert report["decision"]["winner"] == A
    assert report["decision"]["primary_metric_reliable"] is None and report["decision"]["provisional"] is True


def test_a_failed_generation_counts_as_not_publishable():
    w = World().add(A, 40).relabel()
    for g in w.gens[:4]:
        g["draft"] = None
        g["flags"]["generation_failed"] = True
    m = ba.candidate_metrics(A, w.gens, ba.human_output_labels(w.labels(), {"outputs": w.key}), w.clusters, PREREG)
    assert m["publishable_rate"] == pytest.approx(36 / 40)
    assert m["n_generation_failed"] == 4


def _judgments(world, name, family, version, agree, seed):
    rng = random.Random(seed)
    human = ba.human_output_labels(world.labels(), {"outputs": world.key})
    rows = []
    for g in world.gens:
        lab = human.get((g["candidate"], g["item_id"]))
        if lab is None:
            continue
        good = lab["publishable"] if rng.random() < agree else rng.random() < 0.5
        result = ({"criteria": {k: 5 if good else 2 for k in ("faithfulness", "coverage", "coherence", "style")},
                   "unsupported_claims": []} if version == "v2" else {"score": 8 if good else 3})
        rows.append({"candidate": g["candidate"], "item_id": g["item_id"], "judge": name, "judge_version": version,
                     "judge_family": family, "judge_model": name, "result": result, "cost_usd": 0.01,
                     "generator_family": g["generator"]["provider"]})
    return rows


def test_judge_selection_excludes_the_winners_family_and_requires_oof_kappa():
    w = World().add(A, 36).add(B, 12).add(C, 12).relabel()
    js = (_judgments(w, "gemini-judge", "gemini", "v2", agree=1.0, seed=1)  # 완벽하지만 승자(A)와 같은 계열
          + _judgments(w, "haiku-v2", "anthropic", "v2", agree=0.9, seed=2)
          + _judgments(w, "haiku-v1", "anthropic", "v1", agree=0.2, seed=3))
    report = w.analyze(judgments=js)

    assert report["decision"]["winner"] == A
    sel = report["judge_selection"]
    assert sel["judge"] == "haiku-v2" and sel["mode"] == "enforce"
    assert report["judges"]["gemini-judge"]["oof_kappa"] == pytest.approx(1.0)
    assert report["judges"]["haiku-v1"]["oof_kappa"] < 0.4
    assert report["self_preference"]["reference_judge"] == "haiku-v2"


def test_low_agreement_judges_run_in_shadow_mode():
    w = World().add(A, 36).add(B, 12).add(C, 12).relabel()
    report = w.analyze(judgments=_judgments(w, "haiku-v2", "anthropic", "v2", agree=0.1, seed=4))
    assert report["judge_selection"]["mode"] == "shadow"
    assert report["judge_selection"]["threshold"] is None


def test_claim_precision_matches_judge_claims_to_human_error_spans():
    draft = {"title": "t", "sentence": "s", "content": "삼성전자가 3조 원을 투자한다. 주가는 5% 올랐다."}
    gens = {("A", "i"): {"draft": draft}}
    start = draft["content"].index("3조 원")
    human = {("A", "i"): {"fact_errors": [{"field": "content", "start": start, "end": start + 4}]}}
    js = [{"candidate": "A", "item_id": "i", "result": {"unsupported_claims": [
        {"claim": "3조 원을 투자한다"}, {"claim": "주가는 5% 올랐다"}, {"claim": "원문에 없는 문장"}]}}]

    out = ba.claim_precision(js, gens, human)

    assert out == {"precision": 0.5, "n_located": 2, "n_unlocated": 1}


def test_summary_has_no_generated_text():
    w = World().add(A, 36).add(B, 12).add(C, 12).relabel()
    md = ba.summary_markdown(w.analyze())
    assert "삼성전자" not in md and A in md
    assert not math.isnan(w.analyze()["candidates"][A]["cost_per_100_usd"])
