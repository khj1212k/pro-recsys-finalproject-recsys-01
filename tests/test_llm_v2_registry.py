import sys
import os
import logging

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import core.llm.registry as registry
from core.llm.adapters import OpenAICompatLLMClient


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """레지스트리는 모듈 전역 싱글턴 캐시를 쓰므로 매 테스트마다 리셋하고,
    환경변수도 이전 테스트에서 새는 게 없도록 필요한 provider의 키를 항상 채워둔다."""
    registry.reset_registry()
    for env_var in [
        "GEN_PROVIDER", "GEN_MODEL", "JUDGE_PROVIDER", "JUDGE_MODEL", "TONE_PROVIDER", "TONE_MODEL",
    ]:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("UPSTAGE_API_KEY", "test-upstage-key")
    yield
    registry.reset_registry()


def test_role_defaults_resolve_generator_judge_tone_to_different_providers():
    gen_provider, gen_model = registry.resolve_role_config("generator")
    judge_provider, judge_model = registry.resolve_role_config("judge")
    tone_provider, tone_model = registry.resolve_role_config("tone")

    assert gen_provider == "gemini"
    assert judge_provider == "openai"
    assert tone_provider == "upstage"
    # 요건: judge는 generator와 다른 모델 계열이어야 한다
    assert judge_provider != gen_provider
    assert all([gen_model, judge_model, tone_model])


def test_role_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("GEN_PROVIDER", "upstage")
    monkeypatch.setenv("GEN_MODEL", "solar-pro3-260323")
    monkeypatch.setenv("JUDGE_PROVIDER", "gemini")
    monkeypatch.setenv("JUDGE_MODEL", "gemini-2.5-pro")

    assert registry.resolve_role_config("generator") == ("upstage", "solar-pro3-260323")
    assert registry.resolve_role_config("judge") == ("gemini", "gemini-2.5-pro")


def test_unknown_role_raises():
    with pytest.raises(ValueError):
        registry.resolve_role_config("summarizer")


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv("GEN_PROVIDER", "totally-unknown-llm-vendor")
    with pytest.raises(ValueError):
        registry.resolve_role_config("generator")


def test_get_client_returns_same_instance_for_same_provider_and_model():
    client_a = registry.get_client("generator")
    client_b = registry.get_client("generator")
    assert client_a is client_b


def test_get_client_shares_rate_limiter_across_roles_with_same_provider(monkeypatch):
    monkeypatch.setenv("GEN_PROVIDER", "openai")
    monkeypatch.setenv("GEN_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("JUDGE_PROVIDER", "openai")
    monkeypatch.setenv("JUDGE_MODEL", "gpt-4.1-mini")  # 다른 모델이지만 같은 프로바이더

    gen_client = registry.get_client("generator")
    judge_client = registry.get_client("judge")

    assert gen_client is not judge_client  # 모델이 다르므로 인스턴스는 다름
    # 프로바이더가 같으므로 레이트리미터 인스턴스는 공유돼야 함
    assert gen_client._rate_limiter is judge_client._rate_limiter


def test_get_client_warns_when_judge_and_generator_share_provider(monkeypatch, caplog):
    monkeypatch.setenv("GEN_PROVIDER", "openai")
    monkeypatch.setenv("GEN_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("JUDGE_PROVIDER", "openai")
    monkeypatch.setenv("JUDGE_MODEL", "gpt-4o-mini")

    with caplog.at_level(logging.WARNING):
        registry.get_client("judge")

    assert any("같은 모델 계열" in r.message for r in caplog.records)


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("JUDGE_PROVIDER", "openai")

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        registry.get_client("judge")


def test_naver_provider_builds_legacy_hyperclova_adapter(monkeypatch):
    from core.llm.adapters import HyperCLOVALLMClient

    monkeypatch.setenv("GEN_PROVIDER", "naver")
    monkeypatch.setenv("GEN_MODEL", "HCX-003")
    monkeypatch.setenv("NCP_CLOVASTUDIO_API_KEY", "nv-test-key")

    client = registry.get_client("generator")
    assert isinstance(client, HyperCLOVALLMClient)
    assert client.provider == "naver"
