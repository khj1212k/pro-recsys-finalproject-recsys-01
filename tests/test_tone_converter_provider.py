# NOTE: core/llm/ 도입(ADR 0005) 이전에는 ToneConverter가 provider 문자열
# (core.tone_converter.get_llm_client(provider))로 클라이언트를 직접 만들었다.
# 이제는 role="tone" 레지스트리(core.llm.get_client)를 통해 LLMClient를 얻거나,
# 테스트에서는 llm_client를 직접 주입한다.
import sys
import os
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeLLMClient


def test_tone_converter_uses_injected_llm_client_without_touching_registry():
    from core.tone_converter import ToneConverter

    fake = FakeLLMClient(results=[])
    with patch("core.tone_converter.get_client") as mock_get_client:
        ToneConverter(llm_client=fake)

    mock_get_client.assert_not_called()


def test_tone_converter_defaults_to_role_tone_registry_client_when_none_passed():
    from core.tone_converter import ToneConverter

    fake = FakeLLMClient(results=[])
    with patch("core.tone_converter.get_client", return_value=fake) as mock_get_client:
        converter = ToneConverter()

    mock_get_client.assert_called_once_with("tone")
    assert converter.llm_client is fake
