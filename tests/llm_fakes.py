# core/llm/ 테스트용 페이크 OpenAI 클라이언트 헬퍼.
# pytest는 test_*.py만 수집하므로 이 파일은 테스트로 수집되지 않는다.
import httpx2
import openai
from types import SimpleNamespace

import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from core.llm.client import LLMClient, LLMResult, LLMUsage


class FakeLLMClient(LLMClient):
    """core.llm.client.LLMClient 계약을 직접 구현하는 페이크.

    호출부(ClusterEvaluator/NewsletterEvaluator/ToneConverter/NewsReconstructor)
    통합 테스트에서, 어댑터 내부 재시도 로직까지 재현할 필요 없이 결과를 직접
    주입하고 호출 인자를 검증하기 위해 쓴다.
    """

    def __init__(self, results=None, provider="fake", model="fake-model"):
        self.provider = provider
        self.model = model
        self._results = list(results or [])
        self.calls = []  # 각 호출의 (messages, schema, purpose, temperature, max_tokens) 기록

    def complete(self, messages, *, schema=None, purpose="unknown", temperature=0.2, max_tokens=4096):
        self.calls.append(
            {
                "messages": messages,
                "schema": schema,
                "purpose": purpose,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if not self._results:
            raise AssertionError("FakeLLMClient: 준비된 결과가 없는데 추가 호출이 발생했습니다")
        item = self._results.pop(0)
        if isinstance(item, LLMResult):
            return item
        # dict 단축 표기: {"parsed": ...} / {"text": ...} / {"error": ...}
        return LLMResult(
            text=item.get("text"),
            parsed=item.get("parsed"),
            usage=item.get("usage", LLMUsage()),
            latency_s=item.get("latency_s", 0.01),
            attempts=item.get("attempts", 1),
            provider=self.provider,
            model=self.model,
            error=item.get("error"),
        )

    @property
    def call_count(self):
        return len(self.calls)


def make_usage(prompt_tokens=10, completion_tokens=5):
    return SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


def make_response(content, parsed=None, usage=None):
    message = SimpleNamespace(content=content, parsed=parsed)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage or make_usage())


def make_status_error(status_code: int, message: str = "boom") -> openai.APIStatusError:
    resp = httpx2.Response(status_code, request=httpx2.Request("POST", "http://fake"))
    cls = {429: openai.RateLimitError}.get(status_code, openai.APIStatusError)
    if status_code >= 500:
        cls = openai.InternalServerError
    elif status_code == 429:
        cls = openai.RateLimitError
    else:
        cls = openai.APIStatusError
    return cls(message, response=resp, body=None)


def make_timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=httpx2.Request("POST", "http://fake"))


def make_connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx2.Request("POST", "http://fake"))


class FakeChatCompletions:
    """response_fn(mode, **kwargs) -> response 객체 혹은 예외 인스턴스(raise됨).

    mode는 "create" 또는 "parse". response_fn이 예외 인스턴스를 반환하면 그걸 raise한다
    (콜백 본체에서 raise하는 것보다 큐 조립이 간단해서).
    """

    def __init__(self, responses=None, on_call=None):
        # responses: 호출될 때마다 하나씩 pop할 리스트. 각 원소는 response 객체 또는 Exception 인스턴스.
        self._responses = list(responses or [])
        self._on_call = on_call
        self.create_calls = []
        self.parse_calls = []

    def _next(self, mode, kwargs):
        if self._on_call:
            self._on_call(mode, kwargs)
        if not self._responses:
            raise AssertionError("FakeChatCompletions: 응답 큐가 비었는데 추가 호출이 발생했습니다")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._next("create", kwargs)

    def parse(self, **kwargs):
        self.parse_calls.append(kwargs)
        return self._next("parse", kwargs)

    @property
    def call_count(self):
        return len(self.create_calls) + len(self.parse_calls)


class FakeChat:
    def __init__(self, completions: FakeChatCompletions):
        self.completions = completions


class FakeOpenAIClient:
    """core.llm.adapters.OpenAICompatLLMClient(client=...)에 주입하는 페이크."""

    def __init__(self, responses=None, on_call=None):
        self.chat = FakeChat(FakeChatCompletions(responses=responses, on_call=on_call))
