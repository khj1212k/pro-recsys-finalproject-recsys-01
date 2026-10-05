"""콜드 regime 사슬(v1.2)의 기계 판정: 리포트 JSON + 사전 등록 yaml -> 규칙별 판정.

규칙은 ADR 0013 "A2 사전 등록" A2.5에 결과 전에 고정했고, 임계값은 preregistration/cold-v1.2.yaml에서만 읽는다.
이 모듈에는 숫자 임계가 없다. 사람이 표를 읽어 판정하지 않게 하려는 것이다.

공통 규칙:
- 비교에 필요한 수치가 JSON에 없으면 "실패"가 아니라 status="unmeasured"다.
- 부등호는 등록한 그대로 엄격하다(예: CI 하한 > 0.005에서 하한이 정확히 0.005면 통과가 아니다).
- 함수는 입력을 바꾸지 않는다.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

import yaml

PREREG_PATH = Path(__file__).resolve().parent / "preregistration" / "cold-v1.2.yaml"
EVIDENCE_GRADE = "preregistered-run"
DEMO_GRADE = "demo, not evidence"


def load_prereg(path: Optional[Path] = None) -> dict:
    return yaml.safe_load(Path(path or PREREG_PATH).read_text(encoding="utf-8"))


def prereg_sha256(path: Optional[Path] = None) -> str:
    return hashlib.sha256(Path(path or PREREG_PATH).read_bytes()).hexdigest()


def prereg_commit(path: Optional[Path] = None) -> Optional[str]:
    """사전 등록 yaml을 처음 담은 커밋의 SHA. yaml과 같은 커밋에 넣을 수 없어 옆 파일(.commit)에 따로 적어 둔다.
    리포트 머리말에 찍히고, 테스트가 그 커밋의 yaml이 지금 파일과 바이트 단위로 같은지 본다."""
    f = Path(path or PREREG_PATH).with_suffix(".commit")
    if not f.exists():
        return None
    tokens = f.read_text(encoding="utf-8").split()
    return tokens[0] if tokens else None


def cell_key(traffic: str, pool: Any) -> str:
    return f"{traffic}|{pool}"


def _cell(d: dict, traffic: str, pool: Any) -> dict:
    return d.get("e1", {}).get("cells", {}).get(cell_key(traffic, pool), {})


def _diff(container: dict, key: str, metric: str) -> Optional[dict]:
    """{"diff", "lo", "hi"} 또는 None(없음)."""
    v = container.get(key, {}).get(metric) if isinstance(container.get(key), dict) else None
    if not v or v.get("diff") is None or v.get("ci95") is None:
        return None
    return {"diff": float(v["diff"]), "lo": float(v["ci95"][0]), "hi": float(v["ci95"][1])}


def reproduction_gate(d: dict, prereg: dict) -> dict:
    g = prereg["reproduction_gate"]
    lo, hi = g["ndcg10_seed_mean_within"]
    m = _cell(d, g["cell"]["traffic"], g["cell"]["pool"]).get("methods", {}).get(g["arm"], {})
    mean = m.get(prereg["statistics"]["metric"], {}).get("mean")
    out = {"arm": g["arm"], "within": [lo, hi], "mean": mean}
    if mean is None:
        return {**out, "status": "unmeasured"}
    return {**out, "status": "pass" if lo <= mean <= hi else "fail"}


def e1_verdict(d: dict, prereg: dict) -> dict:
    metric = prereg["statistics"]["metric"]
    ra, rb = prereg["e1"]["a"], prereg["e1"]["b"]
    a_key = f"{ra['compare'][0]}-vs-{ra['compare'][1]}"
    da = _diff(_cell(d, **ra["cell"]).get("diffs", {}), a_key, metric)
    if da is None:
        a = {"status": "unmeasured", "comparison": a_key}
    else:
        ok = da["lo"] > ra["pass_if_ci_lo_gt"]
        a = {"status": "measured", "comparison": a_key, **da, "pass": ok,
             "decision": ra["on_pass"] if ok else ra["on_fail"]}

    diffs = _cell(d, **rb["cell"]).get("diffs", {})
    per = {m: _diff(diffs, f"{m}-vs-{rb['baseline']}", metric) for m in rb["models"]}
    if any(v is None for v in per.values()):
        b = {"status": "unmeasured", "comparisons": {m: v for m, v in per.items()}}
    else:
        beats = {m: bool(v["lo"] > rb["beat_if_ci_lo_gt"] and v["diff"] >= rb["beat_if_diff_ge"])
                 for m, v in per.items()}
        any_beats = any(beats.values())
        b = {"status": "measured", "comparisons": per, "beats": beats, "any_beats": any_beats,
             "decision": rb["on_any_beats"] if any_beats else rb["on_none_beats"]}
    return {"a": a, "b": b}


def e2_grid(prereg: dict) -> list:
    return list(prereg["conditions"]["truncate_ks"]) + ["all"]


def e2_kstar(d: dict, prereg: dict) -> dict:
    r = prereg["e2"]
    metric = prereg["statistics"]["metric"]
    grid = e2_grid(prereg)
    cells = d.get("e2", {}).get("p2", {})
    key = f"{r['model']}-vs-{r['baseline']}"
    per: dict = {}
    for k in grid:
        c = cells.get(f"{r['traffic']}|k{k}", {})
        per[k] = _diff(c.get("diffs", {}), key, metric)
    if any(v is None for v in per.values()):
        return {"status": "unmeasured", "comparison": key, "missing": [k for k, v in per.items() if v is None]}
    passes = [per[k]["lo"] > r["pass_if_ci_lo_gt"] for k in grid]
    k_star = None
    for i, k in enumerate(grid):
        tail = passes[i:] if r["require_all_larger_k"] else passes[i:i + 1]
        if all(tail):
            k_star = k
            break
    k_gain = None
    for k in grid:
        if k == 0:
            continue
        g = _diff(cells.get(f"{r['traffic']}|k{k}", {}), "gain_vs_k0", metric)
        if g is not None and g["lo"] > r["gain_min_effect"]:
            k_gain = k
            break
    value = k_star if isinstance(k_star, int) else None
    if k_star is None:
        decision = "k* 없음: 격자의 어떤 k에서도 성립하지 않음 — RECSYS_MIN_PERSONAL_EVENTS를 정하지 않는다"
    elif value is None:
        decision = "절단하지 않은 히스토리에서만 성립 — 유한한 k*가 없어 RECSYS_MIN_PERSONAL_EVENTS를 정하지 않는다"
    else:
        decision = f"RECSYS_MIN_PERSONAL_EVENTS = {value}"
    return {"status": "measured", "comparison": key, "per_k": {str(k): {**per[k], "pass": p}
                                                               for k, p in zip(grid, passes)},
            "k_star": k_star, "min_personal_events": value, "k_gain": k_gain, "decision": decision}


def e3_verdict(d: dict, prereg: dict) -> dict:
    r = prereg["e3"]
    key = f"{r['compare'][0]}-vs-{r['compare'][1]}"
    v = _diff(_cell(d, **r["cell"]).get("diffs", {}), key, prereg["statistics"]["metric"])
    if v is None:
        return {"status": "unmeasured", "comparison": key}
    margin = r["non_inferiority_margin"]
    if v["lo"] > margin:
        status, label = "met", "계약 충족"
    elif v["diff"] <= margin:
        status, label = "not_met", "불충족"
    else:
        status, label = "inconclusive", "보류(검정력 부족)"
    return {"status": status, "label": label, "comparison": key, "margin": margin, **v}


def e6_verdict(d: dict, prereg: dict) -> dict:
    r = prereg["e6"]
    metric = prereg["statistics"]["metric"]
    table: dict = {}
    for c in r["cells"]:
        diffs = _cell(d, **c).get("diffs", {})
        for a in r["alphas"]:
            table[(c["traffic"], a)] = _diff(diffs, f"poolneg_shrunk_a{a}-vs-{r['baseline']}", metric)
    comparisons = {f"{t}|a{a}": v for (t, a), v in table.items()}
    if any(v is None for v in table.values()):
        return {"status": "unmeasured", "comparisons": comparisons}
    passing = [[t, a] for (t, a), v in table.items() if v["lo"] > r["pass_if_ci_lo_gt"]]
    adopted = None
    if passing:
        alphas = sorted({a for _, a in passing})
        adopted = max(alphas, key=lambda a: (min(table[(c["traffic"], a)]["diff"] for c in r["cells"]), -a))
    return {"status": "measured", "comparisons": comparisons, "passing": passing, "adopted_alpha": adopted,
            "decision": (f"pop_ctr_shrunk_24h 채택, α={adopted}(잠정값)" if adopted is not None
                         else "pop_ctr_shrunk_24h를 채택하지 않음")}


def e7_verdict(d: dict, prereg: dict) -> dict:
    r = prereg["e7"]
    metric = prereg["statistics"]["metric"]
    sets: dict = {}
    for name, spec in r["sets"].items():
        diffs = _cell(d, **spec["eval_cell"]).get("diffs", {})
        vs_prior = _diff(diffs, f"heuristic_fit_{name}-vs-{r['prior']}", metric)
        vs_base = _diff(diffs, f"heuristic_fit_{name}-vs-{r['scorer_role_baseline']}", metric)
        if vs_prior is None or vs_base is None:
            return {"status": "unmeasured", "missing_set": name}
        sets[name] = {"vs_prior": vs_prior, "vs_baseline": vs_base,
                      "beats_prior": bool(vs_prior["lo"] > r["beats_prior_if_ci_lo_gt"]),
                      "role": "scorer" if vs_base["lo"] > 0 else "tie_breaker"}
    keep_prior = sets["b"]["vs_prior"]["hi"] < r["keep_prior_if_ci_hi_lt"]
    return {"status": "measured", "sets": sets, "serving_default": "prior" if keep_prior else "b",
            "switch_rule": r["switch_rule_for_adr_0014"],
            "decision": ("서빙 기본값은 사전 가중치 유지((b) 세트가 사전값보다 CI로 나쁨)" if keep_prior
                         else "서빙 기본값 = (b) 세트 가중치")}


def _e8_config(d: dict, name: str, prereg: dict) -> Optional[dict]:
    c = d.get("e8", {}).get(name)
    if not c or c.get("union_recall") is None:
        return None
    v = _diff(c.get("two_stage", {}), "paired_vs_full_pool", prereg["statistics"]["metric"])
    if v is None:
        return None
    r = prereg["e8"]
    recall_ok = c["union_recall"] >= r["min_union_recall"]
    loss_ok = v["lo"] > r["two_stage_margin"]
    return {"union_recall": c["union_recall"], "two_stage_vs_full_pool": v, "recall_ok": bool(recall_ok),
            "loss_ok": bool(loss_ok), "ok": bool(recall_ok and loss_ok)}


def e8_verdict(d: dict, prereg: dict) -> dict:
    serving = _e8_config(d, "serving", prereg)
    harness = _e8_config(d, "harness", prereg)
    out = {"serving": serving, "harness": harness}
    if serving is None:
        return {**out, "status": "unmeasured", "decision": "unmeasured"}
    if serving["ok"]:
        return {**out, "status": "measured", "decision": "keep_serving", "label": "서빙 후보 구성 유지"}
    if harness is None:
        return {**out, "status": "unmeasured", "decision": "unmeasured"}
    if harness["ok"]:
        return {**out, "status": "measured", "decision": "switch_to_harness", "label": "서빙을 하네스 구성으로"}
    return {**out, "status": "measured", "decision": "redesign", "label": "후보 구성 재설계 필요"}


def cold_verdicts(d: dict, prereg: Optional[dict] = None) -> dict:
    """모든 규칙의 판정과 미측정 목록. judgeable이 거짓이면 아래 판정은 참고용일 뿐 결정에 쓰지 않는다."""
    prereg = prereg or load_prereg()
    gate = reproduction_gate(d, prereg)
    grade = d.get("meta", {}).get("evidence", {}).get("grade")
    e1 = e1_verdict(d, prereg)
    verdicts = {"e1": e1, "e2": e2_kstar(d, prereg), "e3": e3_verdict(d, prereg), "e6": e6_verdict(d, prereg),
                "e7": e7_verdict(d, prereg), "e8": e8_verdict(d, prereg)}
    unmeasured = []
    for part in ("a", "b"):
        if e1[part]["status"] == "unmeasured":
            unmeasured.append(f"e1{part}")
    unmeasured += [k for k in ("e2", "e3", "e6", "e7", "e8") if verdicts[k]["status"] == "unmeasured"]
    reason = None
    if grade != EVIDENCE_GRADE:
        reason = f"증거 등급이 '{grade}'다 (demo 또는 등록한 인자와 다른 실행)"
    elif gate["status"] != "pass":
        reason = f"재현 게이트 {gate['status']}: {gate['arm']} nDCG@10 seed 평균 {gate['mean']} (허용 {gate['within']})"
    return {"preregistration": prereg["id"], "judgeable": reason is None, "not_judgeable_reason": reason,
            "gate": gate, "verdicts": verdicts, "unmeasured": unmeasured}
