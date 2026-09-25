"""bake-off 분석 - 사전 등록 규칙(ADR 0009, preregistration/bakeoff-v1.yaml)을 기계적으로 적용한다.

규칙의 상수(게이트 임계값, 부트스트랩 횟수/seed, κ 기준 등)는 전부 사전 등록 파일에서 읽는다.
이 파일에 숫자를 새로 쓰지 않는다 - 결과를 본 뒤 기준을 움직일 여지를 없애기 위해서다.

  python -m evaluation.llm.bakeoff_analysis --run bakeoff-v1

입력: data/bakeoff/<run>/{generations,judgments,cluster_evals}.jsonl, blind_key.json,
      data/labels/<run>/*_labels.jsonl
출력: data/bakeoff/<run>/analysis.json (+ 표준출력 요약). 기사 본문·생성물은 출력에 넣지 않는다.
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from evaluation.llm import calibration as cal

FIELDS = ("title", "sentence", "content")


# ---------------------------------------------------------------------------
# 후보별 지표
# ---------------------------------------------------------------------------

def _mean(xs) -> float:
    xs = [float(x) for x in xs if x is not None]
    return float(np.mean(xs)) if xs else float("nan")


def human_output_labels(labels: dict, blind_key: dict, round_: int = 1) -> Dict[Tuple[str, str], dict]:
    """(candidate, item_id) -> 출력 라벨."""
    out = {}
    for oid, label in labels["output"][round_].items():
        k = blind_key["outputs"].get(oid)
        if k:
            out[(k["candidate"], k["item_id"])] = label
    return out


def publishable_by_item(candidate: str, gens: Sequence[dict], human: dict) -> Dict[str, Optional[bool]]:
    """item_id -> 발행 가능 여부. 생성 실패는 N, 라벨이 아직 없으면 None."""
    out = {}
    for g in gens:
        if g["candidate"] != candidate:
            continue
        if not g.get("draft"):
            out[g["item_id"]] = False
        else:
            lab = human.get((candidate, g["item_id"]))
            out[g["item_id"]] = None if lab is None else bool(lab["publishable"])
    return out


def cost_per_100(gen_cost: float, tone_cost: float, p: float, q: float, cost_model: dict) -> float:
    """100 × (gen_cost × Σ_{k<G} p^k + tone_cost × Σ_{k<T} q^k) - 운영 게이트의 재생성/재변환
    기대 호출 수를 반영한다(실패 독립 가정). G/T는 사전 등록한 최대 시도 수."""
    g = sum(p ** k for k in range(int(cost_model["generation_attempts_max"])))
    t = sum(q ** k for k in range(int(cost_model["tone_attempts_max"])))
    return 100.0 * (gen_cost * g + tone_cost * t)


def candidate_metrics(candidate: str, gens: Sequence[dict], human: dict, cluster_labels: dict,
                      prereg: dict) -> dict:
    rows = [g for g in gens if g["candidate"] == candidate]
    drafts = [g for g in rows if g.get("draft")]
    calls = [c for g in rows for stage in ("generation", "tone") for c in g["calls"][stage]]
    responded = [c for c in calls if c["responded"]]
    pub = publishable_by_item(candidate, gens, human)
    labeled = {i: v for i, v in pub.items() if v is not None}

    single = {i for i, lab in cluster_labels.items() if lab.get("single_event")}
    coverage, style = [], []
    for g in drafts:
        lab = human.get((candidate, g["item_id"]))
        if lab is None:
            continue
        style.append(lab["style"])
        n_facts = len((cluster_labels.get(g["item_id"]) or {}).get("key_facts") or [])
        if n_facts:
            coverage.append(len(set(lab["key_facts_covered"])) / n_facts)

    gen_cost = _mean(g["cost_usd"]["generation"] for g in rows)
    tone_cost = _mean(g["cost_usd"]["tone"] for g in drafts)
    p = _mean(g["flags"]["ops_gate_blocked"] for g in drafts)
    q = _mean(g["flags"]["tone_drift"] for g in drafts)
    latencies = [g["timing"]["total_s"] for g in rows]

    m = {
        "n_rows": len(rows),
        "n_generation_failed": len(rows) - len(drafts),
        "n_fallback_content": sum(bool(g["fallback"]["content"]) for g in rows),
        "n_labeled": len(labeled),
        "labels_complete": len(labeled) == len(rows),
        "publishable_rate": _mean(labeled.values()),
        "publishable_rate_single_event": _mean(v for i, v in labeled.items() if i in single),
        "key_fact_coverage": _mean(coverage),
        "style_mean": _mean(style),
        "unsupported_number_rate": _mean(g["flags"]["unsupported_number"] for g in drafts),
        "tone_drift_rate": q,
        "first_try_schema_pass_rate": (sum(c["first_try_schema_pass"] for c in responded) / len(responded)
                                       if responded else float("nan")),
        "p95_latency_s": float(np.percentile(latencies, 95)) if latencies else float("nan"),
        "ops_gate_block_rate": p,
        "mean_generation_cost_usd": gen_cost,
        "mean_tone_cost_usd": tone_cost,
        "cost_per_100_usd": cost_per_100(gen_cost, tone_cost, p, q, prereg["decision_rule"]["cost_model"]),
    }
    gates = prereg["decision_rule"]["gates"]
    checks = {
        "G1_unsupported_number": m["unsupported_number_rate"] <= gates["unsupported_number_rate_max"],
        "G2_tone_drift": m["tone_drift_rate"] <= gates["tone_drift_rate_max"],
        "G3_first_try_schema": m["first_try_schema_pass_rate"] >= gates["first_try_schema_pass_min"],
        "G4_p95_latency": m["p95_latency_s"] <= gates["p95_latency_s_max"],
    }
    m["gates"] = checks  # NaN 비교는 False -> 측정 못 한 게이트는 통과로 치지 않는다
    m["passes_gates"] = all(checks.values())
    return m


def paired_bootstrap_diff(a: Dict[str, Optional[bool]], b: Dict[str, Optional[bool]], n: int, seed: int,
                          level: float) -> dict:
    """같은 클러스터에서 두 후보가 모두 라벨된 경우만 짝지어 rate_a − rate_b의 부트스트랩 CI."""
    items = sorted(i for i in set(a) & set(b) if a[i] is not None and b[i] is not None)
    if not items:
        return {"n_pairs": 0, "diff": float("nan"), "ci": [float("nan"), float("nan")]}
    d = np.array([float(a[i]) - float(b[i]) for i in items])
    diffs = cal.bootstrap_counts(len(items), n, seed) @ d / len(items)
    return {"n_pairs": len(items), "diff": float(d.mean()), "ci": cal.percentile_ci(diffs.tolist(), level)}


def decide(metrics: Dict[str, dict], pubs: Dict[str, Dict[str, Optional[bool]]], reliability: dict,
           prereg: dict) -> dict:
    rule = prereg["decision_rule"]
    survivors = sorted(c for c, m in metrics.items() if m["passes_gates"])
    if not survivors:
        return {"winner": None, "action": "keep_incumbent",
                "reason": "모든 후보가 게이트에서 탈락 - 모델을 바꾸지 않고 프롬프트를 개선한다",
                "failed_gates": {c: [g for g, ok in m["gates"].items() if not ok] for c, m in metrics.items()}}

    cheapest = min(survivors, key=lambda c: (metrics[c]["cost_per_100_usd"], c))
    min_k = prereg["human_reliability"]["min_publishable_kappa"]
    rel = reliability.get("publishable", {})
    k = rel.get("kappa", float("nan"))
    # 재라벨이 아직 없으면(n=0) 신뢰도를 "측정 전"으로 두고 1차 지표로 잠정 결정한다.
    # 재라벨 표본이 한 범주뿐이면 κ가 정의되지 않는다(NaN) - "κ < 기준"이 관찰된 것이 아니므로
    # 대체 규칙을 발동하지 않고 측정 불가로 보고한다(ADR 0009 Addendum A2).
    # 재라벨을 했는데 κ가 기준 미달이면 사전 등록대로 1차 지표를 버린다.
    if rel.get("n", 0) > 0 and not math.isnan(k) and k < min_k:
        return {"winner": cheapest, "action": "adopt", "survivors": survivors,
                "reason": f"사람 라벨 신뢰도 부족(intra-rater κ={k:.3f} < {min_k}) - 1차 지표를 쓰지 않고 "
                          "게이트 통과 후보 중 최저 비용",
                "primary_metric_reliable": False}

    sup = rule["superiority"]

    def rate(c):
        r = metrics[c]["publishable_rate"]
        return -math.inf if math.isnan(r) else r

    top = max(survivors, key=lambda c: (rate(c), -metrics[c]["cost_per_100_usd"]))
    comparisons, ties = {}, [top]
    for c in survivors:
        if c == top:
            continue
        cmp_ = paired_bootstrap_diff(pubs[top], pubs[c], int(sup["n_resamples"]), int(sup["seed"]),
                                     float(sup["ci_level"]))
        comparisons[c] = cmp_
        lo, hi = cmp_["ci"]
        if math.isnan(lo) or lo <= 0 <= hi:
            ties.append(c)
    winner = top if len(ties) == 1 else min(ties, key=lambda c: (metrics[c]["cost_per_100_usd"], c))
    return {
        "winner": winner, "action": "adopt", "survivors": survivors, "top_publishable": top,
        "tie_set": sorted(ties), "comparisons_vs_top": comparisons,
        "primary_metric_reliable": True if rel.get("n", 0) > 0 and not math.isnan(k) else None,
        "reason": ("발행률 1위가 나머지와 구별됨" if len(ties) == 1
                   else "발행률 차이가 CI로 구별되지 않음 - 동률 집합에서 100건당 비용 최저"),
    }


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------

def _locate(claim: str, draft: dict) -> Optional[Tuple[str, int, int]]:
    for f in FIELDS:
        pos = (draft.get(f) or "").find(claim.strip())
        if claim.strip() and pos >= 0:
            return f, pos, pos + len(claim.strip())
    return None


def claim_precision(judgments: Sequence[dict], gens_by_key: dict, human: dict) -> dict:
    """judge가 든 근거 없는 주장 중 사람이 표시한 사실 오류 구간과 겹치는 비율."""
    hit = located = unlocated = 0
    for j in judgments:
        lab = human.get((j["candidate"], j["item_id"]))
        draft = (gens_by_key.get((j["candidate"], j["item_id"])) or {}).get("draft")
        if lab is None or not draft:
            continue
        for claim in j["result"].get("unsupported_claims") or []:
            loc = _locate(claim.get("claim", ""), draft)
            if loc is None:
                unlocated += 1
                continue
            located += 1
            f, s, e = loc
            if any(err["field"] == f and err["start"] < e and s < err["end"] for err in lab["fact_errors"]):
                hit += 1
    return {"precision": hit / located if located else float("nan"), "n_located": located,
            "n_unlocated": unlocated}


def judge_metrics(judgments: Sequence[dict], gens: Sequence[dict], human: dict, prereg: dict) -> Dict[str, dict]:
    sel = prereg["judge_selection"]
    gens_by_key = {(g["candidate"], g["item_id"]): g for g in gens}
    out = {}
    for name in sorted({j["judge"] for j in judgments}):
        js = [j for j in judgments if j["judge"] == name and (j["candidate"], j["item_id"]) in human]
        if not js:
            continue
        version = js[0]["judge_version"]
        hum = [bool(human[(j["candidate"], j["item_id"])]["publishable"]) for j in js]
        styles = [human[(j["candidate"], j["item_id"])]["style"] for j in js]
        recs = [j["result"] for j in js]
        rules = cal.v2_rule_grid() if version == "v2" else cal.v1_rule_grid()
        sel_out = cal.two_fold_select(recs, hum, [j["item_id"] for j in js], rules, seed=int(sel["fold_seed"]),
                                      k=int(sel["folds"]))
        if version == "v2":
            scores = [(_mean(r["criteria"].values()) if r.get("criteria") else float("nan")) for r in recs]
        else:
            scores = [float(r.get("score") or 0) for r in recs]
        pairs = [(s, h) for s, h in zip(scores, styles) if not math.isnan(s)]
        out[name] = {
            "family": js[0]["judge_family"], "version": version, "model": js[0]["judge_model"], "n": len(js),
            "oof_kappa": sel_out["oof_kappa"], "folds": sel_out["folds"], "rule_full_fit": sel_out["full_fit"],
            "spearman_vs_human_style": cal.spearman(*zip(*pairs)) if len(pairs) > 2 else float("nan"),
            "claim_precision": claim_precision(js, gens_by_key, human) if version == "v2" else None,
            "judge_call_failures": sum(1 for r in recs if version == "v2" and not r.get("criteria")),
            "cost_usd": sum(j["cost_usd"] for j in js),
        }
    return out


def select_judge(jm: Dict[str, dict], generator_family: str, prereg: dict) -> dict:
    sel = prereg["judge_selection"]
    eligible = {n: m for n, m in jm.items()
                if not (sel.get("require_cross_family", True) and m["family"] == generator_family)
                and not math.isnan(m["oof_kappa"])}
    if not eligible:
        return {"judge": None, "mode": "shadow", "reason": "다른 계열 judge의 OOF κ를 계산할 수 없음"}
    best = max(eligible, key=lambda n: (eligible[n]["oof_kappa"], -eligible[n]["cost_usd"]))
    k = eligible[best]["oof_kappa"]
    gate = k >= float(sel["min_oof_kappa"])
    return {"judge": best, "oof_kappa": k, "mode": "enforce" if gate else "shadow",
            "threshold": eligible[best]["rule_full_fit"]["params"] if gate else None,
            "reason": (f"OOF κ {k:.3f} ≥ {sel['min_oof_kappa']}" if gate else
                       f"최고 OOF κ {k:.3f} < {sel['min_oof_kappa']} - judge는 기록만(shadow), 게이트는 결정론적 검사기")}


def self_preference(judgments: Sequence[dict], human: dict, generator_families: set, prereg: dict) -> dict:
    rows = []
    for j in judgments:
        lab = human.get((j["candidate"], j["item_id"]))
        crit = j["result"].get("criteria")
        if j["judge_version"] != "v2" or lab is None or not crit:
            continue
        rows.append({"cluster": j["item_id"], "generator_family": j["generator_family"], "judge": j["judge"],
                     "judge_family": j["judge_family"], "judge_score": _mean(crit.values()),
                     "human_score": float(lab["publishable"])})
    refs = sorted({r["judge"] for r in rows if r["judge_family"] not in generator_families})
    if not refs:
        return {"reference_judge": None, "estimates": {}, "note": "생성기와 다른 계열의 기준 judge가 없음"}
    return {"reference_judge": refs[0],
            "human_score": "publishable(0/1)",
            "estimates": cal.self_preference_did(rows, reference_judge=refs[0],
                                                 n_boot=int(prereg["decision_rule"]["superiority"]["n_resamples"]),
                                                 seed=int(prereg["decision_rule"]["superiority"]["seed"]))}


# ---------------------------------------------------------------------------
# 사람 라벨 신뢰도 / ClusterEvaluator
# ---------------------------------------------------------------------------

def human_reliability(labels: dict) -> dict:
    o1, o2 = labels["output"][1], labels["output"][2]
    c1, c2 = labels["cluster"][1], labels["cluster"][2]
    return {
        "publishable": cal.intra_rater_kappa({k: v["publishable"] for k, v in o1.items()},
                                             {k: v["publishable"] for k, v in o2.items()}),
        "tone_drift": cal.intra_rater_kappa({k: v["tone_drift"] for k, v in o1.items()},
                                            {k: v["tone_drift"] for k, v in o2.items()}),
        "style_linear_weighted": _weighted(o1, o2),
        "single_event": cal.intra_rater_kappa({k: v["single_event"] for k, v in c1.items()},
                                              {k: v["single_event"] for k, v in c2.items()}),
    }


def _weighted(o1, o2) -> dict:
    keys = sorted(set(o1) & set(o2))
    if not keys:
        return {"n": 0, "kappa": float("nan")}
    return {"n": len(keys), "kappa": cal.cohen_kappa([o1[k]["style"] for k in keys], [o2[k]["style"] for k in keys],
                                                     weights="linear")}


def cluster_evaluator_analysis(cluster_evals: Sequence[dict], cluster_labels: dict, prereg: dict) -> dict:
    cc = prereg["cluster_confidence"]
    out = {}
    for tag in sorted({f"{r['evaluator']['provider']}/{r['evaluator']['model']}" for r in cluster_evals}):
        rows = [r for r in cluster_evals
                if f"{r['evaluator']['provider']}/{r['evaluator']['model']}" == tag and r["item_id"] in cluster_labels]
        if not rows:
            continue
        out[tag] = cal.cluster_confidence_analysis(
            [r["result"]["decision"] for r in rows], [r["result"].get("confidence") or 0.0 for r in rows],
            [cluster_labels[r["item_id"]]["single_event"] for r in rows], [r["item_id"] for r in rows],
            seed=int(cc["fold_seed"]), min_auc=float(cc["min_auc"]), min_gain=float(cc["min_balanced_accuracy_gain"]),
            k=int(cc["folds"]))
    return out


# ---------------------------------------------------------------------------
# 전체
# ---------------------------------------------------------------------------

def analyze(prereg: dict, generations: List[dict], labels: dict, blind_key: dict,
            judgments: Sequence[dict] = (), cluster_evals: Sequence[dict] = ()) -> dict:
    human = human_output_labels(labels, blind_key)
    cluster_labels = labels["cluster"][1]
    cands = [c["name"] for c in prereg["candidates"]]
    metrics = {c: candidate_metrics(c, generations, human, cluster_labels, prereg) for c in cands}
    pubs = {c: publishable_by_item(c, generations, human) for c in cands}
    reliability = human_reliability(labels)
    decision = decide(metrics, pubs, reliability, prereg)
    decision["provisional"] = (not all(m["labels_complete"] for m in metrics.values())
                               or reliability["publishable"]["n"] == 0
                               or math.isnan(reliability["publishable"]["kappa"]))

    fam_of = {c["name"]: c["generator"]["provider"] for c in prereg["candidates"]}
    # 승자가 없으면 현재 기본 생성기(사전 등록의 첫 후보 = incumbent) 계열 기준으로 judge를 고른다
    gen_family = fam_of.get(decision.get("winner")) or prereg["candidates"][0]["generator"]["provider"]
    jm = judge_metrics(judgments, generations, human, prereg)
    return {
        "prereg_id": prereg.get("id"),
        "candidates": metrics,
        "decision": decision,
        "human_reliability": reliability,
        "judges": jm,
        "judge_selection": select_judge(jm, gen_family, prereg) if jm else None,
        "self_preference": self_preference(judgments, human, set(fam_of.values()), prereg) if judgments else None,
        "cluster_evaluator": cluster_evaluator_analysis(cluster_evals, cluster_labels, prereg),
    }


def _fmt(x) -> str:
    if isinstance(x, float):
        return "NaN" if math.isnan(x) else f"{x:.3f}"
    return str(x)


def summary_markdown(report: dict) -> str:
    lines = [f"# bake-off 분석 ({report['prereg_id']})", "", "| 후보 | 발행률 | n | G1 | G2 | G3 | G4 p95(s) | 100건당 $ | 게이트 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for c, m in report["candidates"].items():
        lines.append(f"| {c} | {_fmt(m['publishable_rate'])} | {m['n_labeled']}/{m['n_rows']} | "
                     f"{_fmt(m['unsupported_number_rate'])} | {_fmt(m['tone_drift_rate'])} | "
                     f"{_fmt(m['first_try_schema_pass_rate'])} | {_fmt(m['p95_latency_s'])} | "
                     f"{_fmt(m['cost_per_100_usd'])} | {'통과' if m['passes_gates'] else '탈락'} |")
    d = report["decision"]
    lines += ["", f"**결정**: {d.get('winner') or '변경 없음'} - {d['reason']}"
              + (" (라벨 미완료 또는 재라벨 전: 잠정)" if d.get("provisional") else "")]
    js = report.get("judge_selection")
    if js:
        lines.append(f"**judge**: {js.get('judge')} ({js['mode']}) - {js['reason']}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    from evaluation.llm.bakeoff import DEFAULT_PREREG, LABELS_DIR, RUNS_DIR, JsonlStore, load_prereg
    from evaluation.llm.labels import load_labels

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    parser.add_argument("--prereg", type=Path, default=DEFAULT_PREREG)
    args = parser.parse_args(argv)
    prereg = load_prereg(args.prereg)
    run_dir = RUNS_DIR / args.run
    report = analyze(
        prereg,
        JsonlStore(run_dir / "generations.jsonl").rows(),
        load_labels(LABELS_DIR / args.run),
        json.loads((run_dir / "blind_key.json").read_text(encoding="utf-8")),
        judgments=JsonlStore(run_dir / "judgments.jsonl").rows(),
        cluster_evals=JsonlStore(run_dir / "cluster_evals.jsonl").rows(),
    )
    (run_dir / "analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                                           encoding="utf-8")
    print(summary_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
