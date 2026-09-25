import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from core.llm_client import NaverHyperCLOVAClient, OpenAIClient  # noqa: E402
from core.llm.kill_switch import LLMKillSwitchEngaged  # noqa: E402

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
def switch_on(monkeypatch):
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")


def test_legacy_hyperclova_refuses_network_call_when_switch_on(switch_on, monkeypatch):
    monkeypatch.setenv("NCP_CLOVASTUDIO_API_KEY", "nv-dummy")
    client = NaverHyperCLOVAClient()
    with patch("core.llm_client.requests.post") as post:
        with pytest.raises(LLMKillSwitchEngaged):
            client.chat_completion(MESSAGES)
    post.assert_not_called()


def test_legacy_openai_refuses_network_call_when_switch_on(switch_on, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")
    client = OpenAIClient()
    client.client = MagicMock()
    with pytest.raises(LLMKillSwitchEngaged):
        client.chat_completion(MESSAGES)
    client.client.chat.completions.create.assert_not_called()


def test_legacy_openai_calls_through_when_switch_off(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr("config.settings.Settings.LLM_KILL_SWITCH_FILE", str(tmp_path / "absent"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")
    client = OpenAIClient()
    client.client = MagicMock()
    client.client.chat.completions.create.return_value.choices = [MagicMock(message=MagicMock(content="ok"))]
    client.client.chat.completions.create.return_value.usage = MagicMock(prompt_tokens=1, completion_tokens=1)
    assert client.chat_completion(MESSAGES) == "ok"
    client.client.chat.completions.create.assert_called_once()
