import sys
import os
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


def test_tone_converter_passes_provider_string_not_settings_object():
    from core.tone_converter import ToneConverter

    fake_settings = SimpleNamespace(LLM_PROVIDER="openai")

    with patch("core.tone_converter.get_llm_client") as mock_get_client:
        ToneConverter(settings=fake_settings)

    mock_get_client.assert_called_once_with("openai")


def test_tone_converter_defaults_to_settings_llm_provider_when_no_settings_passed():
    from core.tone_converter import ToneConverter, Settings

    with patch("core.tone_converter.get_llm_client") as mock_get_client:
        ToneConverter()

    mock_get_client.assert_called_once_with(Settings.LLM_PROVIDER)
