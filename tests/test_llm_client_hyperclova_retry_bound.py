import sys
import os
from unittest.mock import MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import core.llm_client as llm_client_module
from core.llm_client import NaverHyperCLOVAClient
from config.settings import Settings


def test_hyperclova_chat_completion_gives_up_after_max_retries(monkeypatch):
    """감사(audit) 발견 버그: chat_completion의 재시도 루프가 `while True`로
    되어 있어 서버가 계속 429를 반환하면 무제한으로 재시도했다. 이제는
    Settings.MAX_LLM_CALL_RETRIES를 상한으로 두고 그 이후에는 포기하고
    None을 반환해야 한다."""
    monkeypatch.setattr(llm_client_module.time, "sleep", lambda *_args, **_kwargs: None)

    client = NaverHyperCLOVAClient(api_key="nv-test-key", model="HCX-003")

    always_rate_limited = MagicMock()
    always_rate_limited.status_code = 429
    always_rate_limited.headers = {}

    post_calls = []

    def fake_post(*args, **kwargs):
        post_calls.append(1)
        return always_rate_limited

    monkeypatch.setattr(llm_client_module.requests, "post", fake_post)

    result = client.chat_completion([{"role": "user", "content": "hi"}])

    assert result is None
    assert len(post_calls) == Settings.MAX_LLM_CALL_RETRIES
