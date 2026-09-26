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
    rows = [[k, ci(v["ndcg@10"]), ci(v["recall@10"])] for k, v in d.get("p2_two_stage", {}).items()]
    return table(["후보 생성", "nDCG@10", "Recall@10"], rows)


def mmr_table(d: dict) -> str:
    sweep = d["p2_mmr"]["sweep"]
    lams = list(sweep)
    pts = [(sweep[l]["ndcg@10"]["mean"], sweep[l]["ild@10"]["mean"], sweep[l]["coverage@10"]) for l in lams]
    front = pareto_front(pts)
    rows = []
    for l, f in zip(lams, front):
        s = sweep[l]
        rows.append([l, ci(s["ndcg@10"]), f"{s['recall@10']:.4f}", ci(s["ild@10"], 3), f"{s['coverage@10']:.3f}",
                     ci(s["category_entropy@10"], 3), ci(s["novelty@10"], 2), "O" if f else ""])
    return table(["λ", "nDCG@10", "Recall@10", "ILD@10", "coverage@10", "카테고리 엔트로피@10", "novelty@10",
                  "Pareto"], rows)


def replay_table(d: dict) -> str:
    rp = d["replay"]
    rows = [[k] + [ci(v[c]) for c in ("auc", "ndcg@10")] for k, v in rp["results"].items()]
    return table(["모델|프로필", "AUC", "nDCG@10"], rows)


def render(d: dict) -> str:
    parts = [
        "### P1 전체", p1_table(d),
        "### P1 ablation (인접 단계 쌍체 차이)", diff_table(d["p1_ablation_diffs"]),
        "### P1 ranker v2 vs 베이스라인/변형", diff_table(d["p1_vs_baselines"]),
        "### P1 seen 필터", seen_table(d),
    ]
    if "p2" in d:
        parts += ["### P2 후보 출처 recall", p2_sources_table(d), "### P2 전체 풀 랭킹", p2_table(d)]
        if d.get("p2_ablation_diffs"):
            parts += ["### P2 ablation (인접 단계 쌍체 차이)",
                      diff_table(d["p2_ablation_diffs"], cols=("ndcg@10", "recall@10"))]
        if "p2_vs" in d:
            parts += ["### P2 선택 모델 vs 나머지", diff_table(d["p2_vs"], cols=("ndcg@10", "recall@10"))]
        if "p2_two_stage" in d:
            parts += ["### P2 2단계(출처 합집합 -> 랭커)", two_stage_table(d)]
        if "p2_mmr" in d:
            parts += ["### MMR λ 스윕", mmr_table(d)]
    if "replay" in d:
        parts += ["### 실시간 재생", replay_table(d), diff_table(d["replay"]["diffs"])]
    return "\n\n".join(parts) + "\n"


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    with open(args[0]) as f:
        d = json.load(f)
    sys.stdout.write(render(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
