import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeOpenAIClient, make_response

from core.llm.adapters import OpenAICompatLLMClient
from core.llm.schemas import NewsletterEval


def _fallback_client(responses):
    return OpenAICompatLLMClient(
        provider="upstage",
        model="solar-mini",
        supports_json_schema=False,  # json_schema 미지원 프로바이더로 가정
        client=FakeOpenAIClient(responses=responses),
    )


def test_json_mode_fallback_calls_create_with_json_object_response_format_and_validates():
    raw_json = '{"decision": "PASS", "score": 8, "feedback": "", "issues": []}'
    fake = FakeOpenAIClient(responses=[make_response(raw_json)])
    client = OpenAICompatLLMClient(
        provider="upstage", model="solar-mini", supports_json_schema=False, client=fake,
    )

    result = client.complete(
        [{"role": "user", "content": "evaluate"}],
        schema=NewsletterEval,
        purpose="newsletter_eval",
    )

    assert result.error is None
    assert result.parsed == NewsletterEval(decision="PASS", score=8, feedback="", issues=[])
    assert result.text == raw_json

    # create()가 호출되고 response_format={"type": "json_object"}가 전달돼야 한다
    assert fake.chat.completions.create_calls[0]["response_format"] == {"type": "json_object"}
    assert fake.chat.completions.parse_calls == []


def test_json_mode_fallback_uses_extract_json_from_response_repair_for_malformed_json():
    # 마크다운 코드블록으로 감싸진 응답 - extract_json_from_response가 복구해야 함
    wrapped = '```json\n{"decision": "FAIL", "score": 2, "feedback": "부족", "issues": ["a"]}\n```'
    fake = FakeOpenAIClient(responses=[make_response(wrapped)])
    client = _fallback_client([make_response(wrapped)])

    result = client.complete([{"role": "user", "content": "x"}], schema=NewsletterEval, purpose="newsletter_eval")

    assert result.error is None
    assert result.parsed.decision == "FAIL"
    assert result.parsed.score == 2


def test_json_mode_fallback_without_schema_returns_plain_text():
    fake = FakeOpenAIClient(responses=[make_response("그냥 텍스트 응답")])
    client = OpenAICompatLLMClient(
        provider="upstage", model="solar-mini", supports_json_schema=False, client=fake,
    )

    result = client.complete([{"role": "user", "content": "x"}], schema=None, purpose="newsletter_content_gen")

    assert result.error is None
    assert result.text == "그냥 텍스트 응답"
    assert result.parsed is None
    assert "response_format" not in fake.chat.completions.create_calls[0]


def test_json_mode_fallback_gives_up_when_json_cannot_be_extracted(monkeypatch):
    import core.llm.adapters as adapters_module
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)

    from config.settings import Settings

    # 매번 파싱 불가능한 응답만 오는 경우, 상한(MAX_LLM_CALL_RETRIES)만큼만 재시도해야 한다
    responses = [make_response("이것은 JSON이 아닙니다") for _ in range(Settings.MAX_LLM_CALL_RETRIES)]
    fake = FakeOpenAIClient(responses=responses)
    client = OpenAICompatLLMClient(
        provider="upstage", model="solar-mini", supports_json_schema=False, client=fake,
    )

    result = client.complete([{"role": "user", "content": "x"}], schema=NewsletterEval, purpose="newsletter_eval")

    assert result.parsed is None
    assert result.error is not None
    assert fake.chat.completions.create_calls.__len__() == Settings.MAX_LLM_CALL_RETRIES
