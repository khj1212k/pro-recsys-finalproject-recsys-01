# C10 축소판 (docs/adr/0010 Addendum 2026-10-18): 재호출해도 소용없는 실패는 그 클러스터를 다시 생성하지 않고,
# 클러스터 평가 호출이 그런 실패면 skipped가 아니라 failure_reason=infra/length/content_filter로 기록한다.
#
# 대부분은 실제 그래프 + 실제 role 클라이언트(어댑터·지출 가드) + 페이크 전송 계층이다. 전송 계층을 거치지 않고
# 결과를 직접 주입하는 시나리오(LLMResult 페이크)는 지출 가드가 없는 클라이언트도 같은 정책을 따르는지 본다.
#
# "추가 호출 0"은 전송 계층의 요청 수(call_count)로 센다.
import os
import sys
from types import SimpleNamespace

import openai
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests import llm_graph_fakes as fx  # noqa: E402
from tests.llm_fakes import FakeLLMClient, make_status_error  # noqa: E402

from core.llm.budget import LLMCircuitOpen  # noqa: E402
from core.llm.client import LLMResult, LLMUsage  # noqa: E402
from core.llm.schemas import ClusterEval  # noqa: E402
from core.tone_converter import ToneConverter  # noqa: E402
from pipeline.stages import run_clusters_bounded, summarize_cluster_outcome  # noqa: E402
from workflow.graph import compile_workflow  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, llm_ledger_init):
    fx.configure_env(monkeypatch, tmp_path, llm_ledger_init)
    yield
    fx.teardown_env()


@pytest.fixture
def saved(monkeypatch):
    return fx.no_db(monkeypatch)


def _calls(fake):
    return fake.chat.completions.call_count


# ---------------------------------------------------------------------------
# 분류: 무엇이 "재호출해도 소용없는" 실패인가
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("error,status,reason", [
    ("kill_switch", None, "infra"),
    ("length", None, "length"),
    ("content_filter", None, "content_filter"),
    ("Error code: 401", 401, "infra"),
    ("Error code: 402", 402, "infra"),
    ("Error code: 403", 403, "infra"),
    ("Error code: 404", 404, "infra"),
    # 아래는 기존 정책 그대로 다시 시도하는 실패다
    ("max retries (schema 2) exhausted: x", None, None),
    ("max retries (10) exhausted: 503", 503, None),
    ("call deadline (180s) reached: 429", 429, None),
    ("Error code: 400", 400, None),
    ("boom", None, None),
])
def test_no_retry_reason_table(error, status, reason):
    result = fx.blocked_result(error, http_status=status)

    assert result.no_retry_reason == reason


def test_a_successful_result_never_has_a_no_retry_reason():
    ok = LLMResult(text="x", parsed=ClusterEval(decision="PASS"), usage=LLMUsage(), latency_s=0.0, attempts=1,
                   provider="fake", model="fake-model")

    assert ok.no_retry_reason is None


# ---------------------------------------------------------------------------
# 처음부터 막혀 있다
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403, 404])
def test_http_error_from_the_start_is_recorded_as_infra_not_skipped(saved, status):
    gen, judge = fx.role_transports([], [make_status_error(status, "denied")])

    final = compile_workflow().invoke(fx.initial_state())

    assert final["failed_clusters"] == [1]
    assert final["skipped_clusters"] == []  # 수정 전: 클러스터 평가 호출 실패가 FAIL로 읽혀 skipped
    assert final["failure_reason"] == "infra"
    assert final["completed_newsletters"] == [] and saved == []
    assert _calls(judge) == 1 and _calls(gen) == 0  # 클러스터 평가 1회, 생성·문체 변환 요청 없음
    assert summarize_cluster_outcome(final, 1)["status"] == "failed"
    assert summarize_cluster_outcome(final, 1)["failure_reason"] == "infra"


def test_payment_required_from_the_start_costs_one_call_for_the_whole_run(saved):
    """402는 지출 가드가 런 전체를 멈춘다(ADR 0035). 클러스터가 여럿이어도 요청은 한 번이고 skipped는 없다."""
    gen, judge = fx.role_transports([], [make_status_error(402, "prepay required")])

    def process(cid, idx):
        return summarize_cluster_outcome(compile_workflow().invoke(fx.initial_state(cluster_id=cid)), cid)

    outcomes = run_clusters_bounded(process, [(1, 0), (2, 1), (3, 2)], [], max_workers=1)

    assert outcomes[1]["status"] == "stopped" and outcomes[1]["failure_reason"] == "llm_circuit_open"
    assert outcomes[2]["status"] == outcomes[3]["status"] == "not_started"
    assert not any(o["status"] == "skipped" for o in outcomes.values())
    assert _calls(judge) == 1 and _calls(gen) == 0 and saved == []


@pytest.mark.parametrize("cluster_id", [1, 2, 3])
def test_blocked_results_without_a_guard_cost_one_call_per_cluster(monkeypatch, saved, cluster_id):
    """지출 가드를 거치지 않는 클라이언트가 402를 결과로 돌려줘도 클러스터당 호출 1회 뒤 infra로 닫는다."""
    judge = FakeLLMClient(results=[fx.blocked_result("Error code: 402", http_status=402)])
    generator, tone = FakeLLMClient(results=[]), FakeLLMClient(results=[])
    fx.wire_clients(monkeypatch, judge, generator, tone)

    final = compile_workflow().invoke(fx.initial_state(cluster_id=cluster_id))

    assert final["failed_clusters"] == [cluster_id] and final["skipped_clusters"] == []
    assert final["failure_reason"] == "infra"
    assert (judge.call_count, generator.call_count, tone.call_count) == (1, 0, 0)


def test_content_filter_on_cluster_eval_is_recorded_as_content_filter(saved):
    gen, judge = fx.role_transports([], [openai.ContentFilterFinishReasonError(completion=None)])

    final = compile_workflow().invoke(fx.initial_state())

    assert final["failed_clusters"] == [1] and final["skipped_clusters"] == []
    assert final["failure_reason"] == "content_filter"
    assert _calls(judge) == 1 and _calls(gen) == 0


def test_length_on_cluster_eval_is_recorded_as_length(saved):
    length = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
    gen, judge = fx.role_transports([], [length])

    final = compile_workflow().invoke(fx.initial_state())

    assert final["failed_clusters"] == [1] and final["skipped_clusters"] == []
    assert final["failure_reason"] == "length"
    assert _calls(judge) == 1 and _calls(gen) == 0


def test_kill_switch_from_the_start_sends_no_request_and_is_recorded_as_infra(monkeypatch, saved):
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")
    gen, judge = fx.role_transports([], [])

    final = compile_workflow().invoke(fx.initial_state())

    assert final["failed_clusters"] == [1]
    assert final["skipped_clusters"] == []  # 수정 전 tests/test_llm_kill_switch_workflow.py는 skipped == [1]을 기대했다
    assert final["failure_reason"] == "infra"
    assert final["cluster_eval"]["decision"] == "FAIL" and final["cluster_eval"]["llm_failure"] == "infra"
    assert _calls(judge) == 0 and _calls(gen) == 0 and saved == []


def test_a_genuine_fail_verdict_is_still_skipped():
    """대조군: 모델이 스키마를 지켜 FAIL을 답하면(군집이 나쁨) 예전처럼 skipped다."""
    gen, judge = fx.role_transports([], [fx.cluster_fail()])

    final = compile_workflow().invoke(fx.initial_state())

    assert final["skipped_clusters"] == [1] and final["failed_clusters"] == []
    assert final.get("failure_reason") is None
    assert _calls(judge) == 1 and _calls(gen) == 0


# ---------------------------------------------------------------------------
# 처리 도중 막힌다
# ---------------------------------------------------------------------------

def test_payment_required_mid_cluster_stops_with_no_further_calls(saved):
    """메타 생성 요청이 402: 지출 가드가 런을 멈추고, 그 뒤 judge·문체 변환 요청과 다음 클러스터 요청은 0이다."""
    gen, judge = fx.role_transports([fx.content(), make_status_error(402, "prepay required")], [fx.cluster_pass()])

    with pytest.raises(LLMCircuitOpen):
        compile_workflow().invoke(fx.initial_state())
    with pytest.raises(LLMCircuitOpen):  # 같은 런의 다음 클러스터
        compile_workflow().invoke(fx.initial_state(cluster_id=2))

    assert _calls(gen) == 2 and _calls(judge) == 1
    assert saved == []


def test_blocked_generation_mid_cluster_skips_the_meta_call_and_never_regenerates(monkeypatch, saved):
    """본문 생성이 402 결과(가드 없는 클라이언트): 메타 호출도, 두 번째 생성도, judge·문체 변환도 없다."""
    judge = FakeLLMClient(results=[fx.cluster_pass_result()])
    generator = FakeLLMClient(results=[fx.blocked_result("Error code: 402", http_status=402)])
    tone = FakeLLMClient(results=[])
    fx.wire_clients(monkeypatch, judge, generator, tone)

    final = compile_workflow().invoke(fx.initial_state())

    assert (judge.call_count, generator.call_count, tone.call_count) == (1, 1, 0)
    assert final["newsletter_retry_count"] == 1  # 생성 시도 1회
    assert final["failed_clusters"] == [1] and final["skipped_clusters"] == []
    assert final["failure_reason"] == "infra"
    assert final["faithfulness_report"]["reason"] == "generator_fallback"  # 로컬 폴백 초안은 발행되지 않는다
    assert saved == []


def test_kill_switch_engaged_mid_run_ends_after_one_generation_attempt(monkeypatch, saved):
    """클러스터 평가까지는 나가고, 그 뒤 킬 스위치가 켜진다: 생성은 1회 시도(요청 0)로 끝난다."""
    def engage_after_first_judge_call(_mode, _kwargs):
        monkeypatch.setenv("LLM_KILL_SWITCH", "1")

    gen, judge = fx.role_transports([], [fx.cluster_pass()], on_judge_call=engage_after_first_judge_call)

    final = compile_workflow().invoke(fx.initial_state())

    assert _calls(judge) == 1 and _calls(gen) == 0  # 킬 스위치는 요청을 보내기 전에 막는다
    assert final["newsletter_retry_count"] == 1
    assert final["failed_clusters"] == [1] and final["skipped_clusters"] == []
    assert final["failure_reason"] == "infra"
    assert final["newsletter_draft"]["_fallback"] == ["content", "meta"]
    assert saved == []


def test_length_on_content_generation_does_not_regenerate(saved):
    length = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))
    gen, judge = fx.role_transports([length], [fx.cluster_pass()])

    final = compile_workflow().invoke(fx.initial_state())

    assert _calls(gen) == 1  # 같은 max_tokens로 다시 해도 같은 이유로 잘린다 - 메타 호출도 없다
    assert final["newsletter_retry_count"] == 1
    assert final["failed_clusters"] == [1] and final["failure_reason"] == "length"
    assert saved == []


def test_meta_generation_blocked_after_a_good_body_still_ends_the_cluster(saved):
    gen, judge = fx.role_transports(
        [fx.content(), openai.ContentFilterFinishReasonError(completion=None)], [fx.cluster_pass()])

    final = compile_workflow().invoke(fx.initial_state())

    assert _calls(gen) == 2 and _calls(judge) == 1
    assert final["failed_clusters"] == [1] and final["failure_reason"] == "content_filter"
    assert final["newsletter_draft"]["_fallback"] == ["meta"]
    assert saved == []


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_judge_call_blocked_ends_the_cluster_in_both_judge_modes(monkeypatch, saved, mode):
    monkeypatch.setattr(fx.nodes_module.Settings, "JUDGE_GATE_MODE", mode)
    gen, judge = fx.role_transports(
        [fx.content(), fx.meta()], [fx.cluster_pass(), make_status_error(401, "denied")])

    final = compile_workflow().invoke(fx.initial_state())

    assert _calls(gen) == 2 and _calls(judge) == 2  # 생성 1회, 뉴스레터 평가 1회 - 수정 전에는 생성·평가를 3번 돌았다
    assert final["newsletter_retry_count"] == 1
    assert final["failed_clusters"] == [1] and final["failure_reason"] == "infra"
    assert saved == []


def test_tone_converter_stops_validation_retries_when_the_call_is_blocked():
    """수정 전: 검증 재시도(1 + MAX_RETRY_TONE_VALIDATION)만큼 같은 막힌 호출을 반복했다."""
    for blocked in (fx.blocked_result("kill_switch", attempts=0),
                    fx.blocked_result("Error code: 402", http_status=402)):
        client = FakeLLMClient(results=[blocked])  # 두 번째 호출이 오면 AssertionError

        converted = ToneConverter(llm_client=client).convert(
            {"title": "삼성전자 HBM 증설입니다", "sentence": "요약입니다", "content": "삼성전자가 증설한다고 밝혔습니다.",
             "keywords": ["HBM"]})

        assert client.call_count == 1
        assert converted["title"].startswith("📰")  # 기존 결정론적 폴백 그대로


# ---------------------------------------------------------------------------
# 기존 재시도 정책이 그대로인 실패
# ---------------------------------------------------------------------------

def test_exhausted_transport_retries_still_regenerate_up_to_the_generation_cap(monkeypatch, saved):
    """전송 계층 재시도 소진(503)·스키마 재시도 소진은 막힌 실패가 아니다 - 다음 생성에서 달라질 수 있다."""
    exhausted = {"parsed": None, "text": None, "error": "max retries (10) exhausted: Error code: 503"}
    schema_exhausted = {"parsed": None, "text": None, "error": "max retries (schema 2) exhausted: invalid"}
    judge = FakeLLMClient(results=[fx.cluster_pass_result()])
    generator = FakeLLMClient(results=[exhausted, exhausted, schema_exhausted, schema_exhausted, exhausted, exhausted])
    tone = FakeLLMClient(results=[])
    fx.wire_clients(monkeypatch, judge, generator, tone)

    final = compile_workflow().invoke(fx.initial_state())

    assert generator.call_count == 6  # MAX_RETRY_NEWSLETTER_EVAL(3)회 × (본문 + 메타)
    assert final["failed_clusters"] == [1]
    assert final["failure_reason"] == "generator_fallback"
    assert saved == []
