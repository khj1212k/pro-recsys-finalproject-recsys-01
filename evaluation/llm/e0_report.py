"""E0 리포트 - run 기록에서 A8.3의 지표 전부를 계산해 reports/llm/e0_baseline_v1.{json,md}를 쓴다 (ADR 0009 A8.3·A8.7).

입력은 `data/experiments/e0/<run>/`의 기록(http_attempts·calls·nodes·clusters·ledger·newsletters·run_*)과 동결 반출본이다.
리포트에는 id·sha256·건수·비율만 싣는다(ADR 0023, reports/README.md). 쓰기 전에 **누출 검사**를 한다: 반출본의 제목·본문과
생성 초안(newsletters.jsonl)의 어떤 16자 조각도 리포트 JSON·MD에 나타나면 쓰지 않는다.

모든 비율에 Wilson 95% CI와 분모를 붙인다. 가설 검정은 없다. 비율의 분모는 처리 완료 eval 클러스터(자연 종료:
completed / skipped / failed / error) 수 n_p다. 중단·미시작은 분모에서 빼고 건수와 비용을 따로 적는다.

    python -m evaluation.llm.e0_report --run data/experiments/e0/<run> --export-dir data/exports/<T0> \
        [--balance-before 8000 --balance-before-at 2026-10-20T21:00+09:00 --balance-after 7300 \
         --balance-after-at 2026-10-21T22:10+09:00 --billing-fx 1400 --other-gemini-calls no] [--label-hours 4.1]
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from evaluation.llm import e0_registered as reg

REPORT_JSON = "e0_baseline_v1.json"
REPORT_MD = "e0_baseline_v1.md"
SHINGLE = 16
PURPOSES = ("cluster_eval", "newsletter_content_gen", "newsletter_meta_gen", "newsletter_eval", "tone_convert")
STYLE = {
    "current": {"content_chars": (1000, 1500), "min_paragraphs": 4, "title_max_chars": 15,
                "banned": ("충격", "경악", "논란", "파문", "결국", "드디어", "급기야"), "min_keywords": 5},
    "v2": {"content_chars": (600, 900), "paragraphs": 3, "title_max_chars": 20, "sentence_chars": (30, 45),
           "banned_first_words": ("최근", "요즘", "오늘날", "현재", "지금"),
           "emotive": ("충격", "경악", "논란", "파문", "결국", "드디어", "급기야", "벼락"), "max_emoji": 3},
}
ALLOWED_CATEGORIES = ("정치", "경제", "사회", "세계", "IT/과학", "생활/문화", "스포츠")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F\U0001F1E6-\U0001F1FF]")


class LeakError(RuntimeError):
    """리포트에 기사·초안 텍스트 조각이 들어 있다. 쓰지 않는다."""


# ---------------------------------------------------------------- 통계


def wilson(k: int, n: int, z: float = 1.96) -> Optional[List[float]]:
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def ratio(k: int, n: int) -> Dict[str, Any]:
    return {"k": int(k), "n": int(n), "rate": round(k / n, 4) if n else None, "wilson95": wilson(k, n)}


def dist(values: Iterable[Optional[float]]) -> Dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"n": 0, "values": [], "median": None, "max": None, "sum": 0.0}
    return {"n": len(vals), "values": [round(v, 4) for v in vals], "median": round(statistics.median(vals), 4),
            "max": round(max(vals), 4), "sum": round(sum(vals), 6)}


def _read(path: Path) -> List[Dict[str, Any]]:
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_json(path: Path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


# ---------------------------------------------------------------- 실패 분류 (A8.3 #13)


CALL_ERROR_TYPE = {
    "transport": "transport", "deadline_transport": "transport", "deadline_schema": "schema", "schema": "schema",
    "length": "length", "content_filter": "content_filter", "other": "transport",
}
_TYPE_PRIORITY = ("transport", "length", "content_filter", "schema")


def call_failure_type(call: Optional[Dict[str, Any]]) -> Optional[str]:
    """complete() 행 하나의 실패 유형(transport/length/content_filter/schema) 또는 None(성공)."""
    if not call or call.get("parsed"):
        return None
    error = call.get("error")
    if error is None:
        return None
    if error.startswith("http_"):
        return "transport"
    return CALL_ERROR_TYPE.get(error, "transport")


def _last(calls: Sequence[Dict[str, Any]], purpose: str, attempt: Optional[int] = None) -> Optional[Dict[str, Any]]:
    picked = [c for c in calls if c.get("purpose") == purpose and (attempt is None or c.get("graph_attempt") == attempt)]
    return max(picked, key=lambda c: c.get("call_seq") or 0) if picked else None


def root_cause(end: Dict[str, Any], calls: Sequence[Dict[str, Any]]) -> Optional[str]:
    """위에서부터 먼저 맞는 것 하나(A8.3 #13). 미시작은 None."""
    outcome = str(end.get("outcome") or "")
    if outcome == "aborted_infra":
        return "aborted_infra"
    if outcome == "aborted_budget":
        return "aborted_budget"
    if outcome == "interrupted":
        return "interrupted"
    if outcome == "not_started":
        return None
    if outcome == "error":
        return "exception"
    if outcome == "failed:save_error":
        return "save_error"
    reason = outcome.split(":", 1)[1] if outcome.startswith("failed:") else None
    stage_calls: List[Optional[Dict[str, Any]]] = []
    if outcome == "skipped":
        stage_calls = [_last(calls, "cluster_eval")]
    elif reason == "generator_fallback":
        attempt = max([c.get("graph_attempt") or 0 for c in calls if c.get("purpose") == "newsletter_content_gen"],
                      default=None)
        stage_calls = [_last(calls, "newsletter_content_gen", attempt), _last(calls, "newsletter_meta_gen", attempt)]
    elif reason == "judge_unavailable":
        stage_calls = [_last(calls, "newsletter_eval")]
    types = {t for t in (call_failure_type(c) for c in stage_calls) if t}
    for t in _TYPE_PRIORITY:
        if t in types:
            return t
    if outcome == "completed":
        return "none"
    if outcome == "skipped":
        return "cluster_eval_fail"
    return {"faithfulness": "faithfulness_block", "judge": "judge_fail", "generator_fallback": "generator_fallback",
            "judge_unavailable": "judge_unavailable"}.get(reason or "", "exception")


def cluster_events(calls: Sequence[Dict[str, Any]], http: Sequence[Dict[str, Any]], end: Dict[str, Any]) -> Dict[str, int]:
    """클러스터 안에서 일어난 유형별 사건 건수(A8.3 #13 끝)."""
    c: Counter = Counter()
    for h in http:
        status = h.get("status")
        if status == 429:
            c["http_429"] += 1
        elif status is not None and status >= 500:
            c["http_5xx"] += 1
        elif status is not None and status >= 400:
            c[f"http_{status}"] += 1
        if h.get("transport_error"):
            c[f"transport_{h['transport_error']}"] += 1
        if h.get("finish_reason") in ("length", "content_filter"):
            c[f"finish_{h['finish_reason']}"] += 1
    for call in calls:
        c["schema_failures"] += int(call.get("schema_failures") or 0)
        t = call_failure_type(call)
        if t:
            c[f"call_failed_{t}"] += 1
        if str(call.get("error") or "").startswith("deadline"):
            c["deadline"] += 1
    c["tone_local_fallback"] += int(end.get("tone_local_fallbacks") or 0)
    return dict(sorted((k, v) for k, v in c.items() if v))


# ---------------------------------------------------------------- 규칙 위반(shadow)


def paragraphs(text: str) -> int:
    return sum(1 for block in (text or "").split("\n") if block.strip())


def style_metrics(doc: Dict[str, Any], keywords: Optional[int], invalid_categories: Optional[int]) -> Dict[str, Any]:
    title, sentence, content = (str(doc.get(k) or "") for k in ("title", "sentence", "content"))
    cur, v2 = STYLE["current"], STYLE["v2"]
    banned = sum(text.count(w) for w in cur["banned"] for text in (title, sentence, content))
    emotive = sum(text.count(w) for w in v2["emotive"] for text in (title, sentence, content))
    first = (content.split() or [""])[0]
    emoji = len(_EMOJI.findall(title + sentence + content))
    raw = {"content_chars": len(content), "paragraph_count": paragraphs(content), "title_chars": len(title),
           "sentence_chars": len(sentence), "parsed_keyword_count": keywords,
           "parsed_invalid_category_count": invalid_categories, "banned_count": banned, "emotive_count": emotive,
           "emoji_count": emoji}
    lo, hi = cur["content_chars"]
    current = {"content_chars_out": not lo <= len(content) <= hi, "paragraphs_lt_4": raw["paragraph_count"] < 4,
               "title_gt_15": len(title) > cur["title_max_chars"], "banned_ge_1": banned >= 1,
               "keywords_lt_5": None if keywords is None else keywords < cur["min_keywords"],
               "invalid_category_ge_1": None if invalid_categories is None else invalid_categories >= 1}
    lo2, hi2 = v2["content_chars"]
    slo, shi = v2["sentence_chars"]
    v2rules = {"content_chars_out": not lo2 <= len(content) <= hi2, "paragraphs_ne_3": raw["paragraph_count"] != 3,
               "title_gt_20": len(title) > v2["title_max_chars"], "sentence_chars_out": not slo <= len(sentence) <= shi,
               "banned_first_word": any(first.startswith(w) for w in v2["banned_first_words"]),
               "emotive_ge_1": emotive >= 1, "emoji_gt_3": emoji > v2["max_emoji"]}
    return {"raw": raw, "current": current, "v2": v2rules}


def _rule_rates(rows: Sequence[Dict[str, Any]], group: str) -> Dict[str, Any]:
    keys = sorted({k for r in rows for k in r[group]})
    out = {}
    for key in keys:
        vals = [r[group][key] for r in rows if r[group][key] is not None]
        out[key] = ratio(sum(1 for v in vals if v), len(vals))
    return out


# ---------------------------------------------------------------- 리포트


def _bucket(n: int) -> str:
    return "3-4" if n <= 4 else "5-9" if n <= 9 else "10+"


def _near_dup_pairs(titles_by_press: Dict[str, List[str]]) -> int:
    from rapidfuzz import fuzz

    pairs = 0
    for titles in titles_by_press.values():
        for i in range(len(titles)):
            for j in range(i + 1, len(titles)):
                if fuzz.ratio(titles[i], titles[j]) >= 85:
                    pairs += 1
    return pairs


def balance_reconciliation(ledger_usd: float, balance: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """선불 잔액 대조(A8.7). 잔액은 사람이 결제 화면에서 읽어 넘긴다."""
    base = {"ledger_sum_usd": round(ledger_usd, 6),
            "unknowns": ["잔액 표시 단위", "반영 지연", "VAT 포함 여부"],
            "rule": "차이가 max($0.10, 원장 합의 15%)를 넘으면 '원장 불일치'(비용 수치 신뢰 불가, void 아님)"}
    if not balance or balance.get("before") is None or balance.get("after") is None or not balance.get("fx"):
        return {**base, "status": "미기록", "note": "실행 전·마지막 호출 24시간 뒤의 잔액을 읽어 다시 만든다"}
    delta_krw = float(balance["before"]) - float(balance["after"])
    delta_usd = delta_krw / float(balance["fx"])
    threshold = max(0.10, 0.15 * ledger_usd)
    if balance.get("other_gemini_calls"):
        status = "대조 불가"
    else:
        status = "원장 불일치" if abs(delta_usd - ledger_usd) > threshold else "일치(허용 범위 안)"
    return {**base, "status": status, "balance_before_krw": balance["before"], "balance_before_at": balance.get("before_at"),
            "balance_after_krw": balance["after"], "balance_after_at": balance.get("after_at"),
            "billing_fx_krw_per_usd": balance["fx"], "delta_krw": delta_krw, "delta_usd": round(delta_usd, 6),
            "abs_diff_usd": round(abs(delta_usd - ledger_usd), 6), "threshold_usd": round(threshold, 6),
            "other_gemini_calls": bool(balance.get("other_gemini_calls"))}


def build_report(run_dir: Path, *, prereg: Dict[str, Any], manifest_path: Path, export_dir: Optional[Path] = None,
                 balance: Optional[Dict[str, Any]] = None, label_hours: Optional[float] = None,
                 labels_dir: Optional[Path] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    from evaluation.llm import e0_instrument as ins
    from evaluation.llm import e0_sample

    run_dir = Path(run_dir)
    http = _read(run_dir / ins.HTTP_FILE)
    calls = _read(run_dir / ins.CALLS_FILE)
    nodes = _read(run_dir / ins.NODES_FILE)
    clusters = _read(run_dir / ins.CLUSTERS_FILE)
    texts = _read(run_dir / "newsletters.jsonl")
    state = _load_json(run_dir / "run_state.json", {}) or {}
    manifest = _load_json(run_dir / "run_manifest.json", {}) or {}
    summary = _load_json(run_dir / "run_summary.json", {}) or {}
    preflight = _load_json(run_dir / "preflight.json", {}) or {}
    items = e0_sample.read_manifest(manifest_path)
    by_item = {r["item_id"]: r for r in items}
    record = prereg.get("pre_run_record") or {}

    ends = {r["item_id"]: r for r in clusters if r["kind"] == "end"}
    key = lambda r: (r.get("run_k"), r.get("cluster_id"))  # noqa: E731
    calls_by = defaultdict(list)
    for c in calls:
        calls_by[key(c)].append(c)
    http_by = defaultdict(list)
    for h in http:
        http_by[key(h)].append(h)
    nodes_by = defaultdict(list)
    for n in nodes:
        nodes_by[n["item_id"]].append(n)

    eval_items = [r for r in items if r["split"] == "eval"]
    warm_items = [r for r in items if r["split"] == "warmup"]
    processed = [r for r in eval_items if ends.get(r["item_id"], {}).get("outcome", "").split(":")[0]
                 in reg.NATURAL_OUTCOMES]
    n_p = len(processed)
    proc_keys = {(r["run_k"], r["cluster_id"]) for r in processed}
    p_calls = [c for c in calls if key(c) in proc_keys]
    p_http = [h for h in http if key(h) in proc_keys]

    # 1. 완주 분포
    outcome_counts = Counter(ends[r["item_id"]]["outcome"] for r in eval_items if r["item_id"] in ends)
    not_proc = [r for r in eval_items if r not in processed]
    m1 = {"outcomes_eval": dict(sorted(outcome_counts.items())),
          "completion_rate": ratio(sum(1 for r in processed if ends[r["item_id"]]["outcome"] == "completed"), n_p),
          "excluded_from_denominator": {o: {"count": sum(1 for r in not_proc if ends.get(r["item_id"], {}).get("outcome") == o),
                                            "cost_usd": round(sum(h["cost_usd"] or 0 for r in not_proc
                                                                  if ends.get(r["item_id"], {}).get("outcome") == o
                                                                  for h in http_by[(r["run_k"], r["cluster_id"])]), 6)}
                                        for o in ("aborted_budget", "aborted_infra", "interrupted", "not_started")},
          "length_bias_caution": bool(not_proc)}

    # 2. 첫 시도 스키마 통과율
    def g3(rows):
        responded = [c for c in rows if c.get("responded")]
        return ratio(sum(1 for c in responded if c.get("first_try_schema_pass")), len(responded))

    m2 = {"overall": g3(p_calls), "by_purpose": {p: g3([c for c in p_calls if c["purpose"] == p]) for p in PURPOSES}}

    # 3. 재시도
    status_counts = Counter(str(h.get("status")) for h in p_http)
    transport = Counter(h["transport_error"] for h in p_http if h.get("transport_error"))
    proc_ends = [ends[r["item_id"]] for r in processed]
    m3 = {
        "attempts_per_call": dist(c.get("attempts") for c in p_calls),
        "http_attempts_per_call": dist(c.get("http_attempts") for c in p_calls),
        "schema_failures_sum": sum(int(c.get("schema_failures") or 0) for c in p_calls),
        "http_status_counts": dict(sorted(status_counts.items())),
        "rate_429": ratio(sum(1 for h in p_http if h.get("status") == 429), len(p_http)),
        "transport_failures": {"timeout": transport.get("timeout", 0), "connection": transport.get("connection", 0),
                               "5xx": sum(1 for h in p_http if (h.get("status") or 0) >= 500),
                               "other": sum(v for k, v in transport.items() if k not in ("timeout", "connection"))},
        "deadline_reached_calls": sum(1 for c in p_calls if str(c.get("error") or "").startswith("deadline")),
        "graph": {
            "generation_attempts_per_cluster": dist(e.get("gen_attempts") for e in proc_ends),
            "cluster_eval_retries_per_cluster": dist(max(0, (e.get("cluster_eval_attempts") or 0) - 1) for e in proc_ends),
            "tone_redo_for_drift_per_cluster": dist(max(0, (e.get("tone_rounds") or 0) - 1) for e in proc_ends
                                                    if e.get("tone_rounds")),
            "tone_validation_retries_per_cluster": dist(max(0, (e.get("tone_validation_calls") or 0) -
                                                            (e.get("tone_rounds") or 0)) for e in proc_ends
                                                        if e.get("tone_rounds")),
        },
    }

    # 4. 지연
    saved = [r for r in processed if ends[r["item_id"]]["outcome"] == "completed"]

    def single_pass(r):
        rows = calls_by[(r["run_k"], r["cluster_id"])]
        total = 0.0
        for p in PURPOSES:
            ok = [c for c in rows if c["purpose"] == p and c.get("parsed")]
            if ok:
                total += float(max(ok, key=lambda c: c["call_seq"])["latency_s"] or 0)
        return total

    m4 = {"call_latency_s_by_purpose": {p: dist(c["latency_s"] for c in p_calls if c["purpose"] == p) for p in PURPOSES},
          "cluster_wall_s": dist(ends[r["item_id"]].get("wall_s") for r in processed),
          "saved_cluster_wall_s": dist(ends[r["item_id"]].get("wall_s") for r in saved),
          "single_pass_latency_sum_s": dist(single_pass(r) for r in saved),
          "note": "n이 작아 p95는 적지 않는다(사실상 최댓값). 뉴스레터 임베딩 시간은 빠져 있다(이탈 6)."}

    # 5. 토큰·비용
    def tokens(rows):
        return {k: sum(int(h.get(k) or 0) for h in rows) for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                                                     "hidden_tokens", "reasoning_tokens_reported")}

    first_attempts = [h for h in p_http if h.get("attempt_seq") == 1 and h.get("prompt_tokens")]
    char_ratio = {p: dist(h["prompt_chars"] / h["prompt_tokens"] for h in first_attempts if h["purpose"] == p)
                  for p in PURPOSES}
    all_cost = round(sum(float(h.get("cost_usd") or 0) for h in http), 6)
    cost_kinds = Counter(h.get("cost_kind") for h in http)
    m5 = {
        "tokens_processed_eval": tokens(p_http),
        "tokens_all_phases": tokens(http),
        "cost_usd": {"processed_eval": round(sum(float(h.get("cost_usd") or 0) for h in p_http), 6),
                     "all_phases_run": all_cost,
                     "preflight": round(sum(float(h.get("cost_usd") or 0) for h in http if h.get("cluster_id") is None), 6),
                     "warmup": round(sum(float(h.get("cost_usd") or 0) for r in warm_items
                                         for h in http_by[(r["run_k"], r["cluster_id"])]), 6),
                     "e0_cumulative": summary.get("cumulative_usd")},
        "cost_kind_counts": dict(sorted((str(k), v) for k, v in cost_kinds.items())),
        "cost_per_call_usd": dist(c.get("cost_usd") for c in p_calls),
        "cost_per_cluster_usd": dist(sum(float(h.get("cost_usd") or 0) for h in http_by[(r["run_k"], r["cluster_id"])])
                                     for r in processed),
        "cost_per_saved_usd": dist(sum(float(h.get("cost_usd") or 0) for h in http_by[(r["run_k"], r["cluster_id"])])
                                   for r in saved),
        "prompt_chars_per_token_first_attempt": char_ratio,
        "models_responded": sorted({str(h.get("model_responded")) for h in http if h.get("model_responded")}),
        "formula": "cost = prompt × 입력 단가 + (completion + hidden) × 출력 단가, hidden = max(0, total − prompt − completion)",
        "in_client_spend_snapshot": summary.get("in_client_spend"),
    }

    # 6. 결정론 사실성 검사기
    faith = [n for n in nodes if n["node"] == "check_faithfulness" and n["item_id"] in {r["item_id"] for r in processed}
             and n.get("reason") != "generator_fallback"]
    first = {}
    last = {}
    for n in faith:
        first.setdefault(n["item_id"], n)
        last[n["item_id"]] = n

    def faith_rates(rows):
        rows = list(rows)
        return {"unsupported_numbers_ge_1": ratio(sum(1 for n in rows if n["blocking"].get("numbers", 0) >= 1), len(rows)),
                "unsupported_quotes_ge_1": ratio(sum(1 for n in rows if n["blocking"].get("quotes", 0) >= 1), len(rows)),
                "advisory_entities_ge_1": ratio(sum(1 for n in rows if n["advisory"].get("entities", 0) >= 1), len(rows))}

    by_field = Counter()
    for n in faith:
        for s in n.get("spans") or []:
            by_field[f"{s['group']}.{s['type']}.{s.get('field')}"] += 1
    m6 = {"per_attempt": [{"item_id": n["item_id"], "attempt": n.get("attempt"), "blocking": n["blocking"],
                           "advisory": n["advisory"], "number_total": n.get("number_total"),
                           "gate_passed": n.get("gate_passed")} for n in faith],
          "by_field": dict(sorted(by_field.items())),
          "first_draft": faith_rates(first.values()), "final_draft": faith_rates(last.values()),
          "regenerations_due_to_block": sum(1 for n in faith if n.get("gate_passed") is False),
          "generator_fallback_drafts": sum(1 for n in nodes if n["node"] == "check_faithfulness"
                                           and n.get("reason") == "generator_fallback")}

    # 7. 문체 드리프트
    drift = [n for n in nodes if n["node"] == "check_tone_drift" and n["item_id"] in {r["item_id"] for r in processed}]
    tone_rounds = [n for n in nodes if n["node"] == "convert_tone" and n["item_id"] in {r["item_id"] for r in processed}
                   and not n.get("skipped")]
    saved_formal = {t["item_id"]: t for t in texts if t.get("kind") == "saved_formal"}
    m7 = {"blocking_by_type": {t: ratio(sum(1 for n in drift if n["blocking"].get(t, 0) >= 1), len(drift))
                               for t in ("numbers", "dates", "entities_added")},
          "formal_fallback_saved": ratio(sum(1 for r in saved if saved_formal.get(r["item_id"], {}).get("tone_fallback")
                                             == "formal"), len(saved)),
          "local_fallback_rounds": ratio(sum(1 for r in processed for _ in range(int(ends[r["item_id"]].get(
              "tone_local_fallbacks") or 0))), len(tone_rounds)),
          "offline_saved_sentence_drift": offline_sentence_drift(saved_formal[r["item_id"]] for r in saved
                                                                 if r["item_id"] in saved_formal)}

    # 8. judge v2 shadow
    judge = [n for n in nodes if n["node"] == "eval_newsletter" and n["item_id"] in {r["item_id"] for r in processed}]
    scored = [n for n in judge if n.get("criteria")]
    claims = sum(int(n.get("n_unsupported_claims") or 0) for n in judge)
    m8 = {"criteria_score_counts": {c: dict(sorted(Counter(str(n["criteria"][c]) for n in scored).items()))
                                    for c in ("faithfulness", "coverage", "coherence", "style")},
          "unsupported_claims": dist(n.get("n_unsupported_claims") for n in scored),
          "pass_rate_default_thresholds": ratio(sum(1 for n in scored if n.get("decision") == "PASS"), len(scored)),
          "judge_unavailable": sum(1 for n in judge if n.get("judge_unavailable")),
          "claim_location_success": ratio(sum(int(n.get("claims_located") or 0) for n in judge), claims),
          "note": "shadow judge는 generator와 같은 계열(Gemini)이다."}

    # 9. 클러스터 평가기
    ce = [n for n in nodes if n["node"] == "eval_cluster" and n["item_id"] in {r["item_id"] for r in processed}]
    fails = [n for n in nodes if n["node"] == "handle_cluster_fail" and n["item_id"] in {r["item_id"] for r in processed}]
    m9 = {"decisions_parsed": dict(sorted(Counter(n.get("decision") for n in ce if n.get("call_parsed")).items())),
          "decisions_not_parsed": dict(sorted(Counter(str(n.get("decision")) for n in ce
                                                      if n.get("llm_called") and not n.get("call_parsed")).items())),
          "confidence_parsed": dist(n.get("confidence") for n in ce if n.get("call_parsed")),
          "outliers": dist(n.get("n_outliers") for n in ce),
          "sub_groups": dist(n.get("n_sub_groups") for n in ce),
          "sub_group_sizes": sorted(s for n in ce for s in n.get("sub_group_sizes") or []),
          "retries": dist(max(0, (ends[r["item_id"]].get("cluster_eval_attempts") or 0) - 1) for r in processed),
          "second_subgroup_ge3_dropped_C15": sum(int(n.get("dropped_subgroups_ge3") or 0) for n in fails)}

    # 10. 연결 정합(C2)
    gaps = [n.get("link_gap") for n in nodes if n["node"] == "save_newsletter" and n.get("newsletter_id")
            and n["item_id"] in {r["item_id"] for r in processed}]
    m10 = {"excluded_but_linked_per_saved": dist(gaps), "saved_with_gap": ratio(sum(1 for g in gaps if g and g > 0), len(gaps)),
           "excluded_but_linked_total": sum(g for g in gaps if g)}

    # 11. 규칙 위반(shadow)
    drafts = {}
    for t in texts:
        if t.get("kind") == "draft_attempt" and t.get("attempt") == 1 and t.get("draft") and not t.get("fallback"):
            drafts.setdefault(t["item_id"], t)
    meta_calls = {(c["run_k"], c["cluster_id"], c["graph_attempt"]): c for c in calls if c["purpose"] == "newsletter_meta_gen"}
    style_first, style_saved = [], []
    for r in processed:
        t = drafts.get(r["item_id"])
        if t:
            meta = meta_calls.get((r["run_k"], r["cluster_id"], 1)) or {}
            style_first.append(style_metrics(t["draft"], meta.get("parsed_keyword_count"),
                                             meta.get("parsed_invalid_category_count")))
        s = saved_formal.get(r["item_id"])
        if s:
            gen = max([c.get("graph_attempt") or 0 for c in calls_by[(r["run_k"], r["cluster_id"])]
                       if c["purpose"] == "newsletter_meta_gen"], default=0)
            meta = meta_calls.get((r["run_k"], r["cluster_id"], gen)) or {}
            style_saved.append(style_metrics(s["formal"], meta.get("parsed_keyword_count"),
                                             meta.get("parsed_invalid_category_count")))
    m11 = {}
    for name, rows in (("first_draft", style_first), ("saved_formal_draft", style_saved)):
        m11[name] = {"n": len(rows),
                     "raw": {k: dist(r["raw"][k] for r in rows) for k in ("content_chars", "paragraph_count",
                                                                           "title_chars", "sentence_chars",
                                                                           "parsed_keyword_count",
                                                                           "parsed_invalid_category_count",
                                                                           "banned_count", "emotive_count",
                                                                           "emoji_count")},
                     "current_prompt_rules": _rule_rates(rows, "current"), "v2_rules_reference": _rule_rates(rows, "v2")}
    m11["note"] = "현행 프롬프트 규칙이 주 지표, v2 규칙은 참고. '뉴스' 필러는 폴백 메타 전용이라 세지 않는다."

    # 12. 입력 통계
    m12 = input_stats(processed, export_dir, now=now)

    # 13. 실패 분류
    taxonomy = []
    for r in eval_items + warm_items:
        e = ends.get(r["item_id"], {})
        k = (r["run_k"], r["cluster_id"])
        taxonomy.append({"item_id": r["item_id"], "split": r["split"], "outcome": e.get("outcome"),
                         "root_cause": e.get("root_cause") if "root_cause" in e else root_cause(e, calls_by[k]),
                         "events": cluster_events(calls_by[k], http_by[k], e)})
    m13 = {"clusters": taxonomy,
           "root_cause_counts_processed_eval": dict(sorted(Counter(t["root_cause"] for t in taxonomy
                                                                    if t["split"] == "eval" and t["item_id"]
                                                                    in {r["item_id"] for r in processed}).items()))}

    # 클러스터 행(id·sha·건수만)
    cluster_rows = []
    first_sha = {t["item_id"]: t.get("sha256") for t in drafts.values()}
    for r in eval_items + warm_items:
        e = ends.get(r["item_id"], {})
        k = (r["run_k"], r["cluster_id"])
        cluster_rows.append({
            "item_id": r["item_id"], "split": r["split"], "order": r["order"], "run_k": r["run_k"],
            "cluster_id": r["cluster_id"], "size": r["size"], "size_bucket": r["size_bucket"], "split_v2": r["split_v2"],
            "outcome": e.get("outcome"), "root_cause": e.get("root_cause"), "wall_s": e.get("wall_s"),
            "calls": len(calls_by[k]), "http_attempts": len(http_by[k]),
            "cost_usd": round(sum(float(h.get("cost_usd") or 0) for h in http_by[k]), 6),
            "gen_attempts": e.get("gen_attempts"), "newsletter_id": e.get("newsletter_id"),
            "first_draft_sha256": first_sha.get(r["item_id"]),
            "saved_formal_sha256": (saved_formal.get(r["item_id"]) or {}).get("formal_sha256"),
            "article_ids": [a["raw_news_id"] for a in r["articles"]],
            "article_body_sha256": [a["body_sha256"] for a in r["articles"]],
        })

    ledger_total = sum(float(h.get("cost_usd") or 0) for h in http)
    fx = (balance or {}).get("fx")
    header = {
        "command": manifest.get("command"), "code_sha": record.get("code_sha"), "head_sha": manifest.get("head_sha"),
        "run": run_dir.name, "run_at": manifest.get("started_at"), "dry_run": bool(manifest.get("dry_run")),
        "export_t0": record.get("export_t0_kst"), "day_windows": record.get("day_windows_utc"),
        "embeddings_sha256": record.get("embeddings_sha256"), "export_manifest_sha256": record.get("export_manifest_sha256"),
        "sample_manifest_sha256": record.get("sample_manifest_sha256"),
        "n_eval": len(eval_items), "n_warmup": len(warm_items), "n_processed": n_p,
        "n_aborted_or_not_started": len(not_proc), "bucket_allocation": record.get("bucket_allocation"),
        "seed": {"sample": reg.SAMPLE["seed"], "order": reg.ORDER_SEED},
        "processing_order": [r["item_id"] for r in sorted(items, key=lambda r: r["order"])],
        "cost_usd": round(ledger_total, 6), "billing_fx": fx,
        "cost_krw": round(ledger_total * float(fx), 1) if fx else None,
        "colab_cu": record.get("colab_cu_used"), "label_hours_actual": label_hours,
        "resume_count": len(state.get("resumes") or []), "resumes": state.get("resumes") or [],
        "results_viewed": "등록 시점(A8): 미열람. 이 리포트를 커밋하는 것이 첫 열람이다",
        "declared_deviations": [d for d in (prereg.get("pipeline") or {}).get("declared_deviations", [])],
        "stop": summary.get("stop"), "void": summary.get("void"),
        "partial_run": bool(not_proc), "wall_clock_reached": summary.get("wall_clock_reached"),
        "in_client_cap": manifest.get("in_client_cap"), "forbid_database": manifest.get("forbid_database"),
        "user_decisions": record.get("user_decisions"),
        "run_environment": {k: manifest.get(k) for k in ("python_version", "pip_freeze_sha256", "packages")},
    }
    report = {
        "format": "e0-report/1", "prereg": reg.PREREG_ID, "kind": "descriptive_baseline",
        "generated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "header": header,
        "denominator": {"n_p": n_p, "definition": "처리 완료 eval 클러스터(completed/skipped/failed/error)"},
        "metrics": {"1_completion": m1, "2_first_try_schema_pass": m2, "3_retries": m3, "4_latency": m4,
                    "5_tokens_cost": m5, "6_faithfulness_checker": m6, "7_tone_drift": m7, "8_judge_shadow": m8,
                    "9_cluster_evaluator": m9, "10_link_integrity": m10, "11_style_rules_shadow": m11,
                    "12_input_stats": m12, "13_failure_taxonomy": m13},
        "preflight": preflight, "warmup": {"items": [r["item_id"] for r in warm_items],
                                           "outcomes": {r["item_id"]: ends.get(r["item_id"], {}).get("outcome")
                                                        for r in warm_items},
                                           "audit": state.get("audit_after_warmup")},
        "guard": {"refusals": summary.get("refusals"), "ledger": summary.get("ledger"),
                  "caps_usd": {"valid_run": reg.BUDGET["cap_usd_valid_run"],
                               "e0_cumulative": reg.BUDGET["cap_usd_e0_cumulative"]}},
        "balance_reconciliation": balance_reconciliation(ledger_total, balance),
        "clusters": cluster_rows,
        "human_labels": label_section(labels_dir, run_dir, items, ends, record) if labels_dir else {"status": "pending"},
    }
    return report


def offline_sentence_drift(saved: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """저장된 캐주얼 sentence를 드리프트 게이트에 오프라인으로 넣었으면 막혔을 건수(C4, TONE_FIELDS가 sentence를 뺀 크기)."""
    from config.settings import Settings
    from workflow.gates import check_tone_drift

    rows = [s for s in saved if s.get("converted") and s.get("tone_fallback") != "formal"]
    blocked = 0
    for s in rows:
        formal = {"title": (s.get("formal") or {}).get("sentence") or "", "content": ""}
        casual = {"title": (s.get("converted") or {}).get("summary") or "", "content": ""}
        if not check_tone_drift(formal, casual, blocking_types=Settings.TONE_DRIFT_BLOCKING_TYPES).passed:
            blocked += 1
    return ratio(blocked, len(rows))


def input_stats(processed: Sequence[Dict[str, Any]], export_dir: Optional[Path], now=None) -> Dict[str, Any]:
    articles = [a for r in processed for a in r["articles"]]
    out = {"articles_per_cluster": dist(len(r["articles"]) for r in processed),
           "presses_per_cluster": dist(len({a["press_name"] for a in r["articles"]}) for r in processed),
           "body_chars": dist(a["body_chars"] for a in articles),
           "truncated_over_1500": ratio(sum(1 for a in articles if a["body_chars"] > 1500), len(articles))}
    if export_dir is not None:
        from evaluation.llm import frozen_export

        rows = {int(r["id"]): r for r in frozen_export.load_export(Path(export_dir), now=now).rows}
        pairs = []
        for r in processed:
            by_press = defaultdict(list)
            for a in r["articles"]:
                row = rows.get(int(a["raw_news_id"]))
                if row:
                    by_press[row["press"]].append(row["title"])
            pairs.append(_near_dup_pairs(by_press))
        out["same_press_title_near_dup_pairs"] = dist(pairs)
    return out


def label_section(labels_dir, run_dir, items, ends, record) -> Dict[str, Any]:
    from evaluation.llm import e0_labels

    return e0_labels.label_metrics(Path(labels_dir), Path(run_dir), items, ends, record)


# ---------------------------------------------------------------- 누출 검사와 쓰기


def leak_corpus(run_dir: Path, export_dir: Optional[Path], *, now=None) -> List[str]:
    """리포트에 나타나면 안 되는 텍스트: 반출본의 제목·본문, 생성 초안·변환본·저장본."""
    texts: List[str] = []
    for row in _read(Path(run_dir) / "newsletters.jsonl"):
        for group in ("draft", "converted", "formal"):
            doc = row.get(group)
            if isinstance(doc, dict):
                texts += [str(v) for k, v in doc.items() if isinstance(v, str)]
        texts += [str(row.get(k)) for k in ("title", "sentence", "content") if isinstance(row.get(k), str)]
    if export_dir is not None:
        from evaluation.llm import frozen_export

        for row in frozen_export.load_export(Path(export_dir), now=now).rows:
            texts.append(row["title"])
            if row["body"]:
                texts.append(row["body"])
    return [t for t in texts if t and t.strip()]


def check_no_leak(rendered: Sequence[str], corpus: Iterable[str], *, size: int = SHINGLE) -> None:
    """corpus의 어떤 size자 조각(공백·숫자·기호만으로 된 조각 제외)도 rendered에 없어야 한다. 짧은 텍스트는 통째로 본다."""
    haystack = "\n".join(rendered)
    grams = {haystack[i:i + size] for i in range(max(0, len(haystack) - size + 1))}
    hits = 0
    for text in corpus:
        if len(text) < size:
            if len(text) >= 8 and re.search(r"[가-힣A-Za-z]{2}", text) and text in haystack:
                hits += 1
            continue
        for i in range(len(text) - size + 1):
            piece = text[i:i + size]
            if piece in grams and re.search(r"[가-힣A-Za-z]{2}", piece):
                hits += 1
                break
    if hits:
        raise LeakError(f"리포트에 기사·초안 텍스트 조각이 {hits}건 들어 있다 - 쓰지 않는다")


def render_md(report: Dict[str, Any]) -> str:
    h = report["header"]
    m = report["metrics"]
    lines = [f"# E0 기준선 측정 리포트 ({report['prereg']})", "",
             "기술 통계 기준선이다(ADR 0009 A8). 가설·결정 규칙은 없고, 짝지은 전/후 비교가 아니다. "
             "모든 비율에 Wilson 95% CI와 분모를 적는다. 기사·뉴스레터 텍스트는 싣지 않는다(id·sha256·건수만).", ""]
    if h.get("dry_run"):
        lines += ["> **드라이 런**: 합성 반출본과 페이크 LLM 전송 계층으로 만든 리포트다. 실제 기사·실제 호출이 아니며 어떤 수치도 "
                  "E0 결과가 아니다.", ""]
    lines += ["## 머리말", "", "| 항목 | 값 |", "|---|---|"]
    for k in ("command", "code_sha", "head_sha", "run", "run_at", "export_t0", "embeddings_sha256", "export_manifest_sha256",
              "sample_manifest_sha256", "n_eval", "n_warmup", "n_processed", "n_aborted_or_not_started",
              "bucket_allocation", "seed", "cost_usd", "billing_fx", "cost_krw", "colab_cu", "label_hours_actual",
              "resume_count", "results_viewed", "stop", "void", "partial_run", "wall_clock_reached", "in_client_cap",
              "forbid_database", "user_decisions"):
        lines.append(f"| {k} | {json.dumps(h.get(k), ensure_ascii=False)} |")
    lines += ["", f"처리 순서: {', '.join(h['processing_order'])}", "",
              f"날짜 창(UTC): {json.dumps(h.get('day_windows'), ensure_ascii=False)}", ""]
    if h.get("partial_run"):
        lines += ["> 부분 실행이다. 중단 시점에 진행 중일 확률은 오래 걸리는 클러스터일수록 높으므로(길이 편향) 비율 해석에 주의한다.", ""]

    def r(x):
        if not x or x.get("n") in (None, 0):
            return f"– (n={x.get('n') if x else 0})"
        ci = x.get("wilson95") or [None, None]
        return f"{x['k']}/{x['n']} = {x['rate']:.3f} [{ci[0]:.3f}, {ci[1]:.3f}]"

    def d(x):
        return f"n={x['n']}, 중앙값 {x['median']}, 최댓값 {x['max']}" if x and x.get("n") else "n=0"

    lines += ["## 1. 완주 분포", "", f"- outcome(eval): {json.dumps(m['1_completion']['outcomes_eval'], ensure_ascii=False)}",
              f"- 완주율 completed / n_p: {r(m['1_completion']['completion_rate'])}",
              f"- 분모에서 뺀 것: {json.dumps(m['1_completion']['excluded_from_denominator'], ensure_ascii=False)}", "",
              "## 2. 첫 시도 스키마 통과율", "", f"- 전체: {r(m['2_first_try_schema_pass']['overall'])}"]
    lines += [f"- {p}: {r(v)}" for p, v in m["2_first_try_schema_pass"]["by_purpose"].items()]
    m3 = m["3_retries"]
    lines += ["", "## 3. 재시도", "", f"- complete()당 attempts: {d(m3['attempts_per_call'])}",
              f"- complete()당 HTTP 시도: {d(m3['http_attempts_per_call'])}",
              f"- 스키마 실패 합: {m3['schema_failures_sum']}",
              f"- HTTP 상태: {json.dumps(m3['http_status_counts'], ensure_ascii=False)}",
              f"- 429 비율(상태 429 / 보낸 시도): {r(m3['rate_429'])}",
              f"- 전송 실패: {json.dumps(m3['transport_failures'], ensure_ascii=False)}, deadline 도달 호출 {m3['deadline_reached_calls']}",
              f"- 클러스터당 생성 시도: {d(m3['graph']['generation_attempts_per_cluster'])}",
              f"- 클러스터 평가 재시도: {d(m3['graph']['cluster_eval_retries_per_cluster'])}",
              f"- 문체 재변환(드리프트): {d(m3['graph']['tone_redo_for_drift_per_cluster'])}",
              f"- 문체 검증 재시도: {d(m3['graph']['tone_validation_retries_per_cluster'])}", "",
              "## 4. 지연(초)", ""]
    lines += [f"- {p}: {d(v)}" for p, v in m["4_latency"]["call_latency_s_by_purpose"].items()]
    lines += [f"- 클러스터 벽시계: {d(m['4_latency']['cluster_wall_s'])}",
              f"- 저장본 벽시계: {d(m['4_latency']['saved_cluster_wall_s'])}",
              f"- 단일 패스 근사(마지막 성공 시도 지연 합): {d(m['4_latency']['single_pass_latency_sum_s'])}",
              f"- {m['4_latency']['note']}", "", "## 5. 토큰·비용", ""]
    m5 = m["5_tokens_cost"]
    lines += [f"- 토큰(처리 완료 eval): {json.dumps(m5['tokens_processed_eval'])}",
              f"- 토큰(전 단계): {json.dumps(m5['tokens_all_phases'])}",
              f"- 비용 USD: {json.dumps(m5['cost_usd'])}", f"- 비용 근거: {json.dumps(m5['cost_kind_counts'])}",
              f"- 클러스터당 비용: {d(m5['cost_per_cluster_usd'])}, 저장본당: {d(m5['cost_per_saved_usd'])}",
              f"- 응답 모델 버전: {', '.join(m5['models_responded'])}", f"- {m5['formula']}"]
    lines += [f"- 문자/토큰({p}, 첫 시도): {d(v)}" for p, v in m5["prompt_chars_per_token_first_attempt"].items()]
    m6 = m["6_faithfulness_checker"]
    lines += ["", "## 6. 결정론 사실성 검사기", "",
              f"- 첫 초안 미지원 수치 ≥1: {r(m6['first_draft']['unsupported_numbers_ge_1'])}",
              f"- 첫 초안 미지원 인용 ≥1: {r(m6['first_draft']['unsupported_quotes_ge_1'])}",
              f"- 첫 초안 참고 개체명 ≥1: {r(m6['first_draft']['advisory_entities_ge_1'])}",
              f"- 최종 초안 미지원 수치 ≥1: {r(m6['final_draft']['unsupported_numbers_ge_1'])}",
              f"- 최종 초안 미지원 인용 ≥1: {r(m6['final_draft']['unsupported_quotes_ge_1'])}",
              f"- 차단으로 인한 재생성: {m6['regenerations_due_to_block']}, 폴백 초안: {m6['generator_fallback_drafts']}",
              f"- 필드별: {json.dumps(m6['by_field'], ensure_ascii=False)}", "", "## 7. 문체 드리프트", ""]
    m7 = m["7_tone_drift"]
    lines += [f"- 차단 {t}: {r(v)}" for t, v in m7["blocking_by_type"].items()]
    lines += [f"- 형식체 폴백 저장: {r(m7['formal_fallback_saved'])}",
              f"- 로컬 폴백(_fallback_convert): {r(m7['local_fallback_rounds'])}",
              f"- 저장 sentence 오프라인 드리프트(C4): {r(m7['offline_saved_sentence_drift'])}", "",
              "## 8. judge v2 shadow", ""]
    m8 = m["8_judge_shadow"]
    lines += [f"- 기준별 점수: {json.dumps(m8['criteria_score_counts'], ensure_ascii=False)}",
              f"- 기본 임계값 PASS: {r(m8['pass_rate_default_thresholds'])}, judge_unavailable {m8['judge_unavailable']}",
              f"- 근거 없는 주장 수: {d(m8['unsupported_claims'])}, 위치 추적 성공(C17): {r(m8['claim_location_success'])}",
              f"- {m8['note']}", "", "## 9. 클러스터 평가기", ""]
    m9 = m["9_cluster_evaluator"]
    lines += [f"- 판정(parsed): {json.dumps(m9['decisions_parsed'])}, 판정(parsed=False): {json.dumps(m9['decisions_not_parsed'])}",
              f"- confidence(parsed): {d(m9['confidence_parsed'])}", f"- 이상치 수: {d(m9['outliers'])}",
              f"- sub_groups 수: {d(m9['sub_groups'])}, 크기: {m9['sub_group_sizes']}",
              f"- 재시도: {d(m9['retries'])}, 두 번째 서브그룹 ≥3건 폐기(C15): {m9['second_subgroup_ge3_dropped_C15']}", "",
              "## 10. 연결 정합(C2)", "",
              f"- 저장본당 제외됐는데 연결된 기사 수: {d(m['10_link_integrity']['excluded_but_linked_per_saved'])}",
              f"- 그런 저장본: {r(m['10_link_integrity']['saved_with_gap'])}", "", "## 11. 규칙 위반(shadow)", ""]
    for name in ("first_draft", "saved_formal_draft"):
        block = m["11_style_rules_shadow"][name]
        lines.append(f"- {name} (n={block['n']})")
        lines += [f"  - 현행 {k}: {r(v)}" for k, v in block["current_prompt_rules"].items()]
        lines += [f"  - v2(참고) {k}: {r(v)}" for k, v in block["v2_rules_reference"].items()]
    m12 = m["12_input_stats"]
    lines += ["", "## 12. 입력 통계", "", f"- 클러스터당 기사: {d(m12['articles_per_cluster'])}",
              f"- 클러스터당 언론사: {d(m12['presses_per_cluster'])}", f"- 본문 길이: {d(m12['body_chars'])}",
              f"- 1,500자 절단: {r(m12['truncated_over_1500'])}"]
    if "same_press_title_near_dup_pairs" in m12:
        lines.append(f"- 같은 언론사 제목 near-dup 쌍: {d(m12['same_press_title_near_dup_pairs'])}")
    lines += ["", "## 13. 실패 분류", "",
              f"- 근본 원인(처리 완료 eval): {json.dumps(m['13_failure_taxonomy']['root_cause_counts_processed_eval'], ensure_ascii=False)}",
              "", "| item | split | outcome | root_cause | 사건 |", "|---|---|---|---|---|"]
    for t in m["13_failure_taxonomy"]["clusters"]:
        lines.append(f"| {t['item_id']} | {t['split']} | {t['outcome']} | {t['root_cause']} | "
                     f"{json.dumps(t['events'], ensure_ascii=False)} |")
    bal = report["balance_reconciliation"]
    lines += ["", "## 잔액 대조", "", f"- 상태: {bal['status']}", f"- 원장 합 USD: {bal['ledger_sum_usd']}"]
    for k in ("balance_before_krw", "balance_before_at", "balance_after_krw", "balance_after_at", "billing_fx_krw_per_usd",
              "delta_usd", "abs_diff_usd", "threshold_usd"):
        if k in bal:
            lines.append(f"- {k}: {bal[k]}")
    lines += [f"- 확인하지 못한 것: {', '.join(bal['unknowns'])}", "", "## pre-flight", ""]
    for c in (report.get("preflight") or {}).get("calls", []):
        lines.append(f"- {c['purpose']}: parsed={c['parsed']}, error={c['error']}, 지연 {c['latency_s']}s, "
                     f"토큰 {c['prompt_tokens']}/{c['completion_tokens']}/hidden {c['hidden_tokens']}")
    lines += ["", "## 사람 라벨", "", f"- {json.dumps(report.get('human_labels'), ensure_ascii=False)[:2000]}", "",
              "## 선언된 이탈(A8.2)", ""]
    lines += [f"- {json.dumps(dv, ensure_ascii=False)}" for dv in h.get("declared_deviations") or []]
    return "\n".join(lines) + "\n"


def write_report(report: Dict[str, Any], out_dir: Path, *, texts: Iterable[str]) -> Dict[str, Path]:
    out_dir = Path(out_dir)
    rendered_json = json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n"
    rendered_md = render_md(report)
    check_no_leak([rendered_json, rendered_md], texts)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REPORT_JSON).write_text(rendered_json, encoding="utf-8")
    (out_dir / REPORT_MD).write_text(rendered_md, encoding="utf-8")
    return {"json": out_dir / REPORT_JSON, "md": out_dir / REPORT_MD}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E0 리포트(ADR 0009 A8.7)")
    parser.add_argument("--run", required=True, help="data/experiments/e0/<run>")
    parser.add_argument("--export-dir", required=True)
    parser.add_argument("--prereg", default=str(reg.PREREG_PATH))
    parser.add_argument("--manifest", default=str(reg.REPO_ROOT / reg.SAMPLE_MANIFEST_REL))
    parser.add_argument("--out-dir", default=str(reg.REPO_ROOT / "reports" / "llm"))
    parser.add_argument("--labels-dir", default=None)
    parser.add_argument("--label-hours", type=float, default=None)
    parser.add_argument("--balance-before", type=float, default=None, help="실행 전 선불 잔액(₩)")
    parser.add_argument("--balance-before-at", default=None)
    parser.add_argument("--balance-after", type=float, default=None, help="마지막 호출 24시간 뒤 잔액(₩)")
    parser.add_argument("--balance-after-at", default=None)
    parser.add_argument("--billing-fx", type=float, default=None, help="청구 환율(₩/$)")
    parser.add_argument("--other-gemini-calls", choices=("yes", "no"), default=None)
    args = parser.parse_args(argv)
    balance = {"before": args.balance_before, "before_at": args.balance_before_at, "after": args.balance_after,
               "after_at": args.balance_after_at, "fx": args.billing_fx,
               "other_gemini_calls": args.other_gemini_calls == "yes"}
    report = build_report(Path(args.run), prereg=reg.load_prereg(Path(args.prereg)), manifest_path=Path(args.manifest),
                          export_dir=Path(args.export_dir), balance=balance, label_hours=args.label_hours,
                          labels_dir=Path(args.labels_dir) if args.labels_dir else None)
    try:
        paths = write_report(report, Path(args.out_dir), texts=leak_corpus(Path(args.run), Path(args.export_dir)))
    except LeakError as e:
        print(f"거절: {e}")
        return 2
    print(json.dumps({k: str(v) for k, v in paths.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
