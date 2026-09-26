# bake-off(ADR 0009)가 호출 단위로 재야 하는 값들: 첫 응답의 스키마 통과 여부,
# 재시도 불가 HTTP 상태(402 선불 미결제 등 인프라 실패를 모델 품질 실패와 구분),
# 역할과 무관하게 (provider, model)을 직접 지정하는 레지스트리 오버라이드.
import sys
import os

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeOpenAIClient, make_response, make_status_error

import core.llm.adapters as adapters_module
import core.llm.registry as registry
from core.llm.adapters import OpenAICompatLLMClient
from core.llm.schemas import ClusterEval


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)


def _parsed():
    return ClusterEval(decision="PASS", confidence=0.9)


def test_first_try_schema_pass_reports_zero_schema_failures():
    fake = FakeOpenAIClient(responses=[make_response("{}", parsed=_parsed())])
    client = OpenAICompatLLMClient("openai", "m", supports_json_schema=True, client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval)

    assert result.parsed is not None
    assert result.schema_failures == 0


def test_schema_failures_counts_only_validation_failures_not_transport_retries():
    fake = FakeOpenAIClient(responses=[
        make_status_error(429),
        make_response("{}", parsed=None),
        make_response("{}", parsed=_parsed()),
    ])
    client = OpenAICompatLLMClient("openai", "m", supports_json_schema=True, client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval)

    assert result.parsed is not None
    assert result.attempts == 3
    assert result.schema_failures == 1


def test_non_retryable_http_error_exposes_status_code():
    fake = FakeOpenAIClient(responses=[make_status_error(402, "prepay required")])
    client = OpenAICompatLLMClient("gemini", "m", supports_json_schema=True, client=fake)

    result = client.complete([{"role": "user", "content": "x"}], schema=ClusterEval)

    assert result.parsed is None
    assert result.http_status == 402


@pytest.fixture
def _isolated_registry(monkeypatch):
    registry.reset_registry()
    for env_var in ["GEN_PROVIDER", "GEN_MODEL", "JUDGE_PROVIDER", "JUDGE_MODEL", "TONE_PROVIDER", "TONE_MODEL"]:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("UPSTAGE_API_KEY", "test-upstage-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    yield
    registry.reset_registry()


def test_client_for_builds_explicit_provider_model_ignoring_role_env(_isolated_registry, monkeypatch):
    monkeypatch.setenv("GEN_PROVIDER", "gemini")

    client = registry.client_for("upstage", "solar-pro3")

    assert (client.provider, client.model) == ("upstage", "solar-pro3")
    assert registry.client_for("upstage", "solar-pro3") is client


def test_client_for_shares_instances_and_rate_limiter_with_role_clients(_isolated_registry, monkeypatch):
    monkeypatch.setenv("GEN_PROVIDER", "openai")
    monkeypatch.setenv("GEN_MODEL", "gpt-4.1-mini")

    role_client = registry.get_client("generator")
    explicit = registry.client_for("openai", "gpt-4.1-mini")
    other_model = registry.client_for("openai", "gpt-4o-mini")

    assert explicit is role_client
    assert other_model._rate_limiter is role_client._rate_limiter


def test_anthropic_provider_uses_openai_compat_layer_without_native_json_schema(_isolated_registry):
    # 호환 계층은 response_format을 무시하므로(ADR 0009 검증 표) json_schema 경로를 쓰면 안 된다.
    client = registry.client_for("anthropic", "claude-haiku-4-5")

    assert isinstance(client, OpenAICompatLLMClient)
    assert client.supports_json_schema is False
    assert str(client._client.base_url).rstrip("/") == "https://api.anthropic.com/v1"


def test_client_for_rejects_unknown_provider(_isolated_registry):
    with pytest.raises(ValueError):
        registry.client_for("nope", "x")
