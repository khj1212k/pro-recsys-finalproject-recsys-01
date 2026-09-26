"""run_ebnerd.py가 쓴 JSON에서 리포트용 마크다운 표를 만든다(수치 전사 실수 방지).

    .venv/bin/python -m evaluation.recsys.ebnerd.make_report reports/recsys/ebnerd_v1.json > /tmp/tables.md

해설 문단은 reports/recsys/ebnerd_v1.md에 사람이 쓰고, 표는 이 출력으로 교체한다.
"""
from __future__ import annotations

import json
import sys
from typing import Iterable, Sequence

P1_COLS = ("auc", "mrr", "ndcg@5", "ndcg@10")
ABLATION_ORDER = ("team_binary", "lambdarank_user_groups", "lambdarank_impression_groups", "inview_negatives",
                  "plus_trailing_popularity", "plus_short_term_session", "ranker_v2")
BASELINES = ("random", "popularity_6h", "popularity_24h", "popularity_48h", "recency", "cosine_history",
             "category_share")


def pareto_front(points: Sequence[Sequence[float]]) -> list[bool]:
    """모든 목적을 최대화할 때 다른 점에 지배되지 않는 점 표시(같은 점은 서로 지배하지 않는다)."""
    out = []
    for i, p in enumerate(points):
        dominated = any(
            all(q[k] >= p[k] for k in range(len(p))) and any(q[k] > p[k] for k in range(len(p)))
            for j, q in enumerate(points) if j != i
        )
        out.append(not dominated)
    return out


PROMOTION_MARGIN = 0.02
MIN_BEST_ITERATION = 5


def _ci_lo(diffs: dict, key: str, metric: str = "ndcg@10"):
    v = diffs.get(key, {}).get(metric)
    return (None, None) if v is None else (v["diff"], v["ci95"][0])


def promotion_verdict(d: dict) -> dict:
    """ADR 0013 사전 등록 승격 규칙 R1-R4를 JSON 수치에서 기계적으로 판정한다(비교가 없으면 실패)."""
    p1 = d.get("p1_vs_baselines", {})
    chosen = d.get("p2_selection", {}).get("chosen")
    rules = {}
    diff, lo = _ci_lo(p1, "ranker_v2-vs-popularity_24h")
    rules["R1"] = {"desc": f"P1 nDCG@10 ranker_v2 - popularity_24h >= +{PROMOTION_MARGIN} and CI lo > 0",
                   "diff": diff, "ci_lo": lo,
                   "pass": diff is not None and diff >= PROMOTION_MARGIN and lo > 0}
    diff, lo = _ci_lo(p1, "ranker_v2-vs-team_binary")
    rules["R2"] = {"desc": "P1 nDCG@10 ranker_v2 - team_binary CI lo > 0", "diff": diff, "ci_lo": lo,
                   "pass": lo is not None and lo > 0}
    diff, lo = _ci_lo(d.get("p2_vs", {}), f"{chosen}-vs-cosine_history")
    rules["R3"] = {"desc": f"P2 nDCG@10 {chosen} - cosine_history CI lo > 0", "diff": diff, "ci_lo": lo,
                   "pass": lo is not None and lo > 0}
    # dict.fromkeys: 중복 제거하면서 순서 고정(set은 문자열 해시 seed에 따라 표의 모델 순서가 바뀐다)
    iters = {n: [m["best_iteration"] for m in d.get("p1_models", {}).get(n, [])]
             for n in dict.fromkeys(["ranker_v2", chosen]) if n}
    rules["R4"] = {"desc": f"best_iteration > {MIN_BEST_ITERATION} for every seed", "best_iterations": iters,
                   "pass": bool(iters) and all(its and min(its) > MIN_BEST_ITERATION for its in iters.values())}
    return {"passed": all(r["pass"] for r in rules.values()), "p2_chosen": chosen, "rules": rules}


def recommended_mmr_lambda(ndcg_by_lambda: dict, max_rel_loss: float = 0.02) -> str:
    """nDCG@10이 lambda=1.0(순수 관련도) 대비 상대 max_rel_loss 이내인 가장 작은 lambda."""
    ref = ndcg_by_lambda["1.0"]
    ok = [lam for lam, v in ndcg_by_lambda.items() if v >= (1 - max_rel_loss) * ref]
    return min(ok, key=float)


def ci(m: dict, digits: int = 4) -> str:
    lo, hi = m["ci95"]
    return f"{m['mean']:.{digits}f} [{lo:.{digits}f}, {hi:.{digits}f}]"


def dci(m: dict, digits: int = 4) -> str:
    lo, hi = m["ci95"]
    return f"{m['diff']:+.{digits}f} [{lo:+.{digits}f}, {hi:+.{digits}f}]"


def table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _best_iters(d: dict, name: str) -> str:
    ms = d.get("p1_models", {}).get(name)
    if not ms:
        return "-"
    return "/".join(str(m["best_iteration"]) for m in ms)


def p1_table(d: dict) -> str:
    names = [n for n in BASELINES + ABLATION_ORDER if n in d["p1"]]
    names += [n for n in d["p1"] if n not in names]
    rows = []
    for n in names:
        r = d["p1"][n]
        std = max(r[c]["seed_std"] for c in P1_COLS if c in r)
        rows.append([n] + [ci(r[c]) for c in P1_COLS] + [f"{std:.4f}", _best_iters(d, n)])
    return table(["방법", "AUC", "MRR", "nDCG@5", "nDCG@10", "seed std(최대)", "best_iteration(seed별)"], rows)


def diff_table(diffs: dict, cols: Sequence[str] = ("auc", "ndcg@10")) -> str:
    rows = [[k] + [dci(v[c]) if c in v else "-" for c in cols] for k, v in diffs.items()]
    return table(["비교(a - b)"] + [f"Δ{c}" for c in cols], rows)


def seen_table(d: dict) -> str:
    rows = []
    for n, r in d["p1_seen_filtered"].items():
        base = d["p1"][n]
        rows.append([n, ci(base["auc"]), ci(r["auc"]), ci(base["ndcg@10"]), ci(r["ndcg@10"])])
    return table(["방법", "AUC(전체 후보)", "AUC(seen 제외)", "nDCG@10(전체)", "nDCG@10(seen 제외)"], rows)


def p2_sources_table(d: dict) -> str:
    src = d["p2_sources"]
    ks = sorted({int(k.split("@")[1]) for v in src.values() for k in v if k.startswith("recall@")})
    rows = []
    for n, v in src.items():
        row = [n] + [f"{v[f'recall@{k}']:.4f}" for k in ks]
        row.append(" / ".join(f"{v[f'mean_union_size@{k}']:.0f}" for k in ks) if n == "union" else "-")
        rows.append(row)
    return table(["출처"] + [f"Recall@{k}" for k in ks] + ["합집합 평균 크기"], rows)


def p2_table(d: dict) -> str:
    rows = []
    for n, r in d["p2"].items():
        rows.append([n, ci(r["ndcg@10"]), ci(r["recall@10"]), ci(r["mrr"]), f"{r['coverage@10']:.3f}",
                     ci(r["ild@10"], 3), ci(r["category_entropy@10"], 3), ci(r["novelty@10"], 2)])
    return table(["방법", "nDCG@10", "Recall@10", "MRR", "coverage@10", "ILD@10", "카테고리 엔트로피@10",
                  "novelty@10"], rows)


def two_stage_table(d: dict) -> str:
    """paired_vs_full_pool(같은 요청에서 전체 풀 랭킹 대비 쌍체 차이)이 있는 JSON만 차이 열을 붙인다
    (v1 JSON에는 없다)."""
    stages = d.get("p2_two_stage", {})
    paired = any("paired_vs_full_pool" in v for v in stages.values())
    rows = []
    for k, v in stages.items():
        row = [k, ci(v["ndcg@10"]), ci(v["recall@10"])]
        if paired:
            p = v.get("paired_vs_full_pool", {})
            row += [dci(p[m]) if m in p else "-" for m in ("ndcg@10", "recall@10")]
        rows.append(row)
    header = ["후보 생성", "nDCG@10", "Recall@10"]
    if paired:
        header += ["ΔnDCG@10 vs 전체 풀 (쌍체)", "ΔRecall@10 vs 전체 풀 (쌍체)"]
    return table(header, rows)


def mmr_table(d: dict) -> str:
    sweep = d["p2_mmr"]["sweep"]
    paired = d["p2_mmr"].get("paired_vs_lambda_1.0")
    lams = list(sweep)
    pts = [(sweep[l]["ndcg@10"]["mean"], sweep[l]["ild@10"]["mean"], sweep[l]["coverage@10"]) for l in lams]
    front = pareto_front(pts)
    rows = []
    for l, f in zip(lams, front):
        s = sweep[l]
        row = [l, ci(s["ndcg@10"]), f"{s['recall@10']:.4f}", ci(s["ild@10"], 3), f"{s['coverage@10']:.3f}",
               ci(s["category_entropy@10"], 3), ci(s["novelty@10"], 2), "O" if f else ""]
        if paired is not None:
            p = paired.get(l)
            row += [dci(p["ndcg@10"]) if p else "(기준)", dci(p["ild@10"]) if p else "(기준)"]
        rows.append(row)
    header = ["λ", "nDCG@10", "Recall@10", "ILD@10", "coverage@10", "카테고리 엔트로피@10", "novelty@10", "Pareto"]
    if paired is not None:
        header += ["ΔnDCG@10 vs λ=1.0 (쌍체)", "ΔILD@10 vs λ=1.0 (쌍체)"]
    return table(header, rows)


def replay_table(d: dict) -> str:
    rp = d["replay"]
    rows = [[k] + [ci(v[c]) for c in ("auc", "ndcg@10")] for k, v in rp["results"].items()]
    return table(["모델|프로필", "AUC", "nDCG@10"], rows)


def sanity_table(d: dict) -> str:
    s = d["embedding_sanity"]["category_knn_loo"]
    return table(["k", "기사 수", "카테고리 수", "kNN LOO 정확도", "다수 클래스 비율"],
                 [[s["k"], s["n"], s["n_categories"], f"{s['accuracy']:.4f}", f"{s['majority_baseline']:.4f}"]])


def selection_table(d: dict) -> str:
    sel = d["p2_selection"]
    rows = [[n, f"{v['ndcg@10_seed_mean']:.4f}", " / ".join(f"{x:.4f}" for x in v["seed_values"]),
             "선택" if n == sel["chosen"] else ""] for n, v in sel["table"].items()]
    return table(["모델", "es 표본 P2 nDCG@10(seed 평균)", "seed별", ""], rows)


def verdict_table(d: dict) -> str:
    v = promotion_verdict(d)
    rows = []
    for k, r in v["rules"].items():
        if "diff" in r:
            val = "-" if r["diff"] is None else f"{r['diff']:+.4f} (CI 하한 {r['ci_lo']:+.4f})"
        else:
            val = ", ".join(f"{n}: {its}" for n, its in r["best_iterations"].items())
        rows.append([k, r["desc"], val, "통과" if r["pass"] else "실패"])
    rows.append(["종합", "R1-R4 모두", "", "통과" if v["passed"] else "실패"])
    return table(["규칙", "내용", "측정값", "판정"], rows)


def _partial_run(d: dict) -> bool:
    """--only-models로 일부 모델만 학습한 실행인지(meta.argv 기준)."""
    return "--only-models" in (d.get("meta", {}).get("argv") or [])


def render(d: dict) -> str:
    parts = []
    if "embedding_sanity" in d:
        parts += ["### 임베딩 점검(카테고리 kNN leave-one-out)", sanity_table(d)]
    parts += ["### P1 전체", p1_table(d)]
    # --only-models 부분 실행은 사슬이 비어 있어 ablation·기준 비교가 없을 수 있다
    if d.get("p1_ablation_diffs"):
        parts += ["### P1 ablation (인접 단계 쌍체 차이)", diff_table(d["p1_ablation_diffs"])]
    if d.get("p1_vs_baselines"):
        parts += ["### P1 ranker v2 vs 베이스라인/변형", diff_table(d["p1_vs_baselines"])]
    parts += ["### P1 seen 필터", seen_table(d)]
    if "p2" in d:
        if "p2_selection" in d:
            parts += ["### P2 모델 선택(es 구간 표본, validation 미사용)", selection_table(d)]
        parts += ["### P2 후보 출처 recall", p2_sources_table(d), "### P2 전체 풀 랭킹", p2_table(d)]
        if d.get("p2_ablation_diffs"):
            parts += ["### P2 ablation (인접 단계 쌍체 차이)",
                      diff_table(d["p2_ablation_diffs"], cols=("ndcg@10", "recall@10"))]
        if "p2_vs" in d:
            parts += ["### P2 선택 모델 vs 나머지", diff_table(d["p2_vs"], cols=("ndcg@10", "recall@10"))]
        if "p2_two_stage" in d:
            parts += ["### P2 2단계(출처 합집합 -> 랭커)", two_stage_table(d)]
        if "p2_mmr" in d:
            sweep = d["p2_mmr"]["sweep"]
            lam = recommended_mmr_lambda({k: v["ndcg@10"]["mean"] for k, v in sweep.items()})
            parts += ["### MMR λ 스윕", mmr_table(d), f"사전 등록 규칙(λ=1.0 대비 nDCG@10 상대 손실 ≤2%)의 권장 λ: **{lam}**"]
        if d.get("protocol", {}).get("chain", "inview") == "inview":
            if _partial_run(d):
                parts += ["### 승격 규칙 판정(ADR 0013 사전 등록)",
                          "`--only-models` 부분 실행이라 판정하지 않음(판정은 전체 사슬을 돌린 JSON에서만)."]
            else:
                parts += ["### 승격 규칙 판정(ADR 0013 사전 등록)", verdict_table(d)]
    if "replay" in d:
        rp = d["replay"]
        parts += ["### 실시간 재생",
                  f"대상: {rp['definition']} — 조건을 만족하는 노출은 평가 노출의 {rp['share_of_test_impressions']:.1%}"
                  f"(이 중 평가 {rp['n_impressions']:,}건, 유저 {rp['n_users']:,}명)",
                  replay_table(d), diff_table(rp["diffs"])]
    return "\n\n".join(parts) + "\n"


def click_time_tables(d: dict) -> str:
    """click_time_sensitivity.py JSON(클릭 시각 근사 민감도)의 표."""
    rt = d["read_time"]
    q, over = rt["quantiles_seconds"], rt["share_over_seconds"]
    parts = ["### 클릭이 있는 평가 노출의 read_time(페이지 체류 초)",
             table(["노출 수", "결측"] + list(q) + [f">{s}초 비율" for s in over],
                   [[f"{rt['n_impressions_with_click']:,}", f"{rt['n_missing']:,}"] + [f"{v:.0f}" for v in q.values()]
                    + [f"{v:.3f}" for v in over.values()]])]
    for sec, cols in (("p1", ("auc", "ndcg@10")), ("p2", ("ndcg@10", "recall@10"))):
        rows = [[k] + [ci(v[c]) for c in cols] for k, v in d[sec]["results"].items()]
        parts += [f"### {sec.upper()} 인기도 베이스라인(창 끝 = t − gap초)", table(["방법|gap"] + list(cols), rows),
                  diff_table(d[sec]["diffs_vs_gap0"], cols=cols)]
    rep = d.get("reproduces_reference")
    if rep:
        parts.append(f"gap0과 {rep['reference']}의 같은 베이스라인 평균 최대 절대 차이: "
                     f"P1 {rep['p1']['max_abs_diff_of_means']:.1e}, P2 {rep['p2']['max_abs_diff_of_means']:.1e}")
    return "\n\n".join(parts) + "\n"


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    with open(args[0]) as f:
        d = json.load(f)
    # click_time_sensitivity.py JSON은 gap 목록을 meta에 담는다
    sys.stdout.write(click_time_tables(d) if "gaps_seconds" in d.get("meta", {}) else render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
