import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeOpenAIClient, make_response, make_status_error, make_usage

from core.llm.adapters import OpenAICompatLLMClient
from core.llm.schemas import ClusterEval
from core.llm_metrics import LLMMetricsCollector


def _fresh_collector():
    collector = LLMMetricsCollector()
    collector.reset()
    return collector


def test_successful_call_is_recorded_with_purpose_provider_model_and_tokens(monkeypatch):
    collector = _fresh_collector()
    parsed = ClusterEval(decision="PASS", confidence=0.5)
    fake = FakeOpenAIClient(responses=[
        make_response("{}", parsed=parsed, usage=make_usage(prompt_tokens=123, completion_tokens=45))
    ])
    client = OpenAICompatLLMClient(provider="gemini", model="gemini-2.5-flash", client=fake)

    client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert len(collector.calls) == 1
    record = collector.calls[0]
    assert record.success is True
    assert record.purpose == "cluster_eval"
    assert record.provider == "gemini"
    assert record.model == "gemini-2.5-flash"
    assert record.input_tokens == 123
    assert record.output_tokens == 45


def test_failed_attempts_are_recorded_with_success_false_before_giving_up(monkeypatch):
    import core.llm.adapters as adapters_module
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)

    collector = _fresh_collector()
    fake = FakeOpenAIClient(responses=[
        make_status_error(500),
        make_status_error(500),
        make_response("{}", parsed=ClusterEval(decision="FAIL", confidence=0.0)),
    ])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert len(collector.calls) == 3
    assert [c.success for c in collector.calls] == [False, False, True]
    assert all(c.provider == "openai" and c.model == "gpt-4o-mini" for c in collector.calls)

    summary = collector.get_summary()
    assert summary["total_attempts"] == 3
    assert summary["failed_attempts"] == 2
    assert summary["successful_calls"] == 1
    assert "openai/gpt-4o-mini" in summary["by_model"]
    assert summary["by_model"]["openai/gpt-4o-mini"]["calls"] == 3
    assert summary["by_model"]["openai/gpt-4o-mini"]["failed_calls"] == 2
