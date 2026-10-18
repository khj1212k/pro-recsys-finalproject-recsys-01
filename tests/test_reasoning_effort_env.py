# C7 (docs/adr/0005 부록 2026-10-18): reasoning_effort를 역할별 env로 전달하고, usage의 total/thinking/cached
# 토큰을 LLMUsage와 llm_metrics에 (보고된 경우에만) 기록한다. 설정하지 않으면 요청 본문은 E0까지와 같다.
import os
import sys
from types import SimpleNamespace

import openai
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeOpenAIClient, make_response, make_status_error, make_timeout_error  # noqa: E402

import core.llm.adapters as adapters_module  # noqa: E402
import core.llm.registry as registry  # noqa: E402
from core.llm.adapters import OpenAICompatLLMClient  # noqa: E402
from core.llm.schemas import ClusterEval  # noqa: E402
from core.llm_metrics import LLMMetricsCollector  # noqa: E402

MESSAGES = [{"role": "user", "content": "x"}]
EFFORT_ENVS = ("GEN_REASONING_EFFORT", "JUDGE_REASONING_EFFORT", "TONE_REASONING_EFFORT")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in EFFORT_ENVS + ("GEN_PROVIDER", "GEN_MODEL", "JUDGE_PROVIDER", "JUDGE_MODEL", "TONE_PROVIDER",
                               "TONE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    registry.reset_registry()
    LLMMetricsCollector().reset()
    yield
    registry.reset_registry()


def _usage(prompt=100, completion=20, total=170, reasoning=50, cached=40):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def _parsed():
    return ClusterEval(decision="PASS", confidence=0.9)


# ---------------------------------------------------------------------------
# 역할별 env 해석
# ---------------------------------------------------------------------------

def test_unset_means_the_request_carries_no_reasoning_effort():
    for role in ("generator", "judge", "tone"):
        assert registry.resolve_reasoning_effort(role) is None
        assert registry.get_client(role).reasoning_effort is None


@pytest.mark.parametrize("raw,expected", [
    ("minimal", "minimal"), (" None ", "none"), ("LOW", "low"), ("medium", "medium"), ("high", "high"),
    ("", None), ("unset", None), ("DEFAULT", None),
])
def test_env_value_is_normalised(monkeypatch, raw, expected):
    monkeypatch.setenv("GEN_REASONING_EFFORT", raw)

    assert registry.resolve_reasoning_effort("generator") == expected


def test_unknown_value_is_rejected_instead_of_silently_ignored(monkeypatch):
    monkeypatch.setenv("JUDGE_REASONING_EFFORT", "minimum")

    with pytest.raises(ValueError, match="JUDGE_REASONING_EFFORT"):
        registry.resolve_reasoning_effort("judge")
    with pytest.raises(ValueError, match="JUDGE_REASONING_EFFORT"):
        registry.get_client("judge")


def test_each_role_reads_its_own_variable(monkeypatch):
    monkeypatch.setenv("GEN_REASONING_EFFORT", "minimal")
    monkeypatch.setenv("TONE_REASONING_EFFORT", "none")

    assert registry.get_client("generator").reasoning_effort == "minimal"
    assert registry.get_client("tone").reasoning_effort == "none"
    assert registry.get_client("judge").reasoning_effort is None


def test_roles_sharing_a_model_get_separate_clients_when_their_effort_differs(monkeypatch):
    """generator와 tone은 기본 모델이 같다. effort가 다르면 같은 인스턴스를 공유하면 안 된다."""
    assert registry.get_client("generator") is registry.get_client("tone")  # 기본: 지금까지처럼 하나

    registry.reset_registry()
    monkeypatch.setenv("GEN_REASONING_EFFORT", "minimal")
    generator, tone = registry.get_client("generator"), registry.get_client("tone")

    assert generator is not tone
    assert (generator.model, tone.model) == ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite")
    assert registry.get_client("generator") is generator  # 같은 설정이면 인스턴스를 재사용한다


def test_unset_effort_keeps_the_two_tuple_cache_key():
    """E0 합성 실행(evaluation/llm/e0_synthetic.py)이 (provider, model) 키로 페이크를 끼운다 - 그 키가 그대로여야 한다."""
    client = registry.get_client("generator")

    assert registry._instances[("gemini", "gemini-3.5-flash-lite")] is client


def test_provider_without_documented_support_rejects_a_configured_effort(monkeypatch):
    monkeypatch.setenv("GEN_PROVIDER", "openai")
    monkeypatch.setenv("GEN_REASONING_EFFORT", "minimal")

    with pytest.raises(ValueError, match="reasoning_effort"):
        registry.get_client("generator")


def test_client_for_accepts_an_explicit_effort(monkeypatch):
    client = registry.client_for("gemini", "gemini-3.5-flash-lite", reasoning_effort="none")

    assert client.reasoning_effort == "none"
    assert registry.client_for("gemini", "gemini-3.5-flash-lite") is not client


# ---------------------------------------------------------------------------
# 요청 본문
# ---------------------------------------------------------------------------

def test_structured_request_carries_the_effort_only_when_set():
    on = FakeOpenAIClient(responses=[make_response("{}", parsed=_parsed())])
    off = FakeOpenAIClient(responses=[make_response("{}", parsed=_parsed())])

    OpenAICompatLLMClient(provider="gemini", model="m", client=on, reasoning_effort="minimal").complete(
        MESSAGES, schema=ClusterEval, purpose="cluster_eval")
    OpenAICompatLLMClient(provider="gemini", model="m", client=off).complete(
        MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert on.chat.completions.parse_calls[0]["reasoning_effort"] == "minimal"
    assert "reasoning_effort" not in off.chat.completions.parse_calls[0]  # 미설정 요청은 E0까지와 같다


def test_json_mode_request_carries_the_effort_too():
    fake = FakeOpenAIClient(responses=[make_response('{"decision": "PASS", "confidence": 0.5}')])
    client = OpenAICompatLLMClient(provider="gemini", model="m", supports_json_schema=False, client=fake,
                                   reasoning_effort="none")

    client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert fake.chat.completions.create_calls[0]["reasoning_effort"] == "none"


# ---------------------------------------------------------------------------
# usage 확장과 llm_metrics
# ---------------------------------------------------------------------------

def test_reported_usage_reaches_the_result_and_the_metrics_record():
    fake = FakeOpenAIClient(responses=[make_response("{}", parsed=_parsed(), usage=_usage())])
    client = OpenAICompatLLMClient(provider="gemini", model="m", client=fake, reasoning_effort="minimal")

    result = client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert (result.usage.total_tokens, result.usage.thinking_tokens, result.usage.cached_tokens) == (170, 50, 40)
    record = LLMMetricsCollector().calls[0]
    assert (record.total_tokens, record.thinking_tokens, record.cached_tokens) == (170, 50, 40)
    assert record.reasoning_effort == "minimal"
    reported = LLMMetricsCollector().get_summary()["usage_reported"]
    assert reported["thinking_tokens"] == {"calls_reporting": 1, "sum": 50}
    assert reported["total_tokens"] == {"calls_reporting": 1, "sum": 170}
    assert reported["reasoning_effort_calls"] == {"minimal": 1}


def test_unreported_usage_stays_none_not_zero():
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20)  # 프로바이더가 total·details를 안 보냄
    fake = FakeOpenAIClient(responses=[make_response("{}", parsed=_parsed(), usage=usage)])
    client = OpenAICompatLLMClient(provider="gemini", model="m", client=fake)

    result = client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert (result.usage.total_tokens, result.usage.thinking_tokens, result.usage.cached_tokens) == (None, None, None)
    record = LLMMetricsCollector().calls[0]
    assert (record.total_tokens, record.thinking_tokens, record.cached_tokens) == (None, None, None)
    assert record.reasoning_effort == ""
    reported = LLMMetricsCollector().get_summary()["usage_reported"]
    assert reported["thinking_tokens"] == {"calls_reporting": 0, "sum": None}
    assert reported["reasoning_effort_calls"] == {"unset": 1}


def test_a_reported_zero_is_distinguished_from_no_report():
    reported_zero = _usage(reasoning=0, cached=0)
    fake = FakeOpenAIClient(responses=[make_response("{}", parsed=_parsed(), usage=reported_zero)])

    OpenAICompatLLMClient(provider="gemini", model="m", client=fake).complete(
        MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert LLMMetricsCollector().get_summary()["usage_reported"]["thinking_tokens"] == {"calls_reporting": 1, "sum": 0}


def test_empty_collector_summary_has_the_usage_section():
    assert LLMMetricsCollector().get_summary()["usage_reported"]["total_tokens"] == {"calls_reporting": 0, "sum": None}


def _length_error():
    return openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))


@pytest.mark.parametrize("responses,attempts", [
    # pre-flight에서 본 실제 실패: gemini-3.5-flash-lite가 reasoning_effort=none을 HTTP 400으로 거부했다
    ([make_status_error(400, "reasoning_effort none is not supported")], 1),
    ([make_response("x", parsed=None)] * 3, 3),  # 스키마 검증 실패: 재시도 2회 -> 응답 3개
    ([_length_error()], 1),
    ([openai.ContentFilterFinishReasonError(completion=None)], 1),
    ([RuntimeError("boom")], 1),
    ([make_timeout_error(), make_timeout_error(), make_response("{}", parsed=_parsed())], 3),  # 실패 2 + 성공 1
], ids=["http_400", "schema_retries", "length", "content_filter", "unexpected_error", "timeouts_then_ok"])
def test_every_attempt_that_carried_the_effort_records_it_including_failures(monkeypatch, responses, attempts):
    """실패한 시도도 요청에는 effort를 실었다. 기록이 'unset'이면 원인 설정이 요약에서 사라진다."""
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)
    fake = FakeOpenAIClient(responses=list(responses))
    client = OpenAICompatLLMClient(provider="gemini", model="m", client=fake, reasoning_effort="none")

    client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    sent = fake.chat.completions.parse_calls + fake.chat.completions.create_calls
    assert len(sent) == attempts and all(call["reasoning_effort"] == "none" for call in sent)
    records = LLMMetricsCollector().calls
    assert len(records) == attempts
    assert [r.reasoning_effort for r in records] == ["none"] * attempts
    assert LLMMetricsCollector().get_summary()["usage_reported"]["reasoning_effort_calls"] == {"none": attempts}


def test_a_call_blocked_before_any_request_does_not_claim_an_effort(monkeypatch):
    """킬 스위치로 요청이 나가지 않은 호출은 effort를 실은 적이 없으므로 'unset'으로 센다."""
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")
    fake = FakeOpenAIClient(responses=[])
    client = OpenAICompatLLMClient(provider="gemini", model="m", client=fake, reasoning_effort="none")

    result = client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert result.error == "kill_switch" and fake.chat.completions.call_count == 0
    assert LLMMetricsCollector().get_summary()["usage_reported"]["reasoning_effort_calls"] == {"unset": 1}
