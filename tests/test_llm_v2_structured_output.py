import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeOpenAIClient, make_response

from core.llm.adapters import OpenAICompatLLMClient
from core.llm.schemas import ClusterEval


def _client(responses):
    return OpenAICompatLLMClient(
        provider="openai",
        model="gpt-4o-mini",
        supports_json_schema=True,
        client=FakeOpenAIClient(responses=responses),
    )


def test_structured_output_uses_chat_completions_parse_and_returns_parsed_model():
    parsed = ClusterEval(decision="PASS", confidence=0.9, summary="s", feedback="", outlier_indices=[], sub_groups=[])
    fake = FakeOpenAIClient(responses=[make_response('{"decision": "PASS"}', parsed=parsed)])
    client = OpenAICompatLLMClient(
        provider="openai", model="gpt-4o-mini", supports_json_schema=True, client=fake,
    )

    result = client.complete(
        [{"role": "user", "content": "evaluate"}],
        schema=ClusterEval,
        purpose="cluster_eval",
    )

    assert result.error is None
    assert result.parsed == parsed
    assert result.parsed.decision == "PASS"
    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini"
    assert result.attempts == 1
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5

    # .parse()가 호출되고, response_format으로 스키마 클래스 자체가 전달돼야 한다
    assert fake.chat.completions.parse_calls[0]["response_format"] is ClusterEval
    assert fake.chat.completions.create_calls == []


def test_structured_output_none_parsed_is_treated_as_validation_failure_and_retried(monkeypatch):
    import core.llm.adapters as adapters_module
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)

    parsed = ClusterEval(decision="FAIL", confidence=0.1, summary="", feedback="", outlier_indices=[], sub_groups=[])
    fake = FakeOpenAIClient(responses=[
        make_response('{}', parsed=None),  # 1차: 스키마 검증 실패로 취급
        make_response('{"decision": "FAIL"}', parsed=parsed),  # 2차: 성공
    ])
    client = OpenAICompatLLMClient(
        provider="openai", model="gpt-4o-mini", supports_json_schema=True, client=fake,
    )

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert result.error is None
    assert result.attempts == 2
    assert len(fake.chat.completions.parse_calls) == 2
