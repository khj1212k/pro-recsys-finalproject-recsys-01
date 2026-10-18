# 스키마 검증 실패 재시도 상한 (docs/adr/0010 Addendum 2026-10-18, 실행 계획 WP3 C10 축소판).
# 모델이 스키마를 못 지킨 응답은 응답마다 과금되고 같은 프롬프트에서 같은 위반이 반복되기 쉽다.
# 전송 계층 재시도(429/5xx/타임아웃, MAX_LLM_CALL_RETRIES=10)와 따로 MAX_LLM_SCHEMA_RETRIES(기본 2)까지만 다시 보낸다.
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeOpenAIClient, make_response, make_status_error  # noqa: E402

import core.llm.adapters as adapters_module  # noqa: E402
from config.settings import Settings  # noqa: E402
from core.llm.adapters import OpenAICompatLLMClient  # noqa: E402
from core.llm.schemas import ClusterEval  # noqa: E402

MESSAGES = [{"role": "user", "content": "x"}]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)


def _rejected():
    return make_response("not json", parsed=None)  # parse()가 스키마 검증에 실패해 parsed=None


def _ok():
    return make_response("{}", parsed=ClusterEval(decision="PASS", confidence=0.9))


def _client(responses, **kwargs):
    fake = FakeOpenAIClient(responses=responses)
    return fake, OpenAICompatLLMClient(provider="openai", model="gpt-4o-mini", client=fake, **kwargs)


def _complete(client):
    return client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")


def test_default_cap_is_two_retries():
    assert Settings.MAX_LLM_SCHEMA_RETRIES == 2


def test_persistent_schema_failures_stop_after_the_cap_not_after_ten_requests():
    fake, client = _client([_rejected() for _ in range(Settings.MAX_LLM_CALL_RETRIES)])

    result = _complete(client)

    assert fake.chat.completions.call_count == 3  # 최초 1 + 재시도 2
    assert result.parsed is None and result.text is None
    assert result.schema_failures == 3 and result.attempts == 3
    assert result.error.startswith("max retries (schema 2) exhausted")
    assert result.no_retry_reason is None  # 형식 위반은 호출이 막힌 것이 아니다 - 그래프의 재생성 정책이 그대로 적용된다


def test_recovering_within_the_cap_still_succeeds():
    fake, client = _client([_rejected(), _rejected(), _ok()])

    result = _complete(client)

    assert fake.chat.completions.call_count == 3
    assert result.error is None and result.parsed is not None
    assert result.schema_failures == 2


def test_a_third_retry_is_never_sent():
    fake, client = _client([_rejected(), _rejected(), _rejected(), _ok()])

    result = _complete(client)

    assert fake.chat.completions.call_count == 3
    assert result.parsed is None


def test_cap_can_be_lowered_to_no_retry(monkeypatch):
    monkeypatch.setattr(Settings, "MAX_LLM_SCHEMA_RETRIES", 0)
    fake, client = _client([_rejected(), _ok()])

    result = _complete(client)

    assert fake.chat.completions.call_count == 1
    assert result.error.startswith("max retries (schema 0) exhausted")


def test_json_mode_path_is_capped_the_same_way():
    fake = FakeOpenAIClient(responses=[make_response("이것은 JSON이 아닙니다") for _ in range(10)])
    client = OpenAICompatLLMClient(provider="upstage", model="solar-mini", supports_json_schema=False, client=fake)

    result = _complete(client)

    assert fake.chat.completions.call_count == 3
    assert result.schema_failures == 3


def test_transport_retries_keep_their_own_larger_cap():
    """429가 여러 번 나도 스키마 실패 수로 세지 않는다 - 전송 계층 상한(10)까지 다시 보낸다."""
    fake, client = _client([make_status_error(429) for _ in range(5)] + [_ok()])

    result = _complete(client)

    assert fake.chat.completions.call_count == 6
    assert result.error is None and result.schema_failures == 0


def test_transport_errors_between_schema_failures_do_not_reset_the_count():
    fake, client = _client([_rejected(), make_status_error(503), _rejected(), make_status_error(503), _rejected(), _ok()])

    result = _complete(client)

    assert fake.chat.completions.call_count == 5  # 세 번째 스키마 실패에서 멈춘다
    assert result.schema_failures == 3
    assert result.error.startswith("max retries (schema 2) exhausted")
