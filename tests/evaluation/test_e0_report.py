"""E0 리포트(evaluation/llm/e0_report.py, ADR 0009 A8.3·A8.7) - 손으로 만든 run 기록에서 지표를 계산한다.

기록은 이 파일이 지어낸 것이다(실제 실행 아님). 계산을 손으로 따라갈 수 있게 클러스터 8개(eval 7, warmup 1)만 둔다:
완주 1, skipped 1(마지막 클러스터 평가 호출이 전송 실패), 생성기 폴백 1(메타 호출이 length), 사실성 차단 1,
예산 중단 1, 미시작 1, 중단(interrupted) 1, warmup 완주 1. n_p = 4.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from evaluation.llm import e0_instrument as ins  # noqa: E402
from evaluation.llm import e0_registered as reg  # noqa: E402
from evaluation.llm import e0_report as rep  # noqa: E402

PREREG = yaml.safe_load((REPO_ROOT / reg.PREREG_REL).read_text(encoding="utf-8"))
FIRST_TITLE = "가람시 도서관 건립 계획 확정 소식 정리"   # 20자 > 15
FIRST_CONTENT = "\n".join(["가람시는 새 도서관을 짓는다. 충격적인 소식은 아니다." * 10] * 4)   # 4문단, '충격' 4회


class Run:
    def __init__(self, root: Path):
        self.dir = root / "run-t"
        self.dir.mkdir(parents=True)
        self.items, self.seq = [], {}

    def _w(self, name, row):
        with open(self.dir / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def item(self, item_id, split, order, size=3):
        run_k, cid = int(item_id[1]), int(item_id.split("-c")[1])
        self.items.append({"item_id": item_id, "split": split, "order": order, "run_k": run_k, "cluster_id": cid,
                           "size": size, "size_bucket": "3-4" if size <= 4 else "5-9", "split_v2": "no",
                           "articles": [{"raw_news_id": cid * 100 + i, "url": f"https://news.example/{cid}/{i}",
                                         "press_name": "가람일보" if i % 2 else "누리신문", "body_sha256": "a" * 64,
                                         "title_sha256": "b" * 64, "body_chars": 1000 + 400 * i} for i in range(size)]})
        return run_k, cid

    def call(self, item_id, purpose, *, ga=1, parsed=True, sf=0, error=None, http=((200, (100, 20, 150)),), kw=None,
             inv=None, latency=1.0):
        run_k, cid = int(item_id[1]), int(item_id.split("-c")[1])
        self.seq[item_id] = self.seq.get(item_id, 0) + 1
        seq = self.seq[item_id]
        cost = 0.0
        for n, (status, tokens) in enumerate(http, start=1):
            if status == "timeout":
                row_cost, kind, status, tokens, transport = 0.02, "unobserved_upper_bound", None, None, "timeout"
            elif status >= 400:
                row_cost, kind, transport = 0.0, "error_response", None
                tokens = None
            else:
                row_cost = (tokens[0] * 0.3 + (tokens[2] - tokens[0]) * 2.5) / 1e6
                kind, transport = "observed", None
            cost += row_cost
            self._w(ins.HTTP_FILE, {
                "run": "run-t", "cluster_id": cid, "run_k": run_k, "graph_attempt": ga, "purpose": purpose,
                "call_seq": seq, "attempt_seq": n, "provider": "gemini", "model_requested": "m",
                "model_responded": "gemini-3.5-flash-lite-001", "system_fingerprint": None, "status": status,
                "transport_error": transport, "finish_reason": "stop" if status == 200 else None, "prompt_chars": 120,
                "max_tokens": 1024, "prompt_tokens": tokens[0] if tokens else None,
                "completion_tokens": tokens[1] if tokens else None, "total_tokens": tokens[2] if tokens else None,
                "reasoning_tokens_reported": None, "hidden_tokens": tokens[2] - tokens[0] - tokens[1] if tokens else None,
                "cost_usd": row_cost, "cost_kind": kind, "latency_s": 0.5, "started_at": "2026-10-20T00:00:00Z"})
        self._w(ins.CALLS_FILE, {
            "run": "run-t", "cluster_id": cid, "run_k": run_k, "graph_attempt": ga, "purpose": purpose, "call_seq": seq,
            "provider": "gemini", "model": "m", "prompt_sha12": "0" * 12, "prompt_chars": 120, "temperature": 0.1,
            "max_tokens": 1024, "attempts": len(http), "http_attempts": len(http), "schema_failures": sf,
            "parsed": parsed, "responded": bool(parsed or sf or error in ("length", "content_filter")),
            "first_try_schema_pass": bool(parsed and sf == 0), "error": error, "http_status": None,
            "latency_s": latency, "wall_s": latency, "cost_usd": cost, "parsed_keyword_count": kw,
            "parsed_invalid_category_count": inv, "started_at": "2026-10-20T00:00:00Z"})

    def node(self, item_id, node, **fields):
        run_k, cid = int(item_id[1]), int(item_id.split("-c")[1])
        self._w(ins.NODES_FILE, {"run": "run-t", "node": node, "item_id": item_id, "cluster_id": cid, "run_k": run_k,
                                 **fields})

    def end(self, item_id, outcome, **fields):
        run_k, cid = int(item_id[1]), int(item_id.split("-c")[1])
        if outcome != "not_started":
            self._w(ins.CLUSTERS_FILE, {"kind": "start", "item_id": item_id, "run_k": run_k, "cluster_id": cid})
        self._w(ins.CLUSTERS_FILE, {"kind": "end", "item_id": item_id, "run_k": run_k, "cluster_id": cid,
                                    "split": next(i["split"] for i in self.items if i["item_id"] == item_id),
                                    "outcome": outcome, **fields})

    def text(self, kind, item_id, **fields):
        self._w("newsletters.jsonl", {"kind": kind, "item_id": item_id, **fields})

    def finish(self):
        manifest = self.dir.parent / "e0.jsonl"
        manifest.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.items), encoding="utf-8")
        (self.dir / "run_summary.json").write_text(json.dumps({"cumulative_usd": 0.05, "refusals": 1}))
        (self.dir / "run_manifest.json").write_text(json.dumps({"command": "python -m evaluation.llm.e0_baseline run",
                                                                "started_at": "2026-10-20T00:00:00Z"}))
        (self.dir / "run_state.json").write_text(json.dumps({"resumes": [{"at": "x", "reason": "절전"}]}))
        return manifest


def _faith(blocking_numbers, attempt, gate_passed, entities=0, field="content"):
    spans = [{"group": "blocking", "type": "numbers", "field": field, "start": 3, "end": 6}] * blocking_numbers
    return dict(attempt=attempt, gate_passed=gate_passed, passed=gate_passed, reason=None if gate_passed else "faithfulness",
                blocking={"numbers": blocking_numbers, "quotes": 0}, advisory={"entities": entities},
                number_total=4, spans=spans)


@pytest.fixture
def built(tmp_path):
    r = Run(tmp_path)
    # A 완주: 생성 2회(첫 초안 수치 차단), judge 주장 2개 중 1개 위치 추적, 연결 차이 1
    r.item("r1-c1", "eval", 2, size=4)
    r.call("r1-c1", "cluster_eval")
    r.call("r1-c1", "newsletter_content_gen", http=((429, None), (200, (1000, 300, 1400))))
    r.call("r1-c1", "newsletter_meta_gen", kw=4, inv=1)
    r.call("r1-c1", "newsletter_content_gen", ga=2)
    r.call("r1-c1", "newsletter_meta_gen", ga=2, kw=5, inv=0)
    r.call("r1-c1", "newsletter_eval", ga=2, sf=1, http=((200, (500, 10, 600)), (200, (500, 30, 600))))
    r.call("r1-c1", "tone_convert", ga=1)
    r.node("r1-c1", "eval_cluster", decision="PASS", confidence=0.9, n_outliers=0, n_sub_groups=0,
           sub_group_sizes=[], llm_called=True, call_parsed=True)
    r.node("r1-c1", "check_faithfulness", **_faith(1, 1, False, entities=2))
    r.node("r1-c1", "check_faithfulness", **_faith(0, 2, True))
    r.node("r1-c1", "eval_newsletter", attempt=2, decision="FAIL", criteria={"faithfulness": 2, "coverage": 4,
                                                                             "coherence": 4, "style": 4},
           judge_unavailable=False, n_unsupported_claims=2, claims_located=1)
    r.node("r1-c1", "convert_tone", tone_round=1, skipped=False)
    r.node("r1-c1", "check_tone_drift", tone_round=1, gate_passed=True, blocking={}, advisory={})
    r.node("r1-c1", "save_newsletter", newsletter_id=1, link_gap=1)
    r.text("draft_attempt", "r1-c1", attempt=1, sha256="1" * 64, fallback=[],
           draft={"title": FIRST_TITLE, "sentence": "도서관을 새로 짓는다는 소식이다", "content": FIRST_CONTENT})
    r.text("saved_formal", "r1-c1", news_letter_id=1, formal_sha256="2" * 64, tone_fallback=None,
           formal={"title": "가람시 도서관", "sentence": "가람시가 120억 원으로 도서관을 짓는다", "content": "본문"},
           converted={"title": "📰 가람시 도서관", "summary": "가람시가 130억 원으로 도서관을 지어요", "content": "본문"})
    r.end("r1-c1", "completed", wall_s=30.0, gen_attempts=2, cluster_eval_attempts=1, tone_rounds=1,
          tone_validation_calls=1, root_cause="none")
    # B skipped: 첫 평가 FAIL(서브그룹 2개, 둘째도 3건) → 둘째 평가 호출이 전송 실패
    r.item("r1-c2", "eval", 3, size=6)
    r.call("r1-c2", "cluster_eval")
    r.call("r1-c2", "cluster_eval", ga=2, parsed=False, error="transport", http=(("timeout", None), (503, None)))
    r.node("r1-c2", "eval_cluster", decision="FAIL", confidence=0.4, n_outliers=0, n_sub_groups=2,
           sub_group_sizes=[3, 3], llm_called=True, call_parsed=True)
    r.node("r1-c2", "handle_cluster_fail", skipped=False, dropped_subgroups_ge3=1)
    r.node("r1-c2", "eval_cluster", decision="FAIL", confidence=0.0, llm_called=True, call_parsed=False)
    r.node("r1-c2", "handle_cluster_fail", skipped=True, dropped_subgroups_ge3=0)
    r.end("r1-c2", "skipped", wall_s=10.0, cluster_eval_attempts=2)
    # C 생성기 폴백: 메타 호출 length
    r.item("r2-c3", "eval", 4)
    r.call("r2-c3", "cluster_eval")
    r.call("r2-c3", "newsletter_content_gen")
    r.call("r2-c3", "newsletter_meta_gen", parsed=False, error="length")
    r.node("r2-c3", "check_faithfulness", attempt=1, gate_passed=False, reason="generator_fallback")
    r.end("r2-c3", "failed:generator_fallback", wall_s=12.0, gen_attempts=1, cluster_eval_attempts=1)
    # D 사실성 차단 3회
    r.item("r2-c4", "eval", 5)
    r.call("r2-c4", "cluster_eval")
    for a in (1, 2, 3):
        r.call("r2-c4", "newsletter_content_gen", ga=a)
        r.call("r2-c4", "newsletter_meta_gen", ga=a, kw=5, inv=0)
        r.node("r2-c4", "check_faithfulness", **_faith(2, a, False))
    r.end("r2-c4", "failed:faithfulness", wall_s=40.0, gen_attempts=3, cluster_eval_attempts=1)
    # E 예산 중단, F 미시작, G 중단
    r.item("r3-c5", "eval", 6)
    r.call("r3-c5", "cluster_eval", http=((200, (1000, 100, 1100)),))
    r.end("r3-c5", "aborted_budget", root_cause="aborted_budget")
    r.item("r3-c6", "eval", 7)
    r.end("r3-c6", "not_started", reason="stop:budget")
    r.item("r3-c7", "eval", 8)
    r.end("r3-c7", "interrupted", root_cause="interrupted")
    # warmup
    r.item("r1-c9", "warmup", 0)
    r.call("r1-c9", "cluster_eval")
    r.end("r1-c9", "completed", root_cause="none")
    manifest = r.finish()
    record = dict(PREREG["pre_run_record"], code_sha="c" * 40, colab_cu_used=0.3)
    prereg = dict(PREREG, pre_run_record=record)
    return r, manifest, prereg


@pytest.fixture
def report(built):
    r, manifest, prereg = built
    return rep.build_report(r.dir, prereg=prereg, manifest_path=manifest)


def test_denominator_is_processed_eval_clusters_only(report):
    m1 = report["metrics"]["1_completion"]
    assert report["denominator"]["n_p"] == 4
    assert m1["completion_rate"] == {"k": 1, "n": 4, "rate": 0.25, "wilson95": rep.wilson(1, 4)}
    assert m1["excluded_from_denominator"]["aborted_budget"]["count"] == 1
    assert m1["excluded_from_denominator"]["aborted_budget"]["cost_usd"] == pytest.approx((1000 * 0.3 + 100 * 2.5) / 1e6)
    assert m1["excluded_from_denominator"]["not_started"]["count"] == 1
    assert m1["excluded_from_denominator"]["interrupted"]["count"] == 1 and m1["length_bias_caution"]
    assert report["header"]["n_aborted_or_not_started"] == 3 and report["header"]["partial_run"]


def test_root_causes_follow_the_registered_first_match_order(report):
    causes = {t["item_id"]: t["root_cause"] for t in report["metrics"]["13_failure_taxonomy"]["clusters"]}
    assert causes == {"r1-c1": "none", "r1-c2": "transport", "r2-c3": "length", "r2-c4": "faithfulness_block",
                      "r3-c5": "aborted_budget", "r3-c6": None, "r3-c7": "interrupted", "r1-c9": "none"}
    events = {t["item_id"]: t["events"] for t in report["metrics"]["13_failure_taxonomy"]["clusters"]}
    assert events["r1-c2"] == {"call_failed_transport": 1, "http_5xx": 1, "transport_timeout": 1}
    assert events["r1-c1"]["http_429"] == 1 and events["r1-c1"]["schema_failures"] == 1


@pytest.mark.parametrize("calls,expected", [
    ([{"purpose": "newsletter_content_gen", "graph_attempt": 1, "call_seq": 1, "parsed": False, "error": "schema"},
      {"purpose": "newsletter_meta_gen", "graph_attempt": 1, "call_seq": 2, "parsed": False, "error": "length"}], "length"),
    ([{"purpose": "newsletter_content_gen", "graph_attempt": 1, "call_seq": 1, "parsed": False, "error": "content_filter"},
      {"purpose": "newsletter_meta_gen", "graph_attempt": 1, "call_seq": 2, "parsed": False, "error": "deadline_transport"}],
     "transport"),
    ([{"purpose": "newsletter_content_gen", "graph_attempt": 1, "call_seq": 1, "parsed": True, "error": None},
      {"purpose": "newsletter_meta_gen", "graph_attempt": 1, "call_seq": 2, "parsed": True, "error": None}],
     "generator_fallback"),
])
def test_transport_beats_length_beats_content_filter_beats_schema(calls, expected):
    assert rep.root_cause({"outcome": "failed:generator_fallback"}, calls) == expected


def test_judge_unavailable_and_parsed_cluster_fail_and_exceptions():
    judge = [{"purpose": "newsletter_eval", "call_seq": 4, "parsed": False, "error": "deadline_schema"}]
    assert rep.root_cause({"outcome": "failed:judge_unavailable"}, judge) == "schema"
    assert rep.root_cause({"outcome": "failed:judge_unavailable"}, [{"purpose": "newsletter_eval", "call_seq": 4,
                                                                     "parsed": True}]) == "judge_unavailable"
    assert rep.root_cause({"outcome": "skipped"}, [{"purpose": "cluster_eval", "call_seq": 1, "parsed": True}]) == \
        "cluster_eval_fail"
    assert rep.root_cause({"outcome": "error"}, []) == "exception"
    assert rep.root_cause({"outcome": "failed:save_error"}, []) == "save_error"
    assert rep.root_cause({"outcome": "aborted_infra"}, []) == "aborted_infra"


def test_call_and_http_level_metrics(report):
    m = report["metrics"]
    # 처리 완료 eval의 응답 받은 호출: A 7, B 1(둘째는 응답 없음), C 3(메타 length도 응답은 받았다), D 7 → 18
    assert m["2_first_try_schema_pass"]["overall"]["n"] == 18
    assert m["2_first_try_schema_pass"]["overall"]["k"] == 16       # A의 judge(sf=1), C의 메타(length)가 아니다
    assert m["2_first_try_schema_pass"]["by_purpose"]["newsletter_eval"] == rep.ratio(0, 1)
    assert m["3_retries"]["rate_429"]["k"] == 1 and m["3_retries"]["transport_failures"]["timeout"] == 1
    assert m["3_retries"]["transport_failures"]["5xx"] == 1 and m["3_retries"]["schema_failures_sum"] == 1
    assert m["3_retries"]["graph"]["generation_attempts_per_cluster"]["max"] == 3.0
    tok = m["5_tokens_cost"]["tokens_processed_eval"]
    assert tok["hidden_tokens"] == sum(30 for _ in range(16)) + 100 + 90 + 70   # 기본 30 x 16 + A의 큰 응답들
    assert m["5_tokens_cost"]["cost_kind_counts"]["error_response"] == 2
    assert m["5_tokens_cost"]["cost_usd"]["preflight"] == 0.0
    assert m["5_tokens_cost"]["models_responded"] == ["gemini-3.5-flash-lite-001"]
    assert m["5_tokens_cost"]["prompt_chars_per_token_first_attempt"]["cluster_eval"]["median"] == 1.2


def test_checker_judge_cluster_evaluator_and_link_metrics(report):
    m = report["metrics"]
    assert m["6_faithfulness_checker"]["first_draft"]["unsupported_numbers_ge_1"] == rep.ratio(2, 2)
    assert m["6_faithfulness_checker"]["final_draft"]["unsupported_numbers_ge_1"] == rep.ratio(1, 2)
    assert m["6_faithfulness_checker"]["first_draft"]["advisory_entities_ge_1"] == rep.ratio(1, 2)
    assert m["6_faithfulness_checker"]["generator_fallback_drafts"] == 1
    assert m["8_judge_shadow"]["claim_location_success"] == rep.ratio(1, 2)
    assert m["8_judge_shadow"]["pass_rate_default_thresholds"] == rep.ratio(0, 1)
    assert m["9_cluster_evaluator"]["second_subgroup_ge3_dropped_C15"] == 1
    assert m["9_cluster_evaluator"]["decisions_not_parsed"] == {"FAIL": 1}
    assert m["10_link_integrity"]["excluded_but_linked_total"] == 1
    assert m["7_tone_drift"]["offline_saved_sentence_drift"] == rep.ratio(1, 1)   # 120억 → 130억(C4)


def test_style_rules_on_the_first_draft(report):
    first = report["metrics"]["11_style_rules_shadow"]["first_draft"]
    cur = first["current_prompt_rules"]
    assert first["n"] == 1
    assert cur["title_gt_15"]["k"] == 1 and cur["banned_ge_1"]["k"] == 1 and cur["paragraphs_lt_4"]["k"] == 0
    assert cur["keywords_lt_5"]["k"] == 1 and cur["invalid_category_ge_1"]["k"] == 1
    assert first["raw"]["banned_count"]["values"] == [40.0]


def test_wilson_matches_the_value_quoted_in_a8():
    lo, hi = rep.wilson(10, 15)
    assert (round(lo, 2), round(hi, 2)) == (0.42, 0.85)
    assert rep.wilson(0, 0) is None


def test_balance_reconciliation_rows():
    ok = rep.balance_reconciliation(0.50, {"before": 8000, "after": 7300, "fx": 1400, "other_gemini_calls": False})
    assert ok["status"].startswith("일치") and ok["delta_usd"] == pytest.approx(0.5)
    bad = rep.balance_reconciliation(0.50, {"before": 8000, "after": 7000, "fx": 1400})
    assert bad["status"] == "원장 불일치" and bad["threshold_usd"] == pytest.approx(0.10)
    assert rep.balance_reconciliation(0.5, {"before": 8000, "after": 7300, "fx": 1400, "other_gemini_calls": True})[
        "status"] == "대조 불가"
    assert rep.balance_reconciliation(0.5, None)["status"] == "미기록"


def test_no_text_leaks_and_a_planted_fragment_is_refused(built, report, tmp_path):
    r, _, _ = built
    corpus = rep.leak_corpus(r.dir, None)
    assert FIRST_CONTENT in corpus
    paths = rep.write_report(report, tmp_path / "out", texts=corpus)
    rendered = paths["json"].read_text(encoding="utf-8") + paths["md"].read_text(encoding="utf-8")
    assert "가람시는 새 도서관" not in rendered and FIRST_TITLE not in rendered
    planted = dict(report, notes=["예시: " + FIRST_CONTENT[5:30]])
    with pytest.raises(rep.LeakError):
        rep.write_report(planted, tmp_path / "leak", texts=corpus)
    assert not (tmp_path / "leak").exists()
    short = dict(report, notes=[FIRST_TITLE])
    with pytest.raises(rep.LeakError):
        rep.write_report(short, tmp_path / "leak2", texts=[FIRST_TITLE])


def test_header_carries_the_registered_fields(report):
    header = report["header"]
    for key in PREREG["report"]["header"]:
        if key in ("export_t0", "day_windows", "embeddings_sha256", "bucket_allocation", "seed", "processing_order",
                   "cost_usd", "billing_fx", "cost_krw", "colab_cu", "label_hours_actual", "resume_count",
                   "results_viewed", "declared_deviations", "command", "code_sha", "head_sha", "run_at", "n_eval",
                   "n_warmup", "n_processed", "n_aborted_or_not_started"):
            assert key in header, key
    assert header["resume_count"] == 1 and header["processing_order"][0] == "r1-c9"
    assert report["human_labels"] == {"status": "pending"}


def test_429_ratio_is_reported_over_n_p_and_over_every_sent_attempt(built):
    """A8.3 #3의 429 비율은 "보낸 HTTP 시도 전체"가 분모다. pre-flight·warmup·중단 클러스터의 시도도 보낸 시도라
    그 분모의 값을 따로 싣고, n_p 클러스터만의 값과 분모 이름을 함께 적는다."""
    r, manifest, prereg = built
    sent = [json.loads(l) for l in (r.dir / ins.HTTP_FILE).read_text(encoding="utf-8").splitlines()]
    extra_429 = {**sent[0], "status": 429, "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
                 "hidden_tokens": None, "cost_usd": 0.0, "cost_kind": "error_response", "finish_reason": None}
    r._w(ins.HTTP_FILE, {**extra_429, "cluster_id": None, "run_k": None, "purpose": "cluster_eval"})   # pre-flight
    r._w(ins.HTTP_FILE, {**extra_429, "cluster_id": 5, "run_k": 3})                                    # 예산 중단 클러스터
    m3 = rep.build_report(r.dir, prereg=prereg, manifest_path=manifest)["metrics"]["3_retries"]
    assert m3["rate_429"]["k"] == 1                                    # n_p 클러스터의 시도만
    assert m3["rate_429_all_sent"] == rep.ratio(3, len(sent) + 2)      # 보낸 시도 전체
    assert m3["http_status_counts_all_sent"]["429"] == 3 and m3["http_status_counts"]["429"] == 1
    assert set(m3["denominators"]) == {"rate_429", "http_status_counts", "rate_429_all_sent",
                                       "http_status_counts_all_sent"}
