import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import (
    FakeOpenAIClient,
    make_response,
    make_status_error,
    make_timeout_error,
    make_connection_error,
)

from core.llm.adapters import OpenAICompatLLMClient
from core.llm.schemas import ToneResult
from config.settings import Settings


def _no_sleep(monkeypatch):
    import core.llm.adapters as adapters_module
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)


def test_retries_on_429_then_succeeds(monkeypatch):
    _no_sleep(monkeypatch)
    parsed = ToneResult(title="t", summary="s", content="c", keywords=[])
    fake = FakeOpenAIClient(responses=[
        make_status_error(429),
        make_status_error(429),
        make_response("{}", parsed=parsed),
    ])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ToneResult, purpose="tone_convert")

    assert result.error is None
    assert result.attempts == 3
    assert fake.chat.completions.parse_calls.__len__() == 3


def test_retries_on_5xx_and_timeout_and_connection_error_then_succeeds(monkeypatch):
    _no_sleep(monkeypatch)
    parsed = ToneResult(title="t", summary="s", content="c", keywords=[])
    fake = FakeOpenAIClient(responses=[
        make_status_error(503),
        make_timeout_error(),
        make_connection_error(),
        make_response("{}", parsed=parsed),
    ])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ToneResult, purpose="tone_convert")

    assert result.error is None
    assert result.attempts == 4


def test_gives_up_after_max_retries_on_persistent_429(monkeypatch):
    _no_sleep(monkeypatch)
    responses = [make_status_error(429) for _ in range(Settings.MAX_LLM_CALL_RETRIES)]
    fake = FakeOpenAIClient(responses=responses)
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ToneResult, purpose="tone_convert")

    assert result.error is not None
    assert result.parsed is None
    assert result.text is None
    assert fake.chat.completions.parse_calls.__len__() == Settings.MAX_LLM_CALL_RETRIES


def test_non_retryable_4xx_returns_immediately_without_exhausting_retries(monkeypatch):
    _no_sleep(monkeypatch)
    fake = FakeOpenAIClient(responses=[make_status_error(401, "invalid api key")])
    client = OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ToneResult, purpose="tone_convert")

    assert result.error is not None
    assert fake.chat.completions.parse_calls.__len__() == 1
