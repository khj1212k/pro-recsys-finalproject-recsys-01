"""run_cold.py가 쓴 JSON에서 리포트 마크다운을 만든다(수치를 손으로 옮기지 않는다).

    python -m evaluation.recsys.ebnerd.make_cold_report reports/recsys/ebnerd_v1_2_cold.json \
        > reports/recsys/ebnerd_v1_2_cold.md

머리말에 증거 등급을 찍는다. 등록한 인자·입력과 다른 실행(demo 데이터, 표본 상한, 가짜 임베딩 등)은
"demo, not evidence"로 표기되고 판정 표에 "판정 아님"이 붙는다. 없는 실험은 "미측정"으로 적는다.
"""
from __future__ import annotations

import json
import sys
from typing import Optional

from .cold_verdicts import EVIDENCE_GRADE
from .make_report import ci, dci, table

GRID_METHODS = ("poolneg", "poolneg_mask0", "poolneg_masknan", "poolneg_rank", "popularity_6h", "recency",
                "heuristic_cold", "heuristic_prior4", "random")
STATUS_KO = {"pass": "통과", "fail": "실패", "unmeasured": "미측정", "measured": "측정됨", "met": "계약 충족",
             "not_met": "불충족", "inconclusive": "보류(검정력 부족)"}


def _m(cell: dict, method: str, metric: str = "ndcg@10") -> str:
    v = cell.get("methods", {}).get(method, {}).get(metric)
    return ci(v) if v else "-"


def _d(container: dict, key: str, metric: str = "ndcg@10") -> str:
    v = container.get(key, {}).get(metric) if isinstance(container.get(key), dict) else None
    return dci(v) if v else "-"


def _fmt_diff(v: Optional[dict]) -> str:
    return "-" if not v else f"{v['diff']:+.4f} [{v['lo']:+.4f}, {v['hi']:+.4f}]"


def header(d: dict) -> str:
    meta = d["meta"]
    ev = meta["evidence"]
    pre = meta["preregistration"]
    lines = ["# EB-NeRD v1.2 콜드 regime — 결과 표", ""]
    if ev["grade"] == EVIDENCE_GRADE:
        lines.append(f"> 증거 등급: **{ev['grade']}** — 등록한 인자·입력으로 돈 실행이다. 라벨 {meta['label']}"
                     "(덴마크어 공개 벤치마크). 한국어 서비스의 성능이 아니다.")
    else:
        lines.append(f"> 증거 등급: **{ev['grade']}** — 이 파일의 어떤 수치도 근거로 쓰지 않는다. 배선 확인용이다.")
        lines.append("> 사유: " + "; ".join(ev["reasons"]))
    lines += [
        f"> 사전 등록: `{pre['id']}` (ADR 0013 A2 사전 등록, yaml sha256 `{pre['sha256'][:12]}…`"
        + (f", 커밋 `{pre['commit']}`" if pre.get("commit") else "") + f"). 코드 SHA `{meta['code_sha']}`.",
        f"> 실행: dataset `{meta['dataset']}`, seeds {meta['seeds']}, 부트스트랩 {meta['n_boot']}회, "
        f"P2 표본 {meta['p2_sample']}, 서브샘플 상한 {meta['sub_cap']}, fake_dim {meta['fake_dim']}, "
        f"max_fit {meta['max_fit']}, max_test {meta['max_test']}.",
        "> 값은 평균 [95% CI](유저 단위 클러스터 부트스트랩), 차이는 같은 요청의 쌍체 차이다. 이 파일은 "
        "`make_cold_report`가 JSON에서 만든다. 손으로 고치지 않는다.",
    ]
    return "\n".join(lines)


def verdict_section(d: dict) -> str:
    v = d["verdicts"]
    vv = v["verdicts"]
    note = "" if v["judgeable"] else " (판정 아님)"
    rows = []
    g = v["gate"]
    rows.append(["재현 게이트", f"{g['arm']} nDCG@10 seed 평균이 {g['within']} 안",
                 "-" if g["mean"] is None else f"{g['mean']:.4f}", STATUS_KO[g["status"]]])
    a, b = vv["e1"]["a"], vv["e1"]["b"]
    rows.append(["E1(a)", "pop0에서 poolneg_mask0 − poolneg CI 하한 > 0.005", _fmt_diff(a if "diff" in a else None),
                 "미측정" if a["status"] == "unmeasured" else a["decision"] + note])
    if b["status"] == "unmeasured":
        rows.append(["E1(b)", "sub1·풀 60에서 랭커 − heuristic_cold", "-", "미측정"])
    else:
        val = "; ".join(f"{m}: {_fmt_diff(c)}" for m, c in b["comparisons"].items())
        rows.append(["E1(b)", "sub1·풀 60에서 랭커 − heuristic_cold", val, b["decision"] + note])
    e2 = vv["e2"]
    if e2["status"] == "unmeasured":
        rows.append(["E2", "k* (poolneg(k) − popularity_6h)", "-", "미측정"])
    else:
        val = ", ".join(f"k={k}: {c['lo']:+.4f}{'✓' if c['pass'] else '✗'}" for k, c in e2["per_k"].items())
        rows.append(["E2", "k* (CI 하한, 그 k와 더 큰 k 전부에서 > 0)", val,
                     f"{e2['decision']}; k_gain = {e2['k_gain']}" + note])
    e3 = vv["e3"]
    rows.append(["E3", "poolneg_rank − poolneg CI 하한 > −0.01", _fmt_diff(e3 if "diff" in e3 else None),
                 STATUS_KO[e3["status"]] + ("" if e3["status"] == "unmeasured" else note)])
    e6 = vv["e6"]
    if e6["status"] == "unmeasured":
        rows.append(["E6", "축소 CTR − poolneg (sub1·sub5 × α)", "-", "미측정"])
    else:
        val = "; ".join(f"{k}: {c['lo']:+.4f}" for k, c in e6["comparisons"].items())
        rows.append(["E6", "6개 비교 중 CI 하한 > 0.005", val, e6["decision"] + note])
    e7 = vv["e7"]
    if e7["status"] == "unmeasured":
        rows.append(["E7", "적합 4항 vs 사전값·popularity_6h", "-", "미측정"])
    else:
        val = "; ".join(f"({n}) 사전값 대비 {_fmt_diff(s['vs_prior'])}, "
                        f"{'사전값보다 나음' if s['beats_prior'] else '사전값보다 낫다고 못 함'}, "
                        f"{'스코어러' if s['role'] == 'scorer' else '동점 깨기용'}" for n, s in e7["sets"].items())
        rows.append(["E7", "세트별 표기와 서빙 기본값", val, e7["decision"] + note])
    e8 = vv["e8"]
    if e8["serving"] is None:
        rows.append(["E8", "서빙 구성: 합집합 재현율 ≥ 0.90, 2단계 − 전체 풀 CI 하한 > −0.005", "-", "미측정"])
    else:
        s = e8["serving"]
        val = f"재현율 {s['union_recall']:.4f}, 2단계 − 전체 풀 {_fmt_diff(s['two_stage_vs_full_pool'])}"
        rows.append(["E8", "서빙 구성: 합집합 재현율 ≥ 0.90, 2단계 − 전체 풀 CI 하한 > −0.005", val,
                     ("미측정(하네스 구성 수치 없음)" if e8["status"] == "unmeasured" else e8["label"] + note)])
    out = ["## 0. 판정 요약(기계 판정)"]
    if not v["judgeable"]:
        out.append(f"**이 실행은 판정에 쓰지 않는다** — {v['not_judgeable_reason']}. 아래 표는 규칙이 어떻게 적용되는지만 보여 준다.")
    out.append(table(["규칙", "내용", "측정값", "판정"], rows))
    um = d.get("unmeasured", {})
    if um.get("rules") or um.get("stages"):
        out.append(f"미측정 규칙: {', '.join(um.get('rules', [])) or '없음'} / 끝나지 않은 단계: "
                   f"{', '.join(um.get('stages', [])) or '없음'}. 미측정은 기각이 아니다.")
    return "\n\n".join(out)


def e1_section(d: dict) -> str:
    cells = d.get("e1", {}).get("cells", {})
    if not cells:
        return "## 1. E1 조건 격자\n\n미측정."
    methods = [m for m in GRID_METHODS if any(m in c["methods"] for c in cells.values())]
    rows = [[key, c["n_requests"], c["n_users"]] + [_m(c, m) for m in methods] for key, c in cells.items()]
    parts = ["## 1. E1 조건 격자 — P2 nDCG@10 (트래픽 조건|풀 크기)",
             table(["조건", "요청", "유저"] + list(methods), rows)]
    keys = ["poolneg_mask0-vs-poolneg", "poolneg_masknan-vs-poolneg", "poolneg_masknan@nan-vs-poolneg",
            "poolneg_rank-vs-poolneg", "poolneg-vs-heuristic_cold", "poolneg_mask0-vs-heuristic_cold",
            "poolneg-vs-popularity_6h"]
    keys = [k for k in keys if any(k in c["diffs"] for c in cells.values())]
    rows = [[key] + [_d(c["diffs"], k) for k in keys] for key, c in cells.items()]
    parts += ["### 쌍체 차이 ΔnDCG@10 (a − b)", table(["조건"] + [k.replace("-vs-", " − ") for k in keys], rows)]
    return "\n\n".join(parts)


def e2_section(d: dict) -> str:
    e2 = d.get("e2", {})
    parts = ["## 2. E2 히스토리 절단 곡선"]
    p2 = e2.get("p2", {})
    if not p2:
        parts.append("P2 곡선: 미측정.")
    else:
        rows = []
        for key, c in p2.items():
            rows.append([key, f"{c['truncated_share']:.3f}", _m(c, "poolneg"), _m(c, "poolneg_mask0"),
                         _m(c, "popularity_6h"), _m(c, "recency"), _m(c, "cosine_history"),
                         _d(c["diffs"], "poolneg-vs-popularity_6h"), _d(c["diffs"], "poolneg-vs-recency"),
                         _d(c, "gain_vs_k0")])
        parts.append(table(["조건|k", "절단이 걸린 요청 비율", "poolneg", "poolneg_mask0", "popularity_6h", "recency",
                            "cosine_history", "poolneg − popularity_6h", "poolneg − recency", "poolneg(k) − poolneg(0)"],
                           rows))
        cons = e2.get("consistency_with_e1")
        if cons:
            parts.append(f"점검: {cons['what']} = {cons['max_abs_diff']:.2e}")
    p1 = e2.get("p1", {})
    if not p1:
        parts.append("P1 곡선(서술용): 미측정.")
    else:
        rows = [[key, f"{c['truncated_share']:.3f}", _m(c, "ranker_v2"), _m(c, "popularity_24h"),
                 _m(c, "cosine_history"), _d(c["diffs"], "ranker_v2-vs-popularity_24h")] for key, c in p1.items()]
        parts += ["### P1 곡선(서술용, 판정 없음)",
                  table(["k", "절단이 걸린 요청 비율", "ranker_v2", "popularity_24h", "cosine_history",
                         "ranker_v2 − popularity_24h"], rows)]
    return "\n\n".join(parts)


def e4_section(d: dict) -> str:
    e4 = d.get("e4")
    if not e4:
        return "## 3. E4 일일 배치 릴리스(서술용)\n\n미측정."
    methods = sorted({m for b in e4["buckets"].values() for m in b.get("methods", {})})
    rows = [[label, b["n_requests"], b["n_users"]] + [ci(b["methods"][m]["ndcg@10"]) if m in b.get("methods", {}) else "-"
                                                       for m in methods] for label, b in e4["buckets"].items()]
    um = e4["unit_meta"]
    return "\n\n".join([
        "## 3. E4 일일 배치 릴리스(서술용, 판정 없음)",
        f"요청 {um['n_requests']:,}건, 평균 풀 {um['pool_size_mean']:.1f}개, 풀이 담은 정답 비율(상한) "
        f"{um['recall_upper_bound']:.4f}, 풀 안에 정답이 없는 요청 {e4['requests_without_positive_in_pool']:,}건(버킷에서 제외). "
        "버킷은 풀 안 정답의 릴리스 후 경과 시간이고, nDCG@10은 풀 안 정답만으로 계산했다.",
        table(["버킷", "요청", "유저"] + methods, rows)])


def e6_e7_section(d: dict) -> str:
    cells = d.get("e1", {}).get("cells", {})
    parts = ["## 4. E6 축소 CTR"]
    rows = []
    for key in ("sub1|full", "sub5|full", "sub20|full", "orig|full"):
        c = cells.get(key)
        if c:
            rows.append([key] + [_d(c["diffs"], f"poolneg_shrunk_a{a}-vs-poolneg") for a in (5, 20, 50)])
    parts.append(table(["조건", "α=5 − poolneg", "α=20 − poolneg", "α=50 − poolneg"], rows) if rows else "미측정.")
    parts.append("## 5. E7 4항 휴리스틱 가중치")
    e7 = d.get("e7", {})
    w = e7.get("weights", {})
    if not w:
        parts.append("미측정.")
        return "\n\n".join(parts)
    rows = []
    for name, by_seed in w.items():
        for seed, r in by_seed.items():
            rows.append([name, seed] + [f"{x:+.4f}" for x in r["weights"]]
                        + [" / ".join(f"{x:+.3f}" for x in r["weights_l1_normalized"]), r["n_groups"],
                           "예" if r["converged"] else "아니오"])
    parts.append(table(["세트", "seed", "hist_cos", "short_cos", "recency", "log1p(pop_clicks_6h)", "L1 정규화",
                        "적합 요청 수", "수렴"], rows))
    pw = e7["prior_weights"]
    parts.append(f"사전값: 장기 {pw['long_term']} / 단기 {pw['short_term']} / 신선도 {pw['recency']} / 인기 {pw['popularity']} "
                 "(인기 항의 형태가 달라 가중치를 직접 비교하지 않는다: 사전값은 min(1, log1p(x)/5), 적합은 log1p(x)).")
    rows = []
    for name, key in (("a", "orig|full"), ("b", "sub1|full"), ("a", "sub1|full"), ("b", "orig|full")):
        c = cells.get(key)
        if c:
            h = f"heuristic_fit_{name}"
            rows.append([f"({name}) @ {key}", _m(c, h), _m(c, "heuristic_prior4"), _d(c["diffs"], f"{h}-vs-heuristic_prior4"),
                         _d(c["diffs"], f"{h}-vs-popularity_6h")])
    if rows:
        parts.append(table(["세트 @ 조건", "적합 4항", "사전값 4항", "적합 − 사전값", "적합 − popularity_6h"], rows))
    return "\n\n".join(parts)


def e8_section(d: dict) -> str:
    e8 = d.get("e8", {})
    if not e8:
        return "## 6. E8 후보 생성기 구성\n\n미측정."
    rows = []
    for name, c in e8.items():
        ts = c["two_stage"]
        rows.append([name, f"{c['config']['window_h']:.0f}h", c["judged_model"], f"{c['pool_size_mean']:.1f}",
                     f"{c['union']['mean_union_size_after_seen']:.1f}", f"{c['union_recall']:.4f}",
                     f"{c['recall_upper_bound']:.4f}",
                     ci(ts["full_pool"]["ndcg@10"]), ci(ts["two_stage"]["ndcg@10"]),
                     dci(ts["paired_vs_full_pool"]["ndcg@10"])])
        for arm, block in c.get("descriptive", {}).items():
            rows.append([f"{name} (서술용)", f"{c['config']['window_h']:.0f}h", arm, "", "", "", "",
                         ci(block["full_pool"]["ndcg@10"]), ci(block["two_stage"]["ndcg@10"]),
                         dci(block["paired_vs_full_pool"]["ndcg@10"])])
    parts = ["## 6. E8 후보 생성기 구성",
             table(["구성", "풀 창", "모델", "평균 풀(seen 제외)", "평균 합집합(seen 제외)", "합집합 재현율", "풀 재현율 상한", "전체 풀 nDCG@10",
                    "2단계 nDCG@10", "2단계 − 전체 풀"], rows)]
    contrib = e8.get("serving", {}).get("union", {}).get("mean_contributed")
    if contrib:
        parts.append("서빙 구성의 출처별 평균 기여 수(라운드로빈 순): "
                     + ", ".join(f"{k} {v:.1f}" for k, v in contrib.items()))
    return "\n\n".join(parts)


def models_section(d: dict) -> str:
    rows = []
    for arm, ms in d.get("models", {}).items():
        rows.append([arm, "/".join(str(m["best_iteration"]) for m in ms),
                     "/".join(f"{m['best_es_score']:.4f}" for m in ms), ms[0]["fit_rows"],
                     "/".join(f"{m.get('masked_fit_request_share', 0):.3f}" for m in ms) if "masked_fit_request_share" in ms[0] else "-"])
    parts = ["## 7. 모델과 실행 기록",
             table(["arm", "best_iteration(seed별)", "es nDCG@10(seed별)", "fit 행", "가린 fit 요청 비율"], rows) if rows else "모델 없음."]
    stages = d.get("stages", {})
    if stages:
        parts.append(table(["단계", "완료 단위", "전체 단위", "초"],
                           [[k, s["units_done"], s["units_total"], s["seconds"]] for k, s in stages.items()]))
    comp = d["meta"].get("compute")
    if comp:
        parts.append("계산 자원: " + json.dumps(comp, ensure_ascii=False))
    return "\n\n".join(parts)


def render(d: dict) -> str:
    return "\n\n".join([header(d), verdict_section(d), e1_section(d), e2_section(d), e4_section(d),
                        e6_e7_section(d), e8_section(d), models_section(d)]) + "\n"


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    with open(args[0], encoding="utf-8") as f:
        sys.stdout.write(render(json.load(f)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
