"""E15의 기계 판정과 리포트 표: 리포트 JSON + 사전 등록 yaml -> 판정 (ADR 0013 A3.7).

규칙은 결과 전에 커밋한 preregistration/neural-e15.yaml에서만 읽는다. 이 모듈에는 숫자 임계가 없다.
- 비교에 필요한 수치가 JSON에 없으면 "실패"가 아니라 "unmeasured"다.
- 부등호는 등록한 그대로 엄격하다(하한 > 0.005에서 하한이 정확히 0.005면 통과가 아니다).
- 서빙 비용 게이트는 이 등록에서 재지 않으므로, 통계 조건을 다 통과한 arm의 결과는 pending_serving_cost_gate이고
  shadow 자격 목록은 항상 비어 있다.
- 함수는 입력을 바꾸지 않는다. torch를 쓰지 않는다.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Optional

from .. import cold_verdicts
from ..cold_verdicts import DEMO_GRADE, EVIDENCE_GRADE  # noqa: F401 (재노출)

PREREG_PATH = Path(__file__).resolve().parents[1] / "preregistration" / "neural-e15.yaml"
STATUS_KO = {"pass": "통과", "fail": "실패", "hold": "보류(검정력 부족)", "unmeasured": "미측정"}
OUTCOME_KO = {
    "pending_serving_cost_gate": "통계 조건 충족 — 서빙 비용 게이트 대기(shadow 자격 아님)",
    "not_distinguishable": "구분 불가(seed 평균 Δ의 CI가 0을 포함)",
    "below_min_effect": "효과 미달(결합 하한이 0과 최소 효과 크기 사이)",
    "seed_rule_failed": "seed 규칙 불통과",
    "gate_failed": "게이트 실패", "gate_hold": "게이트 보류(검정력 부족)",
    "unmeasured": "미측정", "invalid_run": "실행 무효",
}


def load_prereg(path: Optional[Path] = None) -> dict:
    return cold_verdicts.load_prereg(path or PREREG_PATH)


def prereg_sha256(path: Optional[Path] = None) -> str:
    return cold_verdicts.prereg_sha256(path or PREREG_PATH)


def prereg_commit(path: Optional[Path] = None) -> Optional[str]:
    return cold_verdicts.prereg_commit(path or PREREG_PATH)


def diff_key(arm: str, reference: str) -> str:
    return f"{arm}-vs-{reference}"


def _entry(task_block: dict, arm: str, reference: str) -> Optional[dict]:
    e = (task_block or {}).get("diffs", {}).get(diff_key(arm, reference))
    return e if e and e.get("diff") is not None and e.get("ci95") else None


def seed_rule(entry: Optional[dict], stats: dict) -> dict:
    """(a) seed마다 쌍체 Δ CI 하한 > 0, (b) 결합 하한 = Δ̄ − sqrt(hw_boot² + (t·sd_Δ/√n)²) > 최소 효과 크기."""
    rule = stats["seed_rule"]
    n = int(rule["n_seeds"])
    per_seed = (entry or {}).get("per_seed") or []
    if entry is None or len(per_seed) != n or any(p.get("ci95") is None for p in per_seed):
        return {"status": "unmeasured", "reason": f"seed {n}개의 쌍체 Δ가 다 있어야 한다(있는 수: {len(per_seed)})"}
    a = all(float(p["ci95"][0]) > rule["per_seed_ci_lo_gt"] for p in per_seed)
    diffs = [float(p["diff"]) for p in per_seed]
    mean = sum(diffs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in diffs) / (n - 1))
    hw = (float(entry["ci95"][1]) - float(entry["ci95"][0])) / 2.0
    lower = float(entry["diff"]) - math.sqrt(hw ** 2 + (rule["t_crit"] * sd / math.sqrt(n)) ** 2)
    b = lower > rule["combined_lo_gt"]
    return {"status": "pass" if (a and b) else "fail", "a_each_seed_lo_gt_0": a, "b_combined_lo_gt_min_effect": b,
            "diff": float(entry["diff"]), "ci95": list(entry["ci95"]), "seed_diffs": diffs,
            "seed_ci_lo": [float(p["ci95"][0]) for p in per_seed], "sd_seed": sd, "hw_boot": hw,
            "combined_lower": lower}


def gate3(entry: Optional[dict], stats: dict) -> dict:
    """비열등 게이트의 3분류: 통과 / 실패 / 보류. 하한은 양측 90% CI(= 단측 95%)의 것이다."""
    if entry is None or entry.get("ci90") is None:
        return {"status": "unmeasured"}
    margin = stats["gate_margin"]
    diff, lo = float(entry["diff"]), float(entry["ci90"][0])
    status = "pass" if lo > margin else ("fail" if diff <= margin else "hold")
    return {"status": status, "diff": diff, "lo90": lo, "margin": margin}


def holm_step_down(entries: dict, stats: dict) -> dict:
    """판정 비교 묶음의 Holm step-down. 남은 비교가 m개일 때 수준 (1 − alpha/m)의 CI 하한 > 최소 효과 크기면 기각한다.

    미측정 비교는 기각되지 않은 채 묶음에 남는다(m이 줄지 않는다). 반환: {이름: 기각 여부}.
    """
    size, min_effect = int(stats["holm"]["family_size"]), stats["min_effect"]
    if len(entries) != size:
        raise ValueError(f"Holm 묶음의 크기는 {size}여야 합니다(받은 수 {len(entries)})")
    rejected: set = set()
    while len(rejected) < size:
        m = str(size - len(rejected))
        new = {name for name, e in entries.items() if name not in rejected and e
               and (e.get("holm_lo") or {}).get(m) is not None and float(e["holm_lo"][m]) > min_effect}
        if not new:
            break
        rejected |= new
    return {name: name in rejected for name in entries}


def validity_status(d: dict, task: str, prereg: dict) -> dict:
    v = (d.get("validity") or {}).get(task) or {}
    out = {name: (v.get(name) or {}).get("status", "unmeasured") for name in prereg["gates"]["validity"]}
    out["status"] = ("fail" if "fail" in out.values() else
                     "unmeasured" if "unmeasured" in out.values() else "pass")
    return out


def _resolve(arm: str, selected: Optional[str]) -> Optional[str]:
    return selected if arm == "sel" else arm


def arm_verdict(d: dict, task: str, arm: str, prereg: dict) -> dict:
    """한 과제·한 판정 arm(sel 또는 D)의 결과. 규칙의 순서는 A3.7 그대로다."""
    stats = prereg["statistics"]
    block = (d.get("tasks") or {}).get(task) or {}
    selected = ((d.get("selection") or {}).get(task) or {}).get("family")
    name = _resolve(arm, selected)
    ref = next(c["reference"] for c in prereg["judged_comparisons"] if c["task"] == task and c["arm"] == arm)
    out = {"task": task, "arm": arm, "model": name, "reference": ref, "validity": validity_status(d, task, prereg)}
    comparison = seed_rule(_entry(block, name, ref) if name else None, stats)
    out["comparison"] = comparison
    cold = prereg["gates"]["cold"]
    gates = {f"cold:{c}": gate3((((block.get("cold") or {}).get(c) or {}).get("diffs") or {}).get(
        diff_key(name, cold["reference"])) if name else None, stats) for c in cold["conditions"]}
    gates["info_control"] = gate3(_entry(block, name, prereg["gates"]["info_control"]["reference"]) if name else None,
                                  stats)
    out["gates"] = gates
    states = [g["status"] for g in gates.values()]
    if out["validity"]["status"] == "fail":
        outcome = "invalid_run"
    elif comparison["status"] == "unmeasured" or out["validity"]["status"] == "unmeasured":
        outcome = "unmeasured"
    elif comparison["status"] == "fail":
        lo, hi = comparison["ci95"]
        if lo <= 0.0 <= hi:
            outcome = "not_distinguishable"
        elif comparison["a_each_seed_lo_gt_0"] and 0.0 < comparison["combined_lower"] <= stats["min_effect"]:
            outcome = "below_min_effect"
        else:
            outcome = "seed_rule_failed"
    elif "fail" in states:
        outcome = "gate_failed"
    elif "hold" in states:
        outcome = "gate_hold"
    elif "unmeasured" in states:
        outcome = "unmeasured"
    else:
        outcome = "pending_serving_cost_gate"
    if outcome not in prereg["outcomes"]:
        raise ValueError(f"등록하지 않은 결과 분류: {outcome}")
    out["outcome"] = outcome
    out["statistical_requirements_met"] = outcome == "pending_serving_cost_gate"
    return out


def neural_verdict(d: dict, prereg: Optional[dict] = None) -> dict:
    """리포트 JSON -> 판정 비교 4개의 결과, 게이트, 서술용 비교, 주장(claim), 미측정 목록."""
    prereg = prereg or load_prereg()
    stats = prereg["statistics"]
    judged = prereg["judged_comparisons"]
    tasks = list(dict.fromkeys(c["task"] for c in judged))
    selection = {t: ((d.get("selection") or {}).get(t) or {}).get("family") for t in tasks}
    by_task = {t: {c["arm"]: arm_verdict(d, t, c["arm"], prereg) for c in judged if c["task"] == t} for t in tasks}

    entries = {}
    for c in judged:
        name = _resolve(c["arm"], selection[c["task"]])
        entries[f"{c['task']}:{c['arm']}"] = _entry((d.get("tasks") or {}).get(c["task"]) or {}, name, c["reference"]) \
            if name else None
    holm = holm_step_down(entries, stats)

    side = {}
    for t in tasks:
        block = (d.get("tasks") or {}).get(t) or {}
        for s in prereg["side_comparisons"]:
            a, r = _resolve(s["arm"], selection[t]), _resolve(s["reference"], selection[t])
            res = seed_rule(_entry(block, a, r) if a and r else None, stats)
            side[f"{t}:{s['arm']}-vs-{s['reference']}"] = {**res, "conclusion": s["on_pass"] if res["status"] == "pass" else None}

    families = set(selection.values())
    same_family = len(families) == 1 and None not in families
    control = prereg["gates"]["info_control"]["reference"]
    neural_ok = same_family and all(
        by_task[t]["sel"]["statistical_requirements_met"] and holm[f"{t}:sel"]
        and side[f"{t}:sel-vs-{control}"]["status"] == "pass" for t in tasks)
    stacking = [t for t in tasks if by_task[t]["D"]["statistical_requirements_met"]]
    mechanical = "neural_arm_both_tasks" if neural_ok else ("stacking_gain" if stacking else "none")
    if mechanical not in prereg["claim"]["values"]:
        raise ValueError(f"등록하지 않은 주장 값: {mechanical}")
    grade = ((d.get("meta") or {}).get("evidence") or {}).get("grade")
    # demo 등급 실행은 판정이 아니므로 어떤 주장도 풀지 않는다. 규칙이 낸 값은 배선 확인용으로만 따로 남긴다.
    claim = mechanical if grade == EVIDENCE_GRADE else "none"

    unmeasured = [f"{t}:{a}" for t in tasks for a, v in by_task[t].items() if v["outcome"] == "unmeasured"]
    unmeasured += [f"{t}:{a}:{g}" for t in tasks for a, v in by_task[t].items() for g, s in v["gates"].items()
                   if s["status"] == "unmeasured" and v["outcome"] != "unmeasured"]
    unmeasured += [k for k, v in side.items() if v["status"] == "unmeasured"]
    return {
        "judged": grade == EVIDENCE_GRADE, "evidence_grade": grade, "selection": selection, "by_task": by_task,
        "holm": holm, "side": side, "claim": claim, "claim_rule_output": mechanical, "claim_tasks_stacking": stacking,
        "serving_cost_gate": prereg["gates"]["serving_cost"]["status"],
        # 서빙 비용 게이트를 재기 전에는 어떤 arm도 shadow 자격을 얻지 못한다(A3.7).
        "shadow_eligible": [],
        "statistical_requirements_met": [f"{t}:{a}" for t in tasks for a, v in by_task[t].items()
                                         if v["statistical_requirements_met"]],
        "unmeasured": unmeasured,
    }


# --- 표 ----------------------------------------------------------------------------------------

def _ci(v: Optional[dict]) -> str:
    return "-" if not v or v.get("mean") is None else f"{v['mean']:.4f} [{v['ci95'][0]:.4f}, {v['ci95'][1]:.4f}]"


def _dci(e: Optional[dict], key: str = "ci95") -> str:
    return "-" if not e or e.get(key) is None else f"{e['diff']:+.4f} [{e[key][0]:+.4f}, {e[key][1]:+.4f}]"


def _table(header: list, rows: list) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def render(d: dict) -> str:
    """리포트 JSON -> 마크다운. 손으로 고치지 않는다."""
    meta, v = d["meta"], d["verdict"]
    ev, pre = meta["evidence"], meta["preregistration"]
    out = ["# EB-NeRD v1.3 — 신경망 사용자 모델 vs LightGBM (E15) 결과 표", ""]
    if ev["grade"] == EVIDENCE_GRADE:
        out.append(f"> 증거 등급: **{ev['grade']}** — 등록한 인자·입력으로 돈 실행이다. 라벨 {meta['label']}"
                   "(덴마크어 공개 벤치마크). 한국어 서비스의 성능이 아니다.")
    else:
        out.append(f"> 증거 등급: **{ev['grade']}** — 이 파일의 어떤 수치도 근거로 쓰지 않는다. 배선 확인용이다.")
        out.append("> 사유: " + "; ".join(ev["reasons"]))
    out += [f"> 사전 등록: `{pre['id']}` (ADR 0013 A3 사전 등록, yaml sha256 `{pre['sha256'][:12]}…`"
            + (f", 커밋 `{pre['commit']}`" if pre.get("commit") else "") + f"). 코드 SHA `{meta['code_sha']}`.",
            "> 값은 nDCG@10 평균 [95% CI](유저 단위 클러스터 부트스트랩), 차이는 같은 노출의 쌍체 차이다. 판정은 "
            "`neural_verdict`가 낸다. 신경망 arm이 받는 정보는 \"A의 정보 + 원 벡터\"다(같은 정보가 아니다).", ""]

    out += ["## 판정", "", "판정 아님(demo 등급) — 아래 표는 배선 확인용이다." if not v["judged"] else
            "판정용 실행이다.", ""]
    rows = []
    for t, arms in v["by_task"].items():
        for a, r in arms.items():
            c = r["comparison"]
            rows.append([t, f"{a}({r['model']}) − {r['reference']}",
                         "-" if c["status"] == "unmeasured" else f"{c['diff']:+.4f} [{c['ci95'][0]:+.4f}, {c['ci95'][1]:+.4f}]",
                         "-" if c["status"] == "unmeasured" else f"{c['combined_lower']:+.4f}",
                         STATUS_KO[c["status"]], STATUS_KO[r["validity"]["status"]],
                         ", ".join(f"{g}={STATUS_KO[s['status']]}" for g, s in r["gates"].items()),
                         OUTCOME_KO[r["outcome"]]])
    out += [_table(["과제", "비교", "Δ [95% CI]", "결합 하한", "seed 규칙", "실행 유효성", "게이트", "결과"], rows), "",
            f"- 주장(claim): `{v['claim']}`. 서빙 비용 게이트: `{v['serving_cost_gate']}` — shadow 자격을 얻은 arm: "
            f"{v['shadow_eligible'] or '없음'}.",
            f"- 선택된 family: {v['selection']}. Holm(4): {v['holm']}.",
            f"- 미측정: {v['unmeasured'] or '없음'}.", ""]
    rows = [[k, STATUS_KO[s["status"]], "-" if s["status"] == "unmeasured" else f"{s['diff']:+.4f}",
             "-" if s["status"] == "unmeasured" else f"{s['combined_lower']:+.4f}", s.get("conclusion") or "-"]
            for k, s in v["side"].items()]
    out += ["## 서술용 비교(같은 seed 규칙, 판정 아님)", "", _table(["비교", "seed 규칙", "Δ", "결합 하한", "기록"], rows), ""]

    for t, block in (d.get("tasks") or {}).items():
        out += [f"## {t} — 판정 표본", ""]
        ref = block.get("arms", {})
        rows = [[a, _ci(s.get("ndcg@10")), s.get("n_seeds", "-"),
                 _dci(block.get("diffs", {}).get(diff_key(a, "A_star"))),
                 _dci(block.get("diffs", {}).get(diff_key(a, "A")))] for a, s in ref.items()]
        out += [_table(["arm", "nDCG@10 [95% CI]", "seed 수", "− A* [95% CI]", "− A [95% CI]"], rows), ""]
        cold = block.get("cold") or {}
        if cold:
            arms = sorted({k.split("-vs-")[0] for c in cold.values() for k in c.get("diffs", {})})
            rows = [[c] + [_dci(cold[c].get("diffs", {}).get(diff_key(a, "A_star")), "ci90") for a in arms] for c in cold]
            out += [f"### {t} — 콜드 조건(같은 조건의 A* 대비 Δ [양측 90% CI])", "", _table(["조건"] + arms, rows), ""]
        sel = (d.get("selection") or {}).get(t)
        if sel:
            out += [f"### {t} — 선택(test 미사용)", "", "```json", json.dumps(sel, ensure_ascii=False, indent=1), "```", ""]
    val = d.get("validity") or {}
    if val:
        out += ["## 실행 유효성 게이트", "", "```json", json.dumps(val, ensure_ascii=False, indent=1), "```", ""]
    out += ["## 한계", "",
            "- test 창(validation)은 v1·v1.1에서 여러 번 보고됐고 A의 피처 구성은 그 결과와 함께 정해졌다(A·A*·A+에 유리한 방향).",
            "- 임베딩한 기사 본문이 노출 시점 판본이라는 보장이 없다. 양성에는 48h·미열람 필터가 없다(v1과 동일).",
            "- 뉴스 인코더는 고정(BGE-M3 투영)이다. 기각이든 통과든 범위는 그 구성에 한정된다.",
            "- 덴마크어 타블로이드의 클릭 로그다. 한국어 서비스에서 어느 쪽이 나은지는 이 결과로 말할 수 없다.", ""]
    if meta.get("compute"):
        out += ["## 계산 자원", "", "```json", json.dumps(meta["compute"], ensure_ascii=False, indent=1), "```", ""]
    return "\n".join(out)


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m evaluation.recsys.ebnerd.neural.report <report.json>", file=sys.stderr)
        return 64
    d = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    d["verdict"] = neural_verdict(d)
    print(render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
