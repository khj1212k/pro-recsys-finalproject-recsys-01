"""워밍업 3단계: 고른 클러스터들을 실제 LangGraph 워크플로우로 끝까지 생성하고 계측한다.

    python -m evaluation.warmup.generate --membership <first_run.json> --out <json> \
        --ledger <ledger.json> --cap-usd 0.20 --n 5 --seed 20260926 [--details <local json>]

- Stage5_NewsletterGeneration과 같은 부품을 쓴다: create_new_batch(run_id) → compile_workflow()
  → 클러스터마다 app.stream(state). 다른 점은 (1) 클러스터를 2단계 멤버십 JSON에서 크기가 퍼지게
  seed로 고른다(Stage5는 id 내림차순 limit), (2) 노드 순서·LLM 호출·HTTP usage를 기록한다.
- 비용: 모든 HTTP 응답의 usage로 원장(ledger)에 누적하고, 요청을 보내기 전에 "누적 + 이 요청의
  최악 비용"이 상한을 넘으면 LLM_KILL_SWITCH를 켜서 파이프라인의 기존 킬 스위치 경로로 멈춘다.
  응답이 없는 요청(타임아웃)은 최악 비용을 상한 추정치로 원장에 더한다.
- 사실성: 각 뉴스레터(격식체 초안, 문체 변환 후 저장본)를 evaluation/llm/faithfulness.py로
  출처 본문 전체 / 생성기가 실제로 본 부분(상위 10건 × 앞 1500자)에 대해 검사하고, 초안→저장본
  드리프트를 compare_rewrite로 잰다.
- --out 리포트에는 id·해시·개수만 남긴다. --details(저장소 밖 경로)에는 분석용으로 미지원 표면형을
  남긴다(생성문 기준, 본문 인용 아님).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from evaluation.warmup.budget import (
    PRICE_SOURCE,
    PRICES_PER_1M,
    BudgetGuard,
    CostLedger,
    LedgerEntry,
    cost_usd,
    generator_visible_articles,
    select_clusters,
    worst_case_cost_usd,
)
from evaluation.warmup.common import distribution, environment, sha256_text, write_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM 계측: complete() 진입 가드 + HTTP 요청/응답 훅
# ---------------------------------------------------------------------------

class Instrument:
    def __init__(self, guard: BudgetGuard):
        self.guard = guard
        self.ledger = guard.ledger
        self.lock = threading.Lock()
        self.ctx: Dict[str, Any] = {"purpose": None, "model": None, "cluster": None, "max_tokens": None,
                                    "prompt_chars": 0}
        self.calls: List[dict] = []  # complete() 단위
        self.http: List[dict] = []  # HTTP 시도 단위
        self._pending = 0

    # --- HTTP hooks (httpx2 event hooks) ---
    def on_request(self, request) -> None:
        body = {}
        try:
            body = json.loads(request.content or b"{}")
        except Exception:
            pass
        model = body.get("model") or self.ctx["model"]
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or self.ctx["max_tokens"] or 0)
        prompt_chars = sum(len(m.get("content") or "") for m in body.get("messages", []) if isinstance(m, dict))
        if not self.guard.allows(model, prompt_chars, max_tokens):
            self.guard.record_block(model, self.ctx["purpose"] or "?", prompt_chars, max_tokens)
            os.environ["LLM_KILL_SWITCH"] = "1"
            raise RuntimeError(f"warmup budget cap reached before sending ({self.ledger.spent():.4f} USD spent)")
        with self.lock:
            self._pending += 1
        request.extensions["warmup_t0"] = time.time()
        request.extensions["warmup_meta"] = {"model": model, "max_tokens": max_tokens, "prompt_chars": prompt_chars}

    def on_response(self, response) -> None:
        meta = response.request.extensions.get("warmup_meta", {})
        t0 = response.request.extensions.get("warmup_t0", time.time())
        with self.lock:
            self._pending -= 1
        response.read()
        usage = {}
        try:
            usage = (response.json() or {}).get("usage") or {}
        except Exception:
            pass
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or (pt + ct))
        model = meta.get("model") or self.ctx["model"]
        c = cost_usd(model, pt, ct, tt) if usage else 0.0
        rec = {
            "cluster": self.ctx["cluster"], "purpose": self.ctx["purpose"], "model": model,
            "status": response.status_code, "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt,
            "hidden_tokens": max(0, tt - pt - ct), "cost_usd": c, "http_s": round(time.time() - t0, 3),
            "prompt_chars": meta.get("prompt_chars"), "max_tokens": meta.get("max_tokens"),
        }
        self.http.append(rec)
        self.ledger.add(LedgerEntry(
            ts=time.time(), model=model, purpose=str(self.ctx["purpose"]), kind="observed",
            prompt_tokens=pt, completion_tokens=ct, total_tokens=tt, cost_usd=c,
            status=response.status_code, tag=f"cluster={self.ctx['cluster']}",
        ))

    # --- complete() wrapper ---
    def wrap_complete(self, orig):
        inst = self

        def complete(client_self, messages, *, schema=None, purpose="unknown", temperature=0.2, max_tokens=4096):
            prompt_chars = sum(len(m.get("content") or "") for m in messages)
            inst.ctx.update({"purpose": purpose, "model": client_self.model, "max_tokens": max_tokens,
                             "prompt_chars": prompt_chars})
            if not inst.guard.allows(client_self.model, prompt_chars, max_tokens):
                inst.guard.record_block(client_self.model, purpose, prompt_chars, max_tokens)
                os.environ["LLM_KILL_SWITCH"] = "1"
                logger.error("예산 상한: %s 호출 전에 킬 스위치를 켰습니다 (spent=%.4f)", purpose, inst.ledger.spent())
            n_http_before = len(inst.http)
            with inst.lock:
                pending_before = inst._pending
            t0 = time.time()
            result = orig(client_self, messages, schema=schema, purpose=purpose,
                          temperature=temperature, max_tokens=max_tokens)
            wall = time.time() - t0
            with inst.lock:
                lost = inst._pending - pending_before  # 응답 없이 끝난 요청(타임아웃 등)
                inst._pending = pending_before
            for _ in range(max(0, lost)):
                ub = worst_case_cost_usd(client_self.model, prompt_chars, max_tokens)
                inst.ledger.add(LedgerEntry(ts=time.time(), model=client_self.model, purpose=purpose,
                                            kind="unobserved_upper_bound", cost_usd=ub,
                                            tag=f"cluster={inst.ctx['cluster']}"))
            inst.calls.append({
                "cluster": inst.ctx["cluster"], "purpose": purpose, "model": client_self.model,
                "ok": result.ok, "parsed": result.parsed is not None, "error": (result.error or "")[:200] or None,
                "attempts": result.attempts, "latency_s": round(result.latency_s, 3), "wall_s": round(wall, 3),
                "input_tokens": result.usage.input_tokens, "output_tokens": result.usage.output_tokens,
                "http_requests": len(inst.http) - n_http_before + max(0, lost), "lost_requests": max(0, lost),
                "prompt_chars": prompt_chars, "max_tokens": max_tokens,
            })
            return result

        return complete


def install_instrumentation(inst: Instrument) -> None:
    """레지스트리가 만드는 OpenAI 클라이언트에 httpx2 훅을 달고 complete()를 감싼다."""
    import openai
    import core.llm.adapters as adapters

    real_openai = adapters.OpenAI

    def openai_with_hooks(*args, **kwargs):
        kwargs["http_client"] = openai.DefaultHttpxClient(
            event_hooks={"request": [inst.on_request], "response": [inst.on_response]}
        )
        return real_openai(*args, **kwargs)

    adapters.OpenAI = openai_with_hooks
    adapters.OpenAICompatLLMClient.complete = inst.wrap_complete(adapters.OpenAICompatLLMClient.complete)


# ---------------------------------------------------------------------------
# 사실성 요약(개수만)
# ---------------------------------------------------------------------------

def newsletter_text(nl: Optional[dict]) -> str:
    if not nl:
        return ""
    return "\n".join(x for x in [nl.get("title") or "", nl.get("sentence") or nl.get("summary") or "",
                                 nl.get("content") or ""] if x)


def faithfulness_counts(report) -> Dict[str, Any]:
    d = report.to_dict()
    return {
        "passed_zero_tolerance": d["passed"],
        "numbers_total": d["number_total"],
        "numbers_exact": d["number_exact"],
        "numbers_approx": d["number_approx"],
        "numbers_unsupported": len(d["unsupported_numbers"]),
        "numbers_unsupported_by_unit": dict(Counter(n["unit"] or "(none)" for n in d["unsupported_numbers"])),
        "entities_total": d["entity_total"],
        "entities_unsupported": len(d["unsupported_entities"]),
        "entities_unsupported_by_tag": dict(Counter(e["tag"] for e in d["unsupported_entities"])),
        "quotes_total": d["quote_total"],
        "quotes_unsupported": len(d["unsupported_quotes"]),
        "entity_extractor": d["entity_extractor"],
    }


def drift_counts(drift) -> Dict[str, Any]:
    d = drift.to_dict()
    return {k: (len(v) if isinstance(v, list) else v) for k, v in d.items()}


def faithfulness_block(formal: dict, saved: dict, sources: List[dict]) -> Dict[str, Any]:
    from evaluation.llm.faithfulness import check_against_sources, compare_rewrite

    full = [a.get("content") or "" for a in sources]
    visible = [a["content"] for a in generator_visible_articles(sources)]
    formal_text = newsletter_text(formal)
    saved_text = newsletter_text(saved)
    reports = {
        "formal_vs_full_sources": check_against_sources(formal_text, full),
        "saved_vs_full_sources": check_against_sources(saved_text, full),
        "formal_vs_generator_visible": check_against_sources(formal_text, visible),
    }
    drift = compare_rewrite(formal.get("content") or "", saved.get("content") or "")
    counts = {k: faithfulness_counts(v) for k, v in reports.items()}
    counts["tone_drift_content"] = drift_counts(drift)
    counts["tone_drift_sentence"] = drift_counts(
        compare_rewrite(formal.get("sentence") or "", saved.get("sentence") or ""))
    details = {k: v.to_dict() for k, v in reports.items()}
    details["tone_drift_content"] = drift.to_dict()
    return {"counts": counts, "details": details}


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def load_articles(ids: List[int]) -> Dict[int, dict]:
    from db.connection import get_connection, release_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT N.raw_news_id, N.raw_news_title, N.raw_news_content, P.press_name,
                       N.news_letter_id, N.raw_news_content_sha256
                FROM news_raw N JOIN press P ON N.press_id = P.press_id
                WHERE N.raw_news_id = ANY(%s)
                """,
                (ids,),
            )
            rows = cur.fetchall()
    finally:
        release_connection(conn)
    return {int(r[0]): {"id": int(r[0]), "title": r[1], "content": r[2], "press_name": r[3],
                        "news_letter_id": r[4], "content_sha256": r[5]} for r in rows}


def db_newsletter(newsletter_id: int) -> Dict[str, Any]:
    from db.connection import get_connection, release_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT news_letter_id, run_id, raw_news_count, news_letter_embedding IS NOT NULL,
                       news_letter_title, news_letter_sentence, news_letter_content, news_letter_keywords
                FROM news_letter WHERE news_letter_id = %s
                """,
                (newsletter_id,),
            )
            r = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM news_raw WHERE news_letter_id = %s", (newsletter_id,))
            linked = cur.fetchone()[0]
            cur.execute(
                """SELECT C.category_name FROM news_letter_categories L JOIN category C
                   ON L.category_id = C.category_id WHERE L.news_letter_id = %s ORDER BY 1""",
                (newsletter_id,),
            )
            cats = [x[0] for x in cur.fetchall()]
    finally:
        release_connection(conn)
    if r is None:
        return {"exists": False}
    return {
        "exists": True, "run_id": r[1], "raw_news_count": r[2], "has_embedding": bool(r[3]),
        "title": r[4], "sentence": r[5], "content": r[6], "keywords": r[7],
        "linked_news_raw": int(linked), "categories": cats,
    }


def run_cluster(app, cluster: dict, articles_by_id: Dict[int, dict], run_id: int, inst: Instrument,
                all_groups: Dict[int, List[int]], data: Dict[str, Any]) -> Dict[str, Any]:
    cid = int(cluster["cluster_idx"])
    inst.ctx["cluster"] = cid
    state: Dict[str, Any] = {
        "current_cluster_id": cid,
        "current_cluster_index": 0,
        "all_cluster_ids": [cid],
        "all_cluster_groups": all_groups,
        "data": data,
        "run_id": run_id,
        "completed_newsletters": [],
        "failed_clusters": [],
        "skipped_clusters": [],
    }
    trace: List[dict] = []
    merged = dict(state)
    t0 = time.time()
    error = None
    try:
        for chunk in app.stream(state, stream_mode="updates"):
            for node, update in chunk.items():
                trace.append({"node": node, "t_s": round(time.time() - t0, 3)})
                if update:
                    merged.update(update)
                    if node == "eval_cluster":
                        ce = update.get("cluster_eval") or {}
                        trace[-1]["cluster_eval"] = {
                            "decision": ce.get("decision"), "confidence": ce.get("confidence"),
                            "n_outliers": len(ce.get("outlier_indices") or []),
                            "n_sub_groups": len(ce.get("sub_groups") or []),
                            "n_articles": len(merged.get("current_articles") or []),
                        }
                    if node == "eval_newsletter":
                        ne = update.get("newsletter_eval") or {}
                        trace[-1]["newsletter_eval"] = {
                            "decision": ne.get("decision"), "score": ne.get("score"),
                            "n_issues": len(ne.get("issues") or []),
                            "feedback_sha256": sha256_text(ne.get("feedback") or ""),
                        }
                    if node == "generate_newsletter":
                        draft = update.get("newsletter_draft") or {}
                        trace[-1]["draft"] = {
                            "is_none": not draft,
                            "content_chars": len(draft.get("content") or ""),
                            "content_sha256": sha256_text(draft.get("content")),
                        }
    except Exception as e:  # Stage5.process_cluster와 같은 방식으로 클러스터 단위로 격리
        error = f"{type(e).__name__}: {e}"[:300]
        logger.exception("cluster %s failed", cid)
    wall = time.time() - t0

    nodes = [t["node"] for t in trace]
    used = merged.get("current_articles") or []
    completed = merged.get("completed_newsletters") or []
    calls = [c for c in inst.calls if c["cluster"] == cid]
    http = [h for h in inst.http if h["cluster"] == cid]

    by_purpose: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "complete_calls": 0, "http_requests": 0, "failed_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "hidden_tokens": 0, "cost_usd": 0.0, "latency_s": []})
    for c in calls:
        p = by_purpose[c["purpose"]]
        p["complete_calls"] += 1
        p["failed_calls"] += 0 if c["ok"] and c["parsed"] else 1
        p["latency_s"].append(c["wall_s"])
    for h in http:
        p = by_purpose[h["purpose"]]
        p["http_requests"] += 1
        p["prompt_tokens"] += h["prompt_tokens"]
        p["completion_tokens"] += h["completion_tokens"]
        p["hidden_tokens"] += h["hidden_tokens"]
        p["cost_usd"] += h["cost_usd"]
    for p in by_purpose.values():
        p["latency_s"] = [round(x, 3) for x in p["latency_s"]]

    out: Dict[str, Any] = {
        "cluster_idx": cid,
        "cluster_size": int(cluster["size"]),
        "n_press": int(cluster.get("n_press", 0)),
        "raw_news_ids": list(cluster["raw_news_ids"]),
        "used_article_ids": [a["id"] for a in used],
        "wall_s": round(wall, 3),
        "error": error,
        "node_trace": trace,
        "loops": {
            "eval_cluster_runs": nodes.count("eval_cluster"),
            "generate_runs": nodes.count("generate_newsletter"),
            "eval_newsletter_runs": nodes.count("eval_newsletter"),
            "tone_runs": nodes.count("convert_tone"),
        },
        "outcome": ("saved" if completed else
                    "skipped_at_cluster_eval" if cid in (merged.get("skipped_clusters") or []) else
                    "failed_max_retries" if "handle_max_retries" in nodes else
                    "failed" if cid in (merged.get("failed_clusters") or []) else "ended_without_save"),
        "newsletter_ids": list(completed),
        "error_message_sha256": sha256_text(merged.get("error_message")) if merged.get("error_message") else None,
        "llm_by_purpose": dict(by_purpose),
        "llm_calls": calls,
        "cost_usd": round(sum(h["cost_usd"] for h in http), 6),
        "tokens": {"prompt": sum(h["prompt_tokens"] for h in http),
                   "completion": sum(h["completion_tokens"] for h in http),
                   "hidden": sum(h["hidden_tokens"] for h in http)},
        "fallbacks": {
            "content_fallback": any(c["purpose"] == "newsletter_content_gen" and not c["parsed"] for c in calls),
            "meta_fallback": any(c["purpose"] == "newsletter_meta_gen" and not c["parsed"] for c in calls),
            "tone_llm_calls_unparsed": sum(1 for c in calls if c["purpose"] == "tone_convert" and not c["parsed"]),
            "tone_conversion_feedback": merged.get("conversion_feedback"),
        },
        "embedding_in_state": merged.get("newsletter_embedding") is not None,
    }

    details: Dict[str, Any] = {"cluster_idx": cid}
    if completed:
        nid = int(completed[0])
        row = db_newsletter(nid)
        formal = merged.get("newsletter_draft") or {}
        saved = {"title": row.get("title"), "sentence": row.get("sentence"), "content": row.get("content")}
        fb = faithfulness_block(formal, saved, used)
        out["saved"] = {
            "newsletter_id": nid, "run_id": row.get("run_id"), "raw_news_count": row.get("raw_news_count"),
            "linked_news_raw": row.get("linked_news_raw"), "has_embedding": row.get("has_embedding"),
            "categories": row.get("categories"), "n_keywords": len(row.get("keywords") or []),
            "title_chars": len(row.get("title") or ""), "sentence_chars": len(row.get("sentence") or ""),
            "content_chars": len(row.get("content") or ""),
            "content_sha256": sha256_text(row.get("content")),
            "sentence_differs_from_formal": (row.get("sentence") or "") != (formal.get("sentence") or ""),
            "formal_content_chars": len(formal.get("content") or ""),
            "formal_content_sha256": sha256_text(formal.get("content")),
            "formal_paragraphs": len([p for p in (formal.get("content") or "").split("\n") if p.strip()]),
        }
        out["faithfulness"] = fb["counts"]
        details["faithfulness"] = fb["details"]
        details["newsletter_id"] = nid
    return {"summary": out, "details": details}


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m evaluation.warmup.generate")
    parser.add_argument("--membership", required=True, help="evaluation.warmup.cluster 출력 JSON")
    parser.add_argument("--out", required=True)
    parser.add_argument("--details", default=None, help="분석용 상세(저장소 밖 경로)")
    parser.add_argument("--ledger", required=True, help="태스크 누적 비용 원장 JSON")
    parser.add_argument("--cap-usd", type=float, default=0.20)
    parser.add_argument("--n", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--dry-run", action="store_true", help="선택·데이터 확인만 하고 LLM은 부르지 않음")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    with open(args.membership, encoding="utf-8") as f:
        first_run = json.load(f)
    clusters = first_run["membership"]["clusters"]
    picked = select_clusters(clusters, n=args.n, seed=args.seed)
    ids = sorted({i for c in picked for i in c["raw_news_ids"]})
    articles = load_articles(ids)
    missing = [i for i in ids if i not in articles]
    already = [i for i in ids if articles.get(i, {}).get("news_letter_id") is not None]
    logger.info("선택 클러스터 %s (크기 %s), 기사 %s건, 누락 %s, 기배정 %s",
                [c["cluster_idx"] for c in picked], [c["size"] for c in picked], len(ids), missing, already)
    if missing or already:
        raise SystemExit("선택한 기사가 DB에 없거나 이미 다른 뉴스레터에 배정됨 - 클러스터링부터 다시")

    ledger = CostLedger(args.ledger)
    guard = BudgetGuard(cap_usd=args.cap_usd, ledger=ledger)
    report: Dict[str, Any] = {
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": environment(),
        "cap_usd": args.cap_usd,
        "spent_before_usd": round(ledger.spent(), 6),
        "prices_per_1m_usd": {m: {"input": p[0], "output": p[1]} for m, p in PRICES_PER_1M.items()},
        "price_source": PRICE_SOURCE,
        "selection": {"seed": args.seed, "n": args.n, "rule": "evenly spaced over distinct cluster sizes (>=3), seeded pick within a size",
                      "clusters": [{"cluster_idx": c["cluster_idx"], "size": c["size"], "n_press": c["n_press"]}
                                   for c in picked],
                      "clustering_input_ids_sha256": first_run["input"]["ids_sha256"]},
        "source_articles": {str(i): {"press": articles[i]["press_name"], "content_sha256": articles[i]["content_sha256"],
                                     "content_chars": len(articles[i]["content"] or "")} for i in ids},
    }
    if args.dry_run:
        write_json(args.out, report)
        return

    from config.settings import Settings
    from core.llm.registry import resolve_role_config
    from core.llm_metrics import get_metrics_collector
    from db.batch_manager import create_new_batch
    from workflow.graph import compile_workflow
    from workflow.nodes import cleanup_workflow_embedder

    report["roles"] = {r: dict(zip(("provider", "model"), resolve_role_config(r))) for r in ("generator", "judge", "tone")}
    report["settings"] = {k: getattr(Settings, k) for k in (
        "MAX_RETRY_CLUSTER_EVAL", "MAX_RETRY_NEWSLETTER_EVAL", "MAX_RETRY_TONE_VALIDATION",
        "MAX_LLM_CALL_RETRIES", "LLM_REQUEST_TIMEOUT_S", "LLM_CALL_DEADLINE_S")}
    report["kill_switch_file"] = Settings.LLM_KILL_SWITCH_FILE

    inst = Instrument(guard)
    install_instrumentation(inst)
    metrics = get_metrics_collector()
    metrics.start_batch()

    all_groups = {int(c["cluster_idx"]): [int(i) for i in c["raw_news_ids"]] for c in clusters}
    cluster_log: Dict[str, Any] = {str(k): v for k, v in all_groups.items()}
    fr = first_run["final_after_split_v2"]["basic_stats"]
    cluster_log["clustering_stats"] = {
        "n_articles": first_run["input"]["n_articles"], "n_clusters": fr["n_clusters"],
        "noise_ratio": fr["noise_ratio"], "effective_params": first_run["params"],
        "source": "evaluation.warmup.cluster 2026-09-26 first run",
        "warmup_selected_cluster_idx": [c["cluster_idx"] for c in picked],
    }
    run_id = create_new_batch(cluster_log)
    report["run_id"] = run_id

    sel_ids = [i for c in picked for i in c["raw_news_ids"]]
    data = {
        "ids": sel_ids,
        "titles": [articles[i]["title"] for i in sel_ids],
        "contents": [articles[i]["content"] for i in sel_ids],
        "press_names": [articles[i]["press_name"] for i in sel_ids],
    }

    app = compile_workflow()
    results, details = [], []
    t_all = time.time()
    try:
        for c in picked:
            if os.environ.get("LLM_KILL_SWITCH") == "1":
                logger.error("예산 상한으로 남은 클러스터를 시작하지 않습니다")
                results.append({"cluster_idx": c["cluster_idx"], "outcome": "not_started_budget"})
                continue
            r = run_cluster(app, c, articles, run_id, inst, all_groups, data)
            results.append(r["summary"])
            details.append(r["details"])
            logger.info("cluster %s → %s (%.1fs, $%.5f, 누적 $%.5f)", c["cluster_idx"], r["summary"]["outcome"],
                        r["summary"]["wall_s"], r["summary"]["cost_usd"], ledger.spent())
    finally:
        cleanup_workflow_embedder()
        metrics.end_batch()

    saved = [r for r in results if r.get("outcome") == "saved"]
    report["total_wall_s"] = round(time.time() - t_all, 3)
    report["results"] = results
    report["totals"] = {
        "clusters_attempted": sum(1 for r in results if r.get("outcome") != "not_started_budget"),
        "newsletters_saved": len(saved),
        "newsletter_ids": [r["saved"]["newsletter_id"] for r in saved],
        "outcomes": dict(Counter(r.get("outcome") for r in results)),
        "run_cost_usd": round(sum(h["cost_usd"] for h in inst.http), 6),
        "unobserved_upper_bound_usd": round(sum(e.cost_usd for e in ledger.entries
                                                if e.kind == "unobserved_upper_bound"), 6),
        "task_spent_usd_after": round(ledger.spent(), 6),
        "http_requests": len(inst.http),
        "http_status": dict(Counter(str(h["status"]) for h in inst.http)),
        "hidden_tokens_total": sum(h["hidden_tokens"] for h in inst.http),
        "budget_blocks": guard.blocked,
        "cost_per_saved_newsletter_usd": (round(sum(h["cost_usd"] for h in inst.http) / len(saved), 6)
                                          if saved else None),
        "wall_s_per_cluster": distribution([r["wall_s"] for r in results if "wall_s" in r]),
        "prompt_tokens_per_char": distribution([h["prompt_tokens"] / h["prompt_chars"] for h in inst.http
                                                if h.get("prompt_chars") and h["prompt_tokens"]]),
    }
    report["llm_metrics_collector"] = metrics.get_summary()
    write_json(args.out, report)
    if args.details:
        write_json(args.details, {"run_id": run_id, "clusters": details, "http": inst.http})
    logger.info("wrote %s; saved=%s cost=$%.5f", args.out, len(saved), report["totals"]["run_cost_usd"])


if __name__ == "__main__":
    main()
