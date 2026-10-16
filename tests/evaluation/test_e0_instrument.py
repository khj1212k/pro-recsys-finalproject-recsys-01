"""E0 계측과 예산 가드(evaluation/llm/e0_instrument.py, ADR 0009 A8.4·A8.5) - 운영 어댑터 + openai SDK + 페이크 HTTP.

운영 OpenAICompatLLMClient를 그대로 쓰고 HTTP 전송 계층만 httpx2.MockTransport로 바꾼다. 그래서 어댑터의 재시도·
스키마 검증·지출 가드(ADR 0035)가 실제로 돌고, E0의 httpx 훅·전송 관찰자·complete() 래퍼가 그 위에서 기록한다.
재시도 사이의 대기만 없앤다(테스트 시간).
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

import httpx2  # noqa: E402
import openai  # noqa: E402

from evaluation.llm import e0_instrument as ins  # noqa: E402
from evaluation.llm import e0_registered as reg  # noqa: E402
from tests.frozen_news_fakes import use_tmp_main_checkout  # noqa: E402

PRICE = (0.30, 2.50)
MARK_PROMPT = "가람시청비밀문장"
MARK_REPLY = "누리구응답문장"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    import core.llm.adapters as adapters

    use_tmp_main_checkout(monkeypatch, tmp_path)
    monkeypatch.setattr(adapters.OpenAICompatLLMClient, "_wait_before_retry", staticmethod(lambda b, s: True))


def _body(model="gemini-3.5-flash-lite", content=None, usage=True, finish="stop", tokens=(100, 20, 150)):
    body = {"id": "x", "object": "chat.completion", "created": 0, "model": model, "system_fingerprint": "fp",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": content if content is not None else
                                                 json.dumps({"content": MARK_REPLY}, ensure_ascii=False)},
                         "finish_reason": finish}]}
    if usage:
        body["usage"] = {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "total_tokens": tokens[2],
                         "completion_tokens_details": {"reasoning_tokens": tokens[2] - tokens[0] - tokens[1]}}
    return body


class Script:
    """요청마다 다음 응답을 낸다. 항목: dict(200 본문) / int(상태 코드) / "timeout" / callable."""

    def __init__(self, *steps, delay=0.0):
        self.steps = list(steps)
        self.calls = 0
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, request):
        with self.lock:
            self.calls += 1
            step = self.steps.pop(0) if self.steps else _body()
        if self.delay:
            time.sleep(self.delay)
        if callable(step):
            step = step(request)
        if step == "timeout":
            raise httpx2.ReadTimeout("t", request=request)
        if isinstance(step, int):
            return httpx2.Response(step, json={"error": {"code": step}})
        return httpx2.Response(200, json=step)


def adapter(handler, model="gemini-3.5-flash-lite"):
    from core.llm.adapters import OpenAICompatLLMClient

    sdk = openai.OpenAI(api_key="t", base_url="https://example.invalid/v1/", timeout=60, max_retries=0,
                        http_client=openai.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    return OpenAICompatLLMClient(provider="gemini", model=model, client=sdk)


def recorder(tmp_path, name="run-a", **kw):
    run_dir = tmp_path / "data" / "experiments" / "e0" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    return ins.Recorder(run_dir, run_name=name, e0_root=run_dir.parent, prices=lambda m: PRICE, **kw)


def instrumented(tmp_path, handler, **kw):
    rec = recorder(tmp_path, **kw)
    real = adapter(handler)
    ins.attach_http_instrumentation(real, rec)
    return rec, ins.RecordingClient(real, rec)


def call(client, purpose="newsletter_content_gen", ctx=None, max_tokens=8192):
    from core.llm.schemas import NewsletterContent

    ctx = ctx or ins.ClusterContext(item_id="r1-c1", cluster_id=1, run_k=1, split="eval")
    messages = [{"role": "user", "content": f"{MARK_PROMPT} 기사 묶음을 정리하라"}]
    return ins.run_in_cluster(ctx, lambda: client.complete(messages, schema=NewsletterContent, purpose=purpose,
                                                           temperature=0.2, max_tokens=max_tokens)), ctx


def rows(rec, name):
    return rec.read(name)


# ---------------------------------------------------------------- 두 층의 기록


def test_one_call_writes_one_http_row_and_one_call_row_with_exactly_the_registered_fields(tmp_path):
    rec, client = instrumented(tmp_path, Script(_body()))
    result, ctx = call(client)
    assert result.parsed is not None
    http, calls = rows(rec, ins.HTTP_FILE), rows(rec, ins.CALLS_FILE)
    assert len(http) == 1 and len(calls) == 1
    assert sorted(http[0]) == sorted(reg.HTTP_ATTEMPT_FIELDS)
    assert sorted(calls[0]) == sorted(reg.CALL_FIELDS)
    h, c = http[0], calls[0]
    assert (h["cluster_id"], h["run_k"], h["purpose"], h["call_seq"], h["attempt_seq"], h["graph_attempt"]) == \
        (1, 1, "newsletter_content_gen", 1, 1, 1)
    assert (h["status"], h["prompt_tokens"], h["completion_tokens"], h["total_tokens"], h["hidden_tokens"],
            h["reasoning_tokens_reported"]) == (200, 100, 20, 150, 30, 30)
    assert h["cost_kind"] == "observed"
    assert h["cost_usd"] == pytest.approx((100 * 0.30 + (20 + 30) * 2.50) / 1e6)
    assert c["cost_usd"] == pytest.approx(h["cost_usd"]) and c["http_attempts"] == 1 and c["first_try_schema_pass"]
    totals = ins.Ledger.totals(rec.dir / ins.LEDGER_FILE)
    assert totals["open_ids"] == [] and totals["settled_usd"] == pytest.approx(h["cost_usd"])
    for name in (ins.HTTP_FILE, ins.CALLS_FILE, ins.LEDGER_FILE):
        text = (rec.dir / name).read_text(encoding="utf-8")
        assert MARK_PROMPT not in text and MARK_REPLY not in text  # 프롬프트·응답 텍스트는 기록하지 않는다


def test_schema_invalid_and_length_responses_are_billed_from_their_usage(tmp_path):
    rec, client = instrumented(tmp_path, Script(_body(content="{not json", tokens=(90, 10, 120)), _body()))
    result, _ = call(client)
    http = rows(rec, ins.HTTP_FILE)
    assert [h["cost_kind"] for h in http] == ["observed", "observed"]
    assert http[0]["cost_usd"] == pytest.approx((90 * 0.30 + (10 + 20) * 2.50) / 1e6) and http[0]["cost_usd"] > 0
    c = rows(rec, ins.CALLS_FILE)[0]
    assert (c["schema_failures"], c["parsed"], c["first_try_schema_pass"], c["responded"], c["http_attempts"]) == \
        (1, True, False, True, 2)

    rec2, client2 = instrumented(tmp_path / "b", Script(_body(content='{"content": "잘림', finish="length")))
    result, _ = call(client2)
    h = rows(rec2, ins.HTTP_FILE)[0]
    c = rows(rec2, ins.CALLS_FILE)[0]
    assert result.error == "length" and h["finish_reason"] == "length" and h["cost_kind"] == "observed"
    assert h["cost_usd"] > 0 and c["error"] == "length" and c["responded"] is True


def test_attempts_without_a_response_are_charged_the_worst_case(tmp_path):
    rec, client = instrumented(tmp_path, Script("timeout", _body(usage=False)))
    result, _ = call(client, max_tokens=8192)
    http = rows(rec, ins.HTTP_FILE)
    prompt_chars = len(f"{MARK_PROMPT} 기사 묶음을 정리하라")
    worst = (prompt_chars * 1.5 * 0.30 + 8192 * 2.50) / 1e6
    assert result.parsed is not None
    assert [(h["status"], h["transport_error"], h["cost_kind"]) for h in http] == [
        (None, "timeout", "unobserved_upper_bound"), (200, None, "unobserved_upper_bound")]
    assert http[0]["cost_usd"] == pytest.approx(worst) and http[1]["cost_usd"] == pytest.approx(worst)
    assert rows(rec, ins.CALLS_FILE)[0]["cost_usd"] == pytest.approx(2 * worst)


def test_error_responses_cost_zero_and_429_is_counted(tmp_path):
    rec, client = instrumented(tmp_path, Script(429, 500, _body()))
    call(client)
    http = rows(rec, ins.HTTP_FILE)
    assert [(h["status"], h["cost_kind"], h["cost_usd"]) for h in http[:2]] == [
        (429, "error_response", 0.0), (500, "error_response", 0.0)]
    assert rec.stop is None  # 429·5xx는 측정 대상이지 중단 사유가 아니다


# ---------------------------------------------------------------- 가드


def test_guard_refuses_the_attempt_that_would_cross_the_cap_and_nothing_is_sent_after(tmp_path):
    from core.llm import budget

    script = Script(_body(), _body(), _body())
    # 한 시도의 최악 비용 = 20자 × 1.5 × $0.30/1M + 8192 × $2.50/1M ≈ $0.020489, 첫 시도의 관측 비용 ≈ $0.000155.
    # 상한 $0.0206: 첫 시도는 0.020489 ≤ 상한이라 가고, 두 번째는 0.000155 + 0.020489 > 상한이라 보내지 않는다.
    rec, client = instrumented(tmp_path, script, cap_run_usd=0.0206)
    call(client)
    assert script.calls == 1
    with pytest.raises(ins.E0BudgetStop):
        call(client)
    assert script.calls == 1 and rec.stop["reason"] == "budget" and rec.stop["cap"] == "valid_run"
    assert isinstance(budget.run_stop(), ins.E0BudgetStop)  # 클라이언트 안 래치도 섰다 - 다른 워커도 멈춘다
    with pytest.raises(budget.LLMRunStop):
        call(client)
    assert script.calls == 1
    ledger = ins.Ledger.read(rec.dir / ins.LEDGER_FILE)
    assert len([r for r in ledger if r["type"] == "reserve"]) == 1   # 거부된 시도는 예약하지 않는다
    assert rec.spent()["run_usd"] <= 0.0206


def test_in_flight_requests_finish_and_no_request_starts_after_the_stop(tmp_path):
    """가드가 멈춘 뒤 진행 중이던 요청은 끝나고(타임아웃 안), 새 요청은 하나도 나가지 않는다."""
    started = []

    def slow(request):
        started.append(time.monotonic())
        time.sleep(0.4)
        return _body()

    script = Script(slow, slow, slow, slow)
    rec, client = instrumented(tmp_path, script, cap_run_usd=0.045)  # 진행 중 최악 비용 2건이면 상한에 닿는다
    results, errors = {}, {}

    def worker(i):
        ctx = ins.ClusterContext(item_id=f"r1-c{i}", cluster_id=i, run_k=1, split="eval")
        try:
            results[i] = call(client, ctx=ctx)[0]
        except BaseException as e:  # noqa: BLE001
            errors[i] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
        time.sleep(0.05)
    for t in threads:
        t.join(5)
    stop_at = time.monotonic()
    assert len(errors) == 1 and isinstance(next(iter(errors.values())), ins.E0BudgetStop)
    assert len(results) == 2 and script.calls == 2
    assert all(t < stop_at for t in started) and len(started) == 2
    assert ins.Ledger.totals(rec.dir / ins.LEDGER_FILE)["open_ids"] == []


def test_the_cumulative_cap_counts_earlier_runs(tmp_path):
    first = recorder(tmp_path, name="run-1")
    first.ledger.append({"type": "reserve", "attempt_id": "a000001", "worst_usd": 1.49})
    first.ledger.append({"type": "settle", "attempt_id": "a000001", "cost_usd": 1.49, "cost_kind": "observed"})
    rec, client = instrumented(tmp_path, Script(_body()), name="run-2")
    with pytest.raises(ins.E0BudgetStop):
        call(client)
    assert rec.stop["cap"] == "e0_cumulative" and rec.spent()["cumulative_usd"] == pytest.approx(1.49)


def test_reservations_left_by_a_dead_process_are_settled_at_worst_case(tmp_path):
    rec = recorder(tmp_path)
    rec.ledger.append({"type": "reserve", "attempt_id": "a000001", "worst_usd": 0.02, "run": "run-a", "cluster_id": 3,
                       "run_k": 1, "purpose": "cluster_eval", "call_seq": 1, "attempt_seq": 1, "graph_attempt": 1,
                       "provider": "gemini", "prompt_chars": 10, "max_tokens": 2048, "model_requested": "m",
                       "started_at": "2026-10-17T00:00:00Z"})
    again = recorder(tmp_path)
    http = rows(again, ins.HTTP_FILE)
    assert len(http) == 1 and http[0]["transport_error"] == "process_died" and http[0]["cost_usd"] == 0.02
    assert again.spent()["run_usd"] == pytest.approx(0.02)
    assert ins.Ledger.totals(again.dir / ins.LEDGER_FILE)["open_ids"] == []


# ---------------------------------------------------------------- 인프라 중단


def test_402_stops_the_run_as_infra_through_the_in_client_breaker(tmp_path):
    from core.llm import budget

    script = Script(402, _body())
    rec, client = instrumented(tmp_path, script)
    with pytest.raises(budget.LLMRunStop):
        _, ctx = call(client)
    assert rec.stop["reason"] == "infra" and script.calls == 1
    assert ins.stop_kind(budget.run_stop()) == "infra"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_and_model_errors_stop_the_run_and_the_next_call_sends_nothing(tmp_path, status):
    from core.llm import budget

    script = Script(status, _body())
    rec, client = instrumented(tmp_path, script)
    ctx = ins.ClusterContext(item_id="r1-c1", cluster_id=1, run_k=1, split="eval")
    result, _ = call(client, ctx=ctx)
    assert result.http_status == status and rec.stop["reason"] == "infra" and ctx.stopped == "infra"
    assert rows(rec, ins.CALLS_FILE)[0]["error"] == f"http_{status}"
    with pytest.raises(budget.LLMRunStop):
        call(client)
    assert script.calls == 1


def test_kill_switch_stops_the_run_as_infra(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")
    script = Script(_body())
    rec, client = instrumented(tmp_path, script)
    result, ctx = call(client)
    assert result.error == "kill_switch" and script.calls == 0
    assert rec.stop["reason"] == "infra" and ctx.stopped == "infra"
    assert rows(rec, ins.CALLS_FILE)[0]["error"] == "kill_switch"


# ---------------------------------------------------------------- 귀속


def test_three_workers_do_not_mix_attribution(tmp_path):
    def echo(request):
        time.sleep(0.01)
        return _body()

    script = Script(*([echo] * 30))
    rec, client = instrumented(tmp_path, script)
    contexts = {i: ins.ClusterContext(item_id=f"r1-c{i}", cluster_id=i, run_k=1, split="eval") for i in range(3)}

    def worker(i):
        for _ in range(10):
            call(client, ctx=contexts[i])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    http, calls = rows(rec, ins.HTTP_FILE), rows(rec, ins.CALLS_FILE)
    assert len(http) == len(calls) == 30
    for i in range(3):
        mine = sorted(h["call_seq"] for h in http if h["cluster_id"] == i)
        assert mine == list(range(1, 11))
        assert sorted(c["call_seq"] for c in calls if c["cluster_id"] == i) == list(range(1, 11))
    assert rec.instrumentation_errors == []


def test_tone_converter_local_fallback_is_flagged(tmp_path):
    from core.tone_converter import ToneConverter

    class Empty:
        provider, model = "fake", "fake"

        def complete(self, messages, **kwargs):
            from core.llm.client import LLMResult, LLMUsage

            return LLMResult(text=None, parsed=None, usage=LLMUsage(), latency_s=0.0, attempts=1, provider="fake",
                             model="fake", error="max retries (10) exhausted: x")

    Tracking = ins.make_tracking_tone_converter(ToneConverter)
    ctx = ins.ClusterContext(item_id="r1-c1", cluster_id=1, run_k=1, split="eval")
    out = ins.run_in_cluster(ctx, lambda: Tracking(llm_client=Empty()).convert(
        {"title": "제목", "sentence": "요약", "content": "본문입니다.", "keywords": []}))
    assert out["title"].startswith("📰") and ctx.tone_local_fallbacks == 1 and ctx.tone_round == 1
    assert ctx.events == [{"type": "tone_local_fallback", "tone_round": 1}]


def test_instrumentation_attaches_to_the_production_client_without_rebuilding_it(tmp_path):
    rec = recorder(tmp_path)
    real = adapter(Script())
    sdk_before, http_before = real._client, real._client._client
    ins.attach_http_instrumentation(real, rec)
    ins.attach_http_instrumentation(real, rec)  # 두 번 달아도 한 번만
    assert real._client is sdk_before and real._client._client is http_before
    assert http_before.event_hooks["request"].count(rec.on_request) == 1
    assert real._client.max_retries == 0 and real._client.timeout == 60
