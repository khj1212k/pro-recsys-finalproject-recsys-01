import os
import sys
from unittest.mock import MagicMock, patch

import httpx
import openai
import pytest
from pydantic import BaseModel

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from config.settings import Settings  # noqa: E402
from core.llm import adapters  # noqa: E402
from core.llm.adapters import OpenAICompatLLMClient  # noqa: E402


class Probe(BaseModel):
    title: str


def _overloaded():
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    return openai.InternalServerError("overloaded", response=httpx.Response(503, request=request), body=None)


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


@pytest.fixture(autouse=True)
def switch_off(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(tmp_path / "absent"))


def test_sdk_client_has_request_timeout_and_no_hidden_sdk_retries():
    with patch.object(adapters, "OpenAI") as sdk:
        OpenAICompatLLMClient("gemini", "m", base_url="https://example.invalid", api_key="k")
    kwargs = sdk.call_args.kwargs
    assert kwargs["timeout"] == Settings.LLM_REQUEST_TIMEOUT_S
    assert kwargs["max_retries"] == 0


def test_retries_stop_at_the_per_call_deadline(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(adapters, "time", clock)
    monkeypatch.setattr(Settings, "LLM_CALL_DEADLINE_S", 10.0)

    client = OpenAICompatLLMClient("gemini", "m", client=MagicMock())
    client._call_structured = MagicMock(side_effect=_overloaded())

    result = client.complete([{"role": "user", "content": "x"}], schema=Probe, purpose="t")

    assert result.error is not None and "deadline" in result.error
    assert clock.slept <= Settings.LLM_CALL_DEADLINE_S
    assert result.attempts == client._call_structured.call_count


def test_judge_default_is_a_model_that_answered_the_2026_09_25_probe(monkeypatch):
    from core.llm import registry

    for var in ("JUDGE_PROVIDER", "JUDGE_MODEL"):
        monkeypatch.delenv(var, raising=False)
    assert registry.ROLE_DEFAULT_MODEL["judge"] == "gemini-3.1-flash-lite"
    assert registry.ROLE_DEFAULT_MODEL["judge"] != registry.ROLE_DEFAULT_MODEL["generator"]
