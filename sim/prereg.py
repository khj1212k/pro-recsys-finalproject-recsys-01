"""Pre-registered checks of ADR 0019 over a metric-validity grid (sim.experiments output).

    python -m sim.prereg --runs-dir out/sim_grid --out reports/sim/grid_v1

P checks hold by construction of the toy policies and are judged on every seed
(a failure is a bug); H checks are directional hypotheses about metric
sensitivity, judged on the 3-seed mean (a failure is a metric weakness);
X items are exploratory and only reported. The definitions mirror the ADR text
and must not be changed after seeing results - add a new version instead.
"""

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

HEADER = "[SIM] 시스템 반응 지표만, 정확도 무주장 (ADR 0019)"
OTHERS = ("static_batch_fallback", "reactive", "reactive_explore", "random")
REPORT_METRICS = (
    ("cold_start", "first_view_coverage"),
    ("cold_start", "first_view_onboarding_category_share"),
    ("reactivity", "after_click_jaccard_mean"),
    ("reactivity", "similar_share_after_click"),
    ("reactivity", "similar_share_lift"),
    ("drift", "adapted_rate"),
    ("drift", "requests_to_adapt_median"),
    ("serving", "empty_rate"),
    ("serving", "fallback_rate"),
    ("engagement", "ctr_top_k"),
)


@dataclass
class Check:
    id: str
    kind: str  # "P" | "H" | "X"
    catalog: str
    preset: str
    claim: str
    passed: Optional[bool]  # None for X items and undeterminable checks
    values: Dict[str, object]


class Grid:
    """metrics[(catalog, preset, policy)] -> list of per-seed metric dicts (seed order)."""

    def __init__(self, reports: Sequence[dict]):
        self.runs: Dict[Tuple[str, str, str], List[Tuple[int, dict]]] = defaultdict(list)
        for rep in reports:
            a = rep["args"]
            self.runs[(a["catalog"], a["preset"], a["policy"])].append((int(a["seed"]), rep["metrics"]))
        for runs in self.runs.values():
            runs.sort(key=lambda r: r[0])

    def cells(self) -> List[Tuple[str, str]]:
        return sorted({(c, p) for c, p, _ in self.runs})

    def seeds(self, cat: str, preset: str, policy: str, metric: str) -> List[Optional[float]]:
        section, name = metric.split(".")
        return [m.get(section, {}).get(name) for _, m in self.runs.get((cat, preset, policy), [])]

    def mean(self, cat: str, preset: str, policy: str, metric: str) -> Optional[float]:
        vals = self.seeds(cat, preset, policy, metric)
        if not vals or any(v is None for v in vals):
            return None
        return float(np.mean(vals))


def _all(vals: List[Optional[float]], pred) -> bool:
    return bool(vals) and all(v is not None and pred(v) for v in vals)


def _gt(a: Optional[float], b: Optional[float]) -> Optional[bool]:
    return None if a is None or b is None else a > b


def _and(*xs: Optional[bool]) -> Optional[bool]:
    if any(x is False for x in xs):
        return False
    return None if any(x is None for x in xs) else True


def checks_for_cell(g: Grid, cat: str, preset: str) -> List[Check]:
    S = lambda pol, met: g.seeds(cat, preset, pol, met)  # noqa: E731
    M = lambda pol, met: g.mean(cat, preset, pol, met)  # noqa: E731
    out: List[Check] = []

    def add(cid, claim, passed, **values):
        out.append(Check(cid, cid[0], cat, preset, claim, passed, values))

    cov = "cold_start.first_view_coverage"
    add("P1", "first_view_coverage: static_batch = 0, 나머지 = 1",
        _all(S("static_batch", cov), lambda v: v == 0) and all(_all(S(p, cov), lambda v: v == 1) for p in OTHERS),
        **{p: S(p, cov) for p in ("static_batch",) + OTHERS})

    emp, fb = "serving.empty_rate", "serving.fallback_rate"
    sb_ok = _all(S("static_batch", emp), lambda v: v > 0) and all(
        e is not None and f is not None and abs(e - f) < 1e-12 for e, f in zip(S("static_batch", emp), S("static_batch", fb)))
    sbf_ok = _all(S("static_batch_fallback", fb), lambda v: v > 0) and _all(S("static_batch_fallback", emp),
                                                                            lambda v: v == 0)
    rest_ok = all(_all(S(p, emp), lambda v: v == 0) and _all(S(p, fb), lambda v: v == 0)
                  for p in ("reactive", "reactive_explore", "random"))
    add("P2", "빈 응답·폴백률이 정책 설계와 일치", sb_ok and sbf_ok and rest_ok,
        **{p: {"empty_rate": S(p, emp), "fallback_rate": S(p, fb)} for p in ("static_batch",) + OTHERS})

    err, ack = "errors.error_rate", "engagement.click_ack_rate"
    pols = ("static_batch",) + OTHERS
    add("P3", "모든 실행에서 error_rate = 0, click_ack_rate = 1",
        all(_all(S(p, err), lambda v: v == 0) and _all(S(p, ack), lambda v: v == 1) for p in pols),
        **{p: {"error_rate": S(p, err), "click_ack_rate": S(p, ack)} for p in pols})

    jac, lift = "reactivity.after_click_jaccard_mean", "reactivity.similar_share_lift"
    add("P4", "static_batch: 클릭 직후 Jaccard = 1, similar_share_lift = 0",
        _all(S("static_batch", jac), lambda v: v == 1) and _all(S("static_batch", lift), lambda v: abs(v) < 1e-12),
        jaccard=S("static_batch", jac), lift=S("static_batch", lift))
    add("P5", "reactive: 클릭 직후 Jaccard < 1, similar_share_lift > 0",
        _all(S("reactive", jac), lambda v: v < 1) and _all(S("reactive", lift), lambda v: v > 0),
        jaccard=S("reactive", jac), lift=S("reactive", lift))

    ob = "cold_start.first_view_onboarding_category_share"
    add("H1", "첫 응답 온보딩 카테고리 비율: reactive > static_batch_fallback, reactive > random",
        _and(_gt(M("reactive", ob), M("static_batch_fallback", ob)), _gt(M("reactive", ob), M("random", ob))),
        **{p: M(p, ob) for p in ("reactive", "static_batch_fallback", "random")})
    add("H2", "클릭 직후 Jaccard: random < reactive_explore < reactive",
        _and(_gt(M("reactive_explore", jac), M("random", jac)), _gt(M("reactive", jac), M("reactive_explore", jac))),
        **{p: M(p, jac) for p in ("random", "reactive_explore", "reactive")})
    after = "reactivity.similar_share_after_click"
    add("H3", "클릭 후 유사 아이템 비율: reactive > reactive_explore",
        _gt(M("reactive", after), M("reactive_explore", after)),
        **{p: M(p, after) for p in ("reactive", "reactive_explore")})
    add("H4", "similar_share_lift: reactive > random", _gt(M("reactive", lift), M("random", lift)),
        **{p: M(p, lift) for p in ("reactive", "random")})

    ad, rta = "drift.adapted_rate", "drift.requests_to_adapt_median"
    r_ad, s_ad, r_rta, s_rta = M("reactive", ad), M("static_batch", ad), M("reactive", rta), M("static_batch", rta)
    add("H5", "drift: reactive adapted_rate ≥ static_batch, requests_to_adapt_median ≤ static_batch",
        _and(None if r_ad is None or s_ad is None else r_ad >= s_ad,
             None if r_rta is None or s_rta is None else r_rta <= s_rta),
        adapted_rate={"reactive": r_ad, "static_batch": s_ad},
        requests_to_adapt_median={"reactive": r_rta, "static_batch": s_rta})

    ctr = "engagement.ctr_top_k"
    per_seed = [None if a is None or b is None else a > b for a, b in zip(S("reactive", ctr), S("random", ctr))]
    add("H6", "ctr_top_k: reactive > random (평균과 시드별)",
        _and(_gt(M("reactive", ctr), M("random", ctr)), *per_seed),
        reactive=S("reactive", ctr), random=S("random", ctr))
    rnd = M("random", ctr)
    add("H7", "random의 ctr_top_k가 [1%, 3%] 안", None if rnd is None else 0.01 <= rnd <= 0.03, random=rnd)

    add("X1", "random의 drift 지표 = 임계값 지표의 우연 수준", None,
        **{p: {"adapted_rate": M(p, ad), "requests_to_adapt_median": M(p, rta)}
           for p in ("random", "static_batch", "reactive")})
    add("X2", "static_batch 대비 reactive의 ctr_top_k", None, **{p: M(p, ctr) for p in ("static_batch", "reactive")})
    return out


def structural_bias_check(g: Grid, cat: str) -> Optional[Check]:
    ctr = "engagement.ctr_top_k"
    ratios = {}
    for preset in ("default", "category_only"):
        r, b = g.mean(cat, preset, "reactive", ctr), g.mean(cat, preset, "random", ctr)
        ratios[preset] = None if r is None or not b else r / b
    if not all(k in {p for c, p in g.cells() if c == cat} for k in ratios):
        return None
    passed = None if None in ratios.values() else ratios["default"] > ratios["category_only"]
    return Check("H8", "H", cat, "default vs category_only",
                 "ctr(reactive)/ctr(random): default > category_only", passed, ratios)


def run_checks(reports: Sequence[dict]) -> List[Check]:
    g = Grid(reports)
    out: List[Check] = []
    for cat, preset in g.cells():
        out.extend(checks_for_cell(g, cat, preset))
    for cat in sorted({c for c, _ in g.cells()}):
        h8 = structural_bias_check(g, cat)
        if h8 is not None:
            out.append(h8)
    return out


def metric_table(reports: Sequence[dict]) -> Dict[str, Dict[str, dict]]:
    g = Grid(reports)
    table: Dict[str, Dict[str, dict]] = {}
    for (cat, preset, policy) in sorted(g.runs):
        row = {}
        for section, name in REPORT_METRICS:
            vals = [v for v in g.seeds(cat, preset, policy, f"{section}.{name}") if v is not None]
            row[f"{section}.{name}"] = ({"mean": float(np.mean(vals)), "min": float(np.min(vals)),
                                        "max": float(np.max(vals)), "n": len(vals)} if vals else None)
        table[f"{cat}/{preset}/{policy}"] = row
    return table


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_fmt(x)}" for k, x in v.items()) + "}"
    return str(v)


def to_markdown(checks: Sequence[Check], table: Dict[str, Dict[str, dict]], meta: dict) -> str:
    verdict = {True: "통과", False: "**위반**", None: "-"}
    lines = [f"# 지표 타당성 격자 v1", "", f"> {HEADER}", "",
             f"- 실행: {meta.get('runs', '?')}건, 시드 {meta.get('seeds', '?')}, 커밋 `{meta.get('git_sha', '?')}`",
             "- 판정 기준: docs/adr/0019 \"사전 등록\" 절 (P: 시드별 전부, H: 3시드 평균, X: 보고만)", "",
             "## 사전 등록 판정", "", "| ID | 카탈로그 | 프리셋 | 주장 | 판정 | 값 |", "|---|---|---|---|---|---|"]
    for c in checks:
        lines.append(f"| {c.id} | {c.catalog} | {c.preset} | {c.claim} | {verdict[c.passed]} | {_fmt(c.values)} |")
    lines += ["", "## 정책별 핵심 지표 (3시드 평균 [최소, 최대])", ""]
    cols = [f"{s}.{n}" for s, n in REPORT_METRICS]
    lines.append("| 카탈로그/프리셋/정책 | " + " | ".join(n.split(".")[1] for n in cols) + " |")
    lines.append("|---" * (len(cols) + 1) + "|")
    for key, row in table.items():
        cells = []
        for col in cols:
            d = row[col]
            cells.append("-" if d is None else f"{d['mean']:.3g} [{d['min']:.3g}, {d['max']:.3g}]")
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def load_reports(runs_dirs: Sequence[Path]) -> List[dict]:
    reps = []
    for d in runs_dirs:
        for p in sorted(Path(d).glob("*__*__*__s*.json")):
            reps.append(json.loads(p.read_text(encoding="utf-8")))
    return reps


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs-dir", type=Path, nargs="+", required=True)
    p.add_argument("--out", type=Path, required=True, help="output path prefix (writes .json and .md)")
    p.add_argument("--git-sha", default="?")
    args = p.parse_args(argv)
    reports = load_reports(args.runs_dir)
    checks = run_checks(reports)
    table = metric_table(reports)
    seeds = sorted({int(r["args"]["seed"]) for r in reports})
    meta = {"runs": len(reports), "seeds": seeds, "git_sha": args.git_sha,
            "click_models": sorted({(r["args"]["catalog"], r["args"]["preset"], round(r["click_model"]["bias"], 4))
                                    for r in reports})}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"header": HEADER, "meta": meta, "checks": [asdict(c) for c in checks], "metrics": table}
    Path(f"{args.out}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(f"{args.out}.md").write_text(to_markdown(checks, table, meta), encoding="utf-8")
    n_fail = sum(1 for c in checks if c.passed is False)
    print(f"{len(checks)} checks, {n_fail} violations -> {args.out}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
