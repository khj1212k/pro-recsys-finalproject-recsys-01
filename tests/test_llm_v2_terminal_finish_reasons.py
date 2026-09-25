# chat.completions.parse()가 던지는 openai.LengthFinishReasonError /
# openai.ContentFilterFinishReasonError는 재시도해도 같은 이유로 다시 실패할
# 확률이 높은 종류의 오류이므로(길이 제한/콘텐츠 필터는 프롬프트를 안 바꾸면
# 반복돼도 안 풀림), 일반 Exception과 구분해 즉시 종료하고 "length"/
# "content_filter"라는 안정적인 에러 코드를 남겨야 한다.
import sys
import os
from types import SimpleNamespace

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import openai

from tests.llm_fakes import FakeOpenAIClient

from core.llm.adapters import OpenAICompatLLMClient
from core.llm.schemas import ClusterEval
from core.llm_metrics import LLMMetricsCollector


def _fresh_collector():
    collector = LLMMetricsCollector()
    collector.reset()
    return collector


def _no_sleep(monkeypatch):
    import core.llm.adapters as adapters_module
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)


def _length_error() -> openai.LengthFinishReasonError:
    return openai.LengthFinishReasonError(completion=SimpleNamespace(usage=None))


def _content_filter_error() -> openai.ContentFilterFinishReasonError:
    return openai.ContentFilterFinishReasonError(completion=None)


def test_length_finish_reason_is_terminal_with_length_error_code(monkeypatch):
    _no_sleep(monkeypatch)
    fake = FakeOpenAIClient(responses=[_length_error()])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert result.error == "length"
    assert result.parsed is None
    assert result.text is None
    assert result.attempts == 1
    # 재시도 가치가 없는 오류이므로 즉시 반환해야 한다 (retry 없음)
    assert len(fake.chat.completions.parse_calls) == 1


def test_content_filter_finish_reason_is_terminal_with_content_filter_error_code(monkeypatch):
    _no_sleep(monkeypatch)
    fake = FakeOpenAIClient(responses=[_content_filter_error()])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert result.error == "content_filter"
    assert result.parsed is None
    assert result.text is None
    assert len(fake.chat.completions.parse_calls) == 1


def test_length_finish_reason_records_distinct_error_type_in_metrics(monkeypatch):
    _no_sleep(monkeypatch)
    collector = _fresh_collector()
    fake = FakeOpenAIClient(responses=[_length_error()])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert len(collector.calls) == 1
    assert collector.calls[0].success is False
    assert collector.calls[0].error_type == "length"


def test_content_filter_finish_reason_records_distinct_error_type_in_metrics(monkeypatch):
    _no_sleep(monkeypatch)
    collector = _fresh_collector()
    fake = FakeOpenAIClient(responses=[_content_filter_error()])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert collector.calls[0].error_type == "content_filter"


def test_generic_openai_error_still_falls_back_to_unknown_error_type(monkeypatch):
    """일반 openai.OpenAIError(위 두 종류가 아닌)는 여전히 기존처럼 처리되고,
    error_type은 특정 코드 없이 기본값("")으로 남는다 - 회귀 확인용."""
    _no_sleep(monkeypatch)
    collector = _fresh_collector()
    fake = FakeOpenAIClient(responses=[openai.OpenAIError("boom")])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval, purpose="cluster_eval")

    assert result.error == "boom"
    assert collector.calls[0].error_type == ""
