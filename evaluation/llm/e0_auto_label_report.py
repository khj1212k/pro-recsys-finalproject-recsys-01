"""E0 자동 라벨 리포트 (ADR 0009 A8.13 (c)) - reports/llm/e0_auto_labels_v1.{json,md}.

자동 라벨러(`evaluation.llm.auto_labeler`)가 쓴 라벨 파일과 호출 기록, E0 run 디렉터리의 노드 기록(judge·검사기·클러스터
평가기)만 읽는다. 리포트에는 id·라벨 값·건수·비율만 싣는다. 쓰기 전에 기사·초안·변환본·라벨러 텍스트의 16자 조각과
겹치는지 보고 겹치면 쓰지 않는다(`auto_labeler.LeakGuard`).

    python -m evaluation.llm.e0_auto_label_report            # 메인 체크아웃 data/의 패키지와 run을 읽는다
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from evaluation.llm import auto_labeler as al

REPORT_REL = "reports/llm/e0_auto_labels_v1"
SIZE_BUCKETS = (("3-4", 3, 4), ("5-9", 5, 9), ("10+", 10, None))

NOT_COMPUTABLE = [
    {"what": "사람 발행 가능률(A8.8 쓰임 1, bake-off 1차 지표의 정의 그대로의 값)", "why": "사람 라벨 없음(사용자 결정 2026-10-17)"},
    {"what": "judge 보정 - 사람 라벨 대비 κ·Spearman(ADR 0009 judge 선정, ADR 0010)", "why": "사람 라벨 없음"},
    {"what": "결정론 검사기의 정밀도·재현율(사람 정답 대비, ADR 0010 '측정하지 않은 것')", "why": "사람 라벨 없음 - 라벨러 구간과의 겹침만 낸다"},
    {"what": "사람 라벨 신뢰도(intra-rater)와 가이드 5절의 κ < 0.60 규칙", "why": "사람 라벨 없음"},
    {"what": "자동 라벨러 자신의 정확도", "why": "정답이 없다 - 일치는 타당성을 보여 주지 않는다"},
    {"what": "라벨 시간 실측(A8.7 label_hours_actual)", "why": "사람 라벨 없음"},
]
CAVEATS = [
    "라벨러(gemini-3-flash-preview)·generator(gemini-3.5-flash-lite)·judge v2(gemini-3.1-flash-lite)가 모두 Gemini다(같은 계열). "
    "라벨러 대 judge 일치는 독립 검증이 아니다.",
    "라벨러는 요청받은 '가장 강한 모델'이 아니다 - 비용 상한 $0.50 때문에 선정 규칙(A8.13 (b)1)이 고른 preview 모델이다.",
    "기사당 앞 1500자만 보여 줬다(가이드는 전체 본문). 1500자 뒤에만 있는 사실을 초안이 맞게 썼다면 오류로 세므로 "
    "사실 오류율은 높게, 발행 가능률은 낮게 치우칠 수 있다.",
    "thinking은 최소(minimal), temperature 1.0(기본값). 런 b는 재라벨 대상 + 예산 안의 나머지라 전 항목이 아니다.",
    "n이 작다(클러스터 15, 출력 14). 비율의 Wilson 95% CI가 넓다. 가설 검정은 없다.",
]


def ratio(k: int, n: int) -> Dict[str, Any]:
    from evaluation.llm.e0_report import ratio as _ratio

    return _ratio(k, n)


def kappa(pairs: Sequence[Tuple[Any, Any]]) -> Optional[float]:
    """Cohen's κ(범주형, 가중치 없음). 기대 일치가 1이면(한 범주만 나옴) 정의되지 않아 None."""
    n = len(pairs)
    if n == 0:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
    if pe >= 1.0:
        return None
    return round((po - pe) / (1 - pe), 4)


def agreement(pairs: Sequence[Tuple[Any, Any]], ids: Sequence[str]) -> Dict[str, Any]:
    k = sum(1 for a, b in pairs if a == b)
    return {**ratio(k, len(pairs)), "kappa_reference_only": kappa(pairs),
            "disagreement_ids": sorted(i for i, (a, b) in zip(ids, pairs) if a != b)}


def dist(values: Iterable[float]) -> Dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "median": round(statistics.median(vals), 4), "min": round(min(vals), 4), "max": round(max(vals), 4),
            "sum": round(sum(vals), 6)}


def bucket_of(n_articles: int) -> str:
    for name, lo, hi in SIZE_BUCKETS:
        if n_articles >= lo and (hi is None or n_articles <= hi):
            return name
    return "<3"


def _git_head() -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(al.REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------- 지표


def _n_errors(row: Dict[str, Any]) -> int:
    return len(row["label"]["fact_errors"]) + len(row["auto"].get("unlocated_fact_errors") or [])


def output_stats(rows: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    errors = [_n_errors(r) for r in rows.values()]
    types = Counter(e["type"] for r in rows.values() for e in r["label"]["fact_errors"])
    types.update(e["type"] for r in rows.values() for e in r["auto"].get("unlocated_fact_errors") or [])
    applicable = [r for r in rows.values() if r["auto"].get("tone_drift_applicable")]
    rule_n = Counter(k for r in rows.values() for k, v in (r["auto"].get("rules") or {}).items() if v is False)
    cov_basis = Counter(r["auto"].get("coverage_basis") for r in rows.values())
    facts_total = sum(r["auto"].get("coverage_n_facts") or 0 for r in rows.values() if r["auto"].get("coverage_basis") in ("a", "b"))
    facts_cov = sum(len(r["label"]["key_facts_covered"]) for r in rows.values() if r["auto"].get("coverage_basis") in ("a", "b"))
    return {
        "n_outputs_labeled": n,
        "publishable": ratio(sum(1 for r in rows.values() if r["label"]["publishable"]), n),
        "publishable_labeler_raw": ratio(sum(1 for r in rows.values() if r["auto"]["publishable_raw"]), n),
        "rule_made_no": dict(sorted(rule_n.items())),
        "fact_error_ge1": ratio(sum(1 for e in errors if e >= 1), n),
        "fact_errors_per_output": dist(errors),
        "fact_error_types": dict(sorted(types.items())),
        "unlocated_fact_errors": sum(len(r["auto"].get("unlocated_fact_errors") or []) for r in rows.values()),
        "ambiguous_spans_first_occurrence_used": sum(int(r["auto"].get("ambiguous_spans") or 0) for r in rows.values()),
        "style": dict(sorted(Counter(str(r["label"]["style"]) for r in rows.values()).items())),
        "headline_mismatch": ratio(sum(1 for r in rows.values() if r["auto"]["headline_mismatch"]), n),
        "key_fact_coverage_micro": ratio(facts_cov, facts_total),
        "coverage_basis": dict(sorted((str(k), v) for k, v in cov_basis.items())),
        "tone_drift": ratio(sum(1 for r in applicable if r["label"]["tone_drift"]), len(applicable)),
        "tone_drift_not_applicable": n - len(applicable),
    }


def cluster_stats(rows: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    return {
        "n_clusters_labeled": n,
        "single_event": ratio(sum(1 for r in rows.values() if r["label"]["single_event"]), n),
        "with_outliers": ratio(sum(1 for r in rows.values() if r["label"]["outlier_ids"]), n),
        "outliers_per_cluster": dist(len(r["label"]["outlier_ids"]) for r in rows.values()),
        "key_facts_per_cluster": dist(len(r["label"]["key_facts"]) for r in rows.values()),
        "no_main_event": sum(1 for r in rows.values() if not r["label"]["key_facts"]),
    }


def run_agreement(a_cl, b_cl, a_out, b_out) -> Dict[str, Any]:
    c_ids = sorted(set(a_cl) & set(b_cl))
    o_ids = sorted(set(a_out) & set(b_out))
    res: Dict[str, Any] = {"n_clusters_both": len(c_ids), "n_outputs_both": len(o_ids), "cluster_ids_both": c_ids,
                           "output_ids_both": o_ids}
    res["single_event"] = agreement([(a_cl[i]["label"]["single_event"], b_cl[i]["label"]["single_event"]) for i in c_ids], c_ids)
    res["outlier_set_exact"] = agreement([(tuple(sorted(a_cl[i]["label"]["outlier_ids"])), tuple(sorted(b_cl[i]["label"]["outlier_ids"])))
                                          for i in c_ids], c_ids)
    res["main_event_present"] = agreement([(bool(a_cl[i]["label"]["key_facts"]), bool(b_cl[i]["label"]["key_facts"])) for i in c_ids], c_ids)
    res["publishable"] = agreement([(a_out[i]["label"]["publishable"], b_out[i]["label"]["publishable"]) for i in o_ids], o_ids)
    res["publishable_labeler_raw"] = agreement([(a_out[i]["auto"]["publishable_raw"], b_out[i]["auto"]["publishable_raw"])
                                                for i in o_ids], o_ids)
    res["fact_error_present"] = agreement([(_n_errors(a_out[i]) > 0, _n_errors(b_out[i]) > 0) for i in o_ids], o_ids)
    res["fact_error_count_equal"] = agreement([(_n_errors(a_out[i]), _n_errors(b_out[i])) for i in o_ids], o_ids)
    res["style_exact"] = agreement([(a_out[i]["label"]["style"], b_out[i]["label"]["style"]) for i in o_ids], o_ids)
    within = sum(1 for i in o_ids if abs(a_out[i]["label"]["style"] - b_out[i]["label"]["style"]) <= 1)
    res["style_within_1"] = ratio(within, len(o_ids))
    res["headline_mismatch"] = agreement([(a_out[i]["auto"]["headline_mismatch"], b_out[i]["auto"]["headline_mismatch"]) for i in o_ids], o_ids)
    tone_ids = [i for i in o_ids if a_out[i]["auto"].get("tone_drift_applicable") and b_out[i]["auto"].get("tone_drift_applicable")]
    res["tone_drift"] = agreement([(a_out[i]["label"]["tone_drift"], b_out[i]["label"]["tone_drift"]) for i in tone_ids], tone_ids)
    fact_pairs, fact_ids = [], []
    for i in o_ids:
        if a_out[i]["auto"].get("coverage_basis") == "a" and b_out[i]["auto"].get("coverage_basis") == "a":
            n = int(a_out[i]["auto"].get("coverage_n_facts") or 0)
            ca, cb = set(a_out[i]["label"]["key_facts_covered"]), set(b_out[i]["label"]["key_facts_covered"])
            for j in range(n):
                fact_pairs.append((j in ca, j in cb))
                fact_ids.append(f"{i}#{j}")
    res["coverage_per_fact_same_facts"] = agreement(fact_pairs, fact_ids)
    res["note"] = "κ는 정의되면 참고로만 적고 해석하지 않는다(A8.8 재라벨의 보고 규칙). 런 b의 coverage는 런 a의 핵심 사실로 물었다."
    return res


def judge_comparison(a_out, key: Dict[str, Dict[str, Any]], nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    judge = {}
    for n in nodes:
        if n["node"] == "eval_newsletter" and n.get("attempt") == 1 and n.get("criteria"):
            judge.setdefault(n["item_id"], n)
    pairs_pub, pairs_err, ids = [], [], []
    table = {"judge_pass_pub_Y": 0, "judge_pass_pub_N": 0, "judge_fail_pub_Y": 0, "judge_fail_pub_N": 0}
    err_table = {"judge_unsupported_ge1_err_Y": 0, "judge_unsupported_ge1_err_N": 0, "judge_unsupported_0_err_Y": 0,
                 "judge_unsupported_0_err_N": 0}
    for oid, row in sorted(a_out.items()):
        k = key.get(oid) or {}
        if k.get("kind") != "first_draft":
            continue
        j = judge.get(k["item_id"])
        if j is None:
            continue
        jp = j.get("decision") == "PASS"
        lp = bool(row["label"]["publishable"])
        table[f"judge_{'pass' if jp else 'fail'}_pub_{'Y' if lp else 'N'}"] += 1
        ju = int(j.get("n_unsupported_claims") or 0) >= 1
        le = _n_errors(row) >= 1
        err_table[f"judge_unsupported_{'ge1' if ju else '0'}_err_{'Y' if le else 'N'}"] += 1
        pairs_pub.append((jp, lp))
        pairs_err.append((ju, le))
        ids.append(oid)
    return {"judge_decision_vs_publishable": {"table": table, **agreement(pairs_pub, ids)},
            "judge_unsupported_vs_fact_error": {"table": err_table, **agreement(pairs_err, ids)},
            "note": "judge v2 shadow(첫 초안, 기본 임계값)와 자동 라벨러(런 a). 둘 다 Gemini - 같은 계열이고 어느 쪽도 정답이 아니다."}


def cluster_evaluator_comparison(a_cl, nodes) -> Dict[str, Any]:
    first = {}
    for n in nodes:
        if n["node"] == "eval_cluster" and n.get("call_parsed"):
            first.setdefault(n["item_id"], n)
    pairs, ids = [], []
    table = {"pass_single_Y": 0, "pass_single_N": 0, "fail_single_Y": 0, "fail_single_N": 0}
    for i, row in sorted(a_cl.items()):
        e = first.get(i)
        if e is None:
            continue
        p = e.get("decision") == "PASS"
        s = bool(row["label"]["single_event"])
        table[f"{'pass' if p else 'fail'}_single_{'Y' if s else 'N'}"] += 1
        pairs.append((p, s))
        ids.append(i)
    return {"first_cluster_eval_vs_single_event": {"table": table, **agreement(pairs, ids)},
            "note": "ClusterEvaluator(judge 모델, 첫 판정)와 자동 라벨러(런 a). 같은 계열."}


def by_bucket(a_cl, a_out, pkg: al.Package, ends) -> Dict[str, Any]:
    res: Dict[str, Any] = {}
    for name, _, _ in SIZE_BUCKETS:
        items = [i for i, c in pkg.clusters.items() if bucket_of(len(c["articles"])) == name]
        outs = {o["output_id"]: a_out[o["output_id"]] for o in pkg.outputs if o["item_id"] in items and o["output_id"] in a_out}
        cls = {i: a_cl[i] for i in items if i in a_cl}
        saved = [i for i in items if str((ends.get(i) or {}).get("outcome", "")).split(":")[0] == "completed"]
        res[name] = {"clusters": len(items), "clusters_labeled": len(cls), "saved": len(saved),
                     "single_event": ratio(sum(1 for r in cls.values() if r["label"]["single_event"]), len(cls)),
                     "outputs_labeled": len(outs),
                     "publishable": ratio(sum(1 for r in outs.values() if r["label"]["publishable"]), len(outs)),
                     "fact_error_ge1": ratio(sum(1 for r in outs.values() if _n_errors(r) >= 1), len(outs))}
    return res


def call_stats(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    by = {}
    for scope, rows in (("preflight", [c for c in calls if c.get("run") == "preflight"]),
                        ("labeling", [c for c in calls if c.get("run") != "preflight"])):
        u = [c.get("usage") or {} for c in rows if c.get("status") == 200]
        by[scope] = {
            "http_attempts": len(rows),
            "status": dict(sorted(Counter(str(c.get("status")) for c in rows).items())),
            "transport_errors": dict(sorted(Counter(c.get("transport_error") for c in rows if c.get("transport_error")).items())),
            "outcome": dict(sorted(Counter(c.get("outcome") for c in rows if c.get("outcome")).items())),
            "finish_reason": dict(sorted(Counter(c.get("finish_reason") for c in rows if c.get("finish_reason")).items())),
            "tokens": {"prompt": sum(x.get("prompt") or 0 for x in u), "candidates": sum(x.get("candidates") or 0 for x in u),
                       "thoughts": sum(x.get("thoughts") or 0 for x in u), "cached": sum(x.get("cached") or 0 for x in u),
                       "total": sum(x.get("total") or 0 for x in u)},
            "cost_usd": round(sum(c.get("cost_usd") or 0 for c in rows), 6),
            "latency_s": dist(c.get("latency_s") for c in rows if c.get("status") == 200),
            "by_kind": {k: {"attempts": sum(1 for c in rows if c.get("kind") == k),
                            "cost_usd": round(sum(c.get("cost_usd") or 0 for c in rows if c.get("kind") == k), 6)}
                        for k in al.KINDS},
        }
    by["model_versions"] = sorted({c.get("model_version") for c in calls if c.get("model_version")})
    return by


def unit_stats(units: List[Dict[str, Any]]) -> Dict[str, Any]:
    ends = [u for u in units if u["event"] == "end"]
    res: Dict[str, Any] = {}
    for run in ("a", "b"):
        res[run] = {k: dict(sorted(Counter(u["status"] for u in ends if u["run"] == run and u["kind"] == k).items()))
                    for k in al.KINDS}
    res["repaired_then_labeled"] = sum(1 for u in ends if u["status"] == "labeled" and int(u.get("responses") or 1) > 1)
    res["violation_codes"] = dict(sorted(Counter(v for u in ends if u["status"] != "labeled" for v in u.get("violations") or []).items()))
    return res


# ---------------------------------------------------------------- 조립


def build(proto: al.Protocol, pkg: al.Package, *, items: List[Dict[str, Any]], ends: Dict[str, Dict[str, Any]],
          nodes: List[Dict[str, Any]], spend: Optional[Dict[str, Any]] = None, command: str = "",
          now: Optional[datetime] = None) -> Tuple[Dict[str, Any], al.LeakGuard]:
    from evaluation.llm import e0_labels

    store_root = pkg.labels_dir / "auto" / proto.id
    read = lambda rel: al._read_jsonl(store_root / rel)  # noqa: E731
    a_cl = {r["target_id"]: r for r in read("run-a/cluster_labels.jsonl")}
    b_cl = {r["target_id"]: r for r in read("run-b/cluster_labels.jsonl")}
    a_out = {r["target_id"]: r for r in read("run-a/output_labels.jsonl")}
    b_out = {r["target_id"]: r for r in read("run-b/output_labels.jsonl")}
    calls, units = read("calls.jsonl"), read("units.jsonl")
    manifest = json.loads((store_root / "manifest.json").read_text(encoding="utf-8")) if (store_root / "manifest.json").exists() else {}
    key = json.loads((pkg.run_dir / "blind_key.json").read_text(encoding="utf-8"))["outputs"]
    metrics_a = e0_labels.label_metrics(pkg.labels_dir, pkg.run_dir, items, ends, {}, label_files_dir=store_root / "run-a")
    if "checker_vs_human_first_draft" in metrics_a:
        chk = metrics_a.pop("checker_vs_human_first_draft")
        chk["note"] = ("A8.8 쓰임 3의 계산을 자동 라벨(런 a)에 그대로 한 값이다. 사람 정답이 아니므로 '정밀도·재현율'이 아니라 "
                       "검사기 차단 구간 중 라벨러 구간과 겹친 비율(precision 칸), 라벨러 number·quote 구간 중 검사기와 겹친 비율"
                       "(recall_* 칸)로 읽는다.")
        metrics_a["checker_vs_auto_labeler_first_draft"] = chk
    metrics_a.pop("relabel", None)  # 사람 재라벨 칸 - 자동 라벨에서는 run_agreement가 대신한다
    runs = manifest.get("runs") or []
    report = {
        "kind": "e0_auto_labels", "format": 1, "generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "header": {
            "command": command, "head_sha": _git_head(), "protocol": proto.id,
            "protocol_sha256": al.sha256_file(al.PROTOCOL_PATH), "adr": proto.doc["adr"],
            "e0_run": pkg.run_dir.name, "package_expires_at_utc": pkg.retention["expires_at_utc"],
            "labeler_model": proto.model, "labeler_model_versions_seen": sorted({c.get("model_version") for c in calls
                                                                                  if c.get("model_version")}),
            "generator_model": proto.labeler["not_generator"], "judge_v2_model": proto.labeler["not_judge_v2"],
            "seeds": proto.labeler["seeds"], "temperature": proto.labeler["temperature"],
            "thinking_level": proto.labeler["thinking_level"], "article_body_chars": proto.chars,
            "schema_mode_used": manifest.get("schema_mode"), "thinking_spelling_used": manifest.get("thinking_spelling"),
            "preflight": {k: v.get("status") for k, v in ((manifest.get("preflight") or {}).get("results") or {}).items()},
            "preflight_changes": (manifest.get("preflight") or {}).get("changes"),
            "cost_cap_usd": proto.doc["budget"]["cap_usd"], "budget_env": proto.doc["budget"]["env"],
            "in_client_spend": spend, "invocations": [{"at": r.get("at"), "stop": r.get("stop"),
                                                       "result": r.get("result"), "spent_run_usd": r.get("spent_run_usd")}
                                                      for r in runs],
            "human_labels": "없음 - 사용자 결정(2026-10-17, ADR 0009 A8.13 (a))",
            "results_viewed": "A8.13 커밋까지 자동 라벨 0건. 이 리포트가 자동 라벨의 첫 집계다",
        },
        "calls": call_stats(calls),
        "units": unit_stats(units),
        "denominators": {"n_p": len(e0_labels.processed_eval(items, ends)), "clusters_in_package": len(pkg.clusters),
                         "outputs_in_package": len(pkg.outputs),
                         "outputs_with_saved_conversion": len(pkg.conversions),
                         "relabel_targets": {"clusters": pkg.relabel.get("clusters"), "outputs": pkg.relabel.get("outputs")}},
        "run_a": {"clusters": cluster_stats(a_cl), "outputs": output_stats(a_out), "a8_8_uses": metrics_a},
        "run_b": {"clusters": cluster_stats(b_cl), "outputs": output_stats(b_out)},
        "run_a_vs_run_b": run_agreement(a_cl, b_cl, a_out, b_out),
        "labeler_vs_judge_v2": judge_comparison(a_out, key, nodes),
        "labeler_vs_cluster_evaluator": cluster_evaluator_comparison(a_cl, nodes),
        "by_size_bucket_run_a": by_bucket(a_cl, a_out, pkg, ends),
        "rows": {
            "clusters": [{"item_id": i, "n_articles": len(pkg.clusters[i]["articles"]),
                          "bucket": bucket_of(len(pkg.clusters[i]["articles"])),
                          "outcome": (ends.get(i) or {}).get("outcome"),
                          "a_single_event": (a_cl.get(i) or {}).get("label", {}).get("single_event"),
                          "a_n_outliers": len((a_cl.get(i) or {}).get("label", {}).get("outlier_ids") or []),
                          "a_n_key_facts": len((a_cl.get(i) or {}).get("label", {}).get("key_facts") or []),
                          "b_single_event": (b_cl.get(i) or {}).get("label", {}).get("single_event")}
                         for i in sorted(pkg.clusters)],
            "outputs": [{"output_id": o["output_id"], "item_id": o["item_id"],
                         **{f"{r}_{k}": v for r, rows in (("a", a_out), ("b", b_out)) if o["output_id"] in rows
                            for k, v in (("publishable", rows[o["output_id"]]["label"]["publishable"]),
                                         ("publishable_raw", rows[o["output_id"]]["auto"]["publishable_raw"]),
                                         ("n_fact_errors", _n_errors(rows[o["output_id"]])),
                                         ("style", rows[o["output_id"]]["label"]["style"]),
                                         ("n_covered", len(rows[o["output_id"]]["label"]["key_facts_covered"])),
                                         ("n_facts", rows[o["output_id"]]["auto"].get("coverage_n_facts")),
                                         ("tone_drift", rows[o["output_id"]]["label"]["tone_drift"]
                                          if rows[o["output_id"]]["auto"].get("tone_drift_applicable") else None))}}
                        for o in sorted(pkg.outputs, key=lambda o: o["output_id"])],
        },
        "not_computable": NOT_COMPUTABLE,
        "caveats": CAVEATS,
    }
    guard = al.LeakGuard().add_package(pkg).add_responses(read("rationales.jsonl"))
    for rows in (a_cl, b_cl, a_out, b_out):
        guard.add_responses({"response": r} for r in rows.values())
    return report, guard


def _r(x: Dict[str, Any]) -> str:
    if not isinstance(x, dict) or "k" not in x:
        return json.dumps(x, ensure_ascii=False)
    if not x["n"]:
        return f"0/0 (정의 안 됨)"
    ci = x.get("wilson95")
    s = f"{x['k']}/{x['n']} = {x['rate']:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]"
    if "kappa_reference_only" in x:
        s += f", κ(참고) {x['kappa_reference_only']}" if x["kappa_reference_only"] is not None else ", κ 정의 안 됨"
    if x.get("disagreement_ids"):
        s += f", 불일치 {', '.join(x['disagreement_ids'])}"
    return s


def render_md(rep: Dict[str, Any]) -> str:
    h = rep["header"]
    a, b = rep["run_a"], rep["run_b"]
    ag = rep["run_a_vs_run_b"]
    u = a["a8_8_uses"]
    lines = [
        "# E0 자동 라벨 리포트 (e0-auto-labels-v1)", "",
        "E0 기준선(ADR 0009 A8)의 사람 라벨 자리를 자동 라벨러가 채운 기술 통계다(사용자 결정 2026-10-17, A8.13). **사람 라벨은 없다.** "
        "모든 수치는 자동 라벨러(LLM)의 판단이고 그 정확도는 재지 않았다. 비율에 Wilson 95% CI와 분모를 적는다. 기사·초안·라벨러가 쓴 "
        "텍스트는 싣지 않는다(id·라벨 값·건수만).", "",
        "## 머리말", "", "| 항목 | 값 |", "|---|---|",
    ]
    for k in ("command", "head_sha", "protocol", "protocol_sha256", "adr", "e0_run", "labeler_model", "labeler_model_versions_seen",
              "generator_model", "judge_v2_model", "seeds", "temperature", "thinking_level", "article_body_chars", "schema_mode_used",
              "thinking_spelling_used", "preflight", "preflight_changes", "cost_cap_usd", "package_expires_at_utc", "human_labels",
              "results_viewed"):
        lines.append(f"| {k} | {json.dumps(h.get(k), ensure_ascii=False)} |")
    spend = h.get("in_client_spend") or {}
    lines.append(f"| in_client_spend(run) | {json.dumps({k: spend.get(k) for k in ('run_id', 'run_usd', 'day_usd', 'total_usd')}, ensure_ascii=False)} |")
    c = rep["calls"]
    lines += ["", "## 1. 호출과 비용", "",
              f"- 라벨링 HTTP 시도 {c['labeling']['http_attempts']}건, 상태 {json.dumps(c['labeling']['status'])}, "
              f"전송 오류 {json.dumps(c['labeling']['transport_errors'])}, 결과 {json.dumps(c['labeling']['outcome'])}, "
              f"종료 사유 {json.dumps(c['labeling']['finish_reason'])}",
              f"- pre-flight {c['preflight']['http_attempts']}건, ${c['preflight']['cost_usd']:.6f}",
              f"- 비용(라벨러 단가로 계산): 라벨링 ${c['labeling']['cost_usd']:.6f} + pre-flight ${c['preflight']['cost_usd']:.6f}; "
              f"가드 원장의 이 런 합 ${spend.get('run_usd')} (상한 ${h['cost_cap_usd']})",
              f"- 토큰(라벨링): {json.dumps(c['labeling']['tokens'])}. 응답이 밝힌 캐시 입력(cached)도 정가로 계산했다"
              "(ADR 0035의 정산 규칙) - 실제 청구는 이보다 적을 수 있다",
              f"- 종류별: {json.dumps(c['labeling']['by_kind'])}",
              f"- 응답 모델 버전: {', '.join(c['model_versions'])}",
              f"- 지연(초, 200 응답): {json.dumps(c['labeling']['latency_s'])}",
              f"- 실행: {json.dumps(h['invocations'], ensure_ascii=False)}", "",
              "## 2. 라벨 단위", "",
              f"- 런 a: {json.dumps(rep['units']['a'], ensure_ascii=False)}",
              f"- 런 b: {json.dumps(rep['units']['b'], ensure_ascii=False)}",
              f"- 다시 물어 라벨이 된 단위 {rep['units']['repaired_then_labeled']}, 라벨이 안 된 단위의 위반 코드 "
              f"{json.dumps(rep['units']['violation_codes'], ensure_ascii=False)}",
              f"- 분모: {json.dumps(rep['denominators'], ensure_ascii=False)}", "",
              "## 3. 런 a - 클러스터 라벨", "",
              f"- 라벨 {a['clusters']['n_clusters_labeled']}개, single_event {_r(a['clusters']['single_event'])}, 이상치 있음 "
              f"{_r(a['clusters']['with_outliers'])}, 주된 사건 없음 {a['clusters']['no_main_event']}",
              f"- 핵심 사실 수 {json.dumps(a['clusters']['key_facts_per_cluster'])}, 이상치 수 {json.dumps(a['clusters']['outliers_per_cluster'])}",
              f"- 파이프라인 결과 × single_event(A8.8 쓰임 2): {json.dumps(u.get('outcome_by_single_event'), ensure_ascii=False)}",
              f"- ClusterEvaluator 첫 판정 × single_event: {_r(rep['labeler_vs_cluster_evaluator']['first_cluster_eval_vs_single_event'])}, "
              f"표 {json.dumps(rep['labeler_vs_cluster_evaluator']['first_cluster_eval_vs_single_event']['table'])}", "",
              "## 4. 런 a - 출력 라벨", "",
              f"- 발행 가능률(A8.8 쓰임 1, 자동 라벨): 주 지표(저장본 Y / n_p) {_r(u.get('publishable_primary'))}; 저장본만 "
              f"{_r(u.get('publishable_saved_only'))}; 첫 초안만 {_r(u.get('publishable_first_draft'))}",
              f"- 라벨러 자신의 publishable(규칙 합성 전) {_r(a['outputs']['publishable_labeler_raw'])}; 지켜지지 않은 규칙별 출력 수"
              f"(한 출력이 여러 규칙에 걸릴 수 있다, A8.13 (b)6) {json.dumps(a['outputs']['rule_made_no'], ensure_ascii=False)}",
              f"- 사실 오류 ≥1 {_r(a['outputs']['fact_error_ge1'])}; 출력당 {json.dumps(a['outputs']['fact_errors_per_output'])}; 유형 "
              f"{json.dumps(a['outputs']['fact_error_types'])}; 위치 못 찾음 {a['outputs']['unlocated_fact_errors']}, 여러 위치(첫 위치 사용) "
              f"{a['outputs']['ambiguous_spans_first_occurrence_used']}",
              f"- style {json.dumps(a['outputs']['style'])}; 제목·한줄소개 불일치 {_r(a['outputs']['headline_mismatch'])}",
              f"- 핵심 사실 커버리지(사실 단위) {_r(a['outputs']['key_fact_coverage_micro'])}; 기준 {json.dumps(a['outputs']['coverage_basis'])}",
              f"- tone_drift(의미 기준, 저장 변환본) {_r(a['outputs']['tone_drift'])}, 대상 아님 {a['outputs']['tone_drift_not_applicable']}",
              f"- 결정론 검사기 대 자동 라벨(첫 초안, A8.8 쓰임 3의 계산 - 정답 대비가 아니다): "
              f"{json.dumps(u.get('checker_vs_auto_labeler_first_draft'), ensure_ascii=False)}",
              f"- 저장본: {json.dumps(u.get('saved'), ensure_ascii=False)}", "",
              "## 5. 런 a 대 런 b (자기 일관성 - A8.8 재라벨 자리)", "",
              f"- 둘 다 있는 것: 클러스터 {ag['n_clusters_both']}, 출력 {ag['n_outputs_both']}",
              f"- single_event {_r(ag['single_event'])}",
              f"- 이상치 집합 완전 일치 {_r(ag['outlier_set_exact'])}; 주된 사건 유무 {_r(ag['main_event_present'])}",
              f"- publishable {_r(ag['publishable'])}; 라벨러 자신의 publishable {_r(ag['publishable_labeler_raw'])}",
              f"- 사실 오류 유무 {_r(ag['fact_error_present'])}; 오류 수 같음 {_r(ag['fact_error_count_equal'])}",
              f"- style 정확 {_r(ag['style_exact'])}; ±1 {_r(ag['style_within_1'])}",
              f"- 제목 불일치 {_r(ag['headline_mismatch'])}; tone_drift {_r(ag['tone_drift'])}",
              f"- 커버리지(같은 핵심 사실, 사실 단위) {_r(ag['coverage_per_fact_same_facts'])}",
              f"- {ag['note']}", "",
              "## 6. 라벨러 대 judge v2 shadow", "",
              f"- judge 판정 × publishable: {_r(rep['labeler_vs_judge_v2']['judge_decision_vs_publishable'])}, 표 "
              f"{json.dumps(rep['labeler_vs_judge_v2']['judge_decision_vs_publishable']['table'])}",
              f"- judge 근거 없는 주장 ≥1 × 라벨러 사실 오류 ≥1: {_r(rep['labeler_vs_judge_v2']['judge_unsupported_vs_fact_error'])}, 표 "
              f"{json.dumps(rep['labeler_vs_judge_v2']['judge_unsupported_vs_fact_error']['table'])}",
              f"- {rep['labeler_vs_judge_v2']['note']}", "",
              "## 7. 크기 버킷별(런 a)", ""]
    for name, x in rep["by_size_bucket_run_a"].items():
        lines.append(f"- {name}: 클러스터 {x['clusters']}(저장 {x['saved']}), single_event {_r(x['single_event'])}, 출력 "
                     f"{x['outputs_labeled']}, publishable {_r(x['publishable'])}, 사실 오류 ≥1 {_r(x['fact_error_ge1'])}")
    lines += ["", "## 8. 런 b 요약", "",
              f"- 클러스터 {b['clusters']['n_clusters_labeled']}, single_event {_r(b['clusters']['single_event'])}",
              f"- 출력 {b['outputs']['n_outputs_labeled']}, publishable {_r(b['outputs']['publishable'])}, 사실 오류 ≥1 "
              f"{_r(b['outputs']['fact_error_ge1'])}", "",
              "## 9. 행(id·라벨 값만)", "", "| item | 기사 | 버킷 | outcome | a single_event | a 이상치 | a 핵심 사실 | b single_event |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]["clusters"]:
        lines.append(f"| {r['item_id']} | {r['n_articles']} | {r['bucket']} | {r['outcome']} | {r['a_single_event']} | {r['a_n_outliers']} | "
                     f"{r['a_n_key_facts']} | {r['b_single_event']} |")
    lines += ["", "| output | item | a publishable | a 라벨러 Y | a 오류 | a style | a 커버 | b publishable | b 오류 | b style |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]["outputs"]:
        cov = f"{r.get('a_n_covered')}/{r.get('a_n_facts')}" if "a_n_covered" in r else ""
        lines.append(f"| {r['output_id']} | {r['item_id']} | {r.get('a_publishable')} | {r.get('a_publishable_raw')} | "
                     f"{r.get('a_n_fact_errors')} | {r.get('a_style')} | {cov} | {r.get('b_publishable', '')} | "
                     f"{r.get('b_n_fact_errors', '')} | {r.get('b_style', '')} |")
    lines += ["", "## 계산하지 못한 것(측정하지 않음)", ""]
    lines += [f"- {x['what']} - {x['why']}" for x in rep["not_computable"]]
    lines += ["", "## 주의", ""] + [f"- {x}" for x in rep["caveats"]] + [""]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from evaluation.llm import e0_registered as reg
    from evaluation.llm import e0_sample

    parser = argparse.ArgumentParser(description="E0 자동 라벨 리포트 (ADR 0009 A8.13)")
    parser.add_argument("--out", default=str(al.REPO_ROOT / REPORT_REL))
    args = parser.parse_args(argv)
    al._quiet()
    proto = al.load_protocol()
    labels_dir, run_dir = al._paths(proto)
    al.purge_expired(labels_dir, proto.id)
    pkg = al.load_package(labels_dir, run_dir)
    items = e0_sample.read_manifest(reg.REPO_ROOT / reg.SAMPLE_MANIFEST_REL)
    ends = {r["item_id"]: r for r in al._read_jsonl(run_dir / "clusters.jsonl") if r["kind"] == "end"}
    nodes = al._read_jsonl(run_dir / "nodes.jsonl")
    al.configure_budget(proto)
    from core.llm import budget

    snap = budget.spend_snapshot()
    rep, guard = build(proto, pkg, items=items, ends=ends, nodes=nodes,
                       spend={k: snap.get(k) for k in ("run_id", "run_usd", "day_usd", "total_usd", "caps_usd", "open_reservations")},
                       command="python -m evaluation.llm.e0_auto_label_report")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    guard.write_text(out.with_suffix(".json"), json.dumps(rep, ensure_ascii=False, indent=1) + "\n")
    guard.write_text(out.with_suffix(".md"), render_md(rep))
    print(json.dumps({"written": [str(out.with_suffix(".json")), str(out.with_suffix(".md"))]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
