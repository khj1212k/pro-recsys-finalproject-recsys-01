# LLM 프로바이더 어댑터
# - OpenAICompatLLMClient: OpenAI Chat Completions 호환 엔드포인트(OpenAI/Gemini/Upstage)용 단일 어댑터
# - HyperCLOVALLMClient: 레거시 NaverHyperCLOVAClient를 LLMClient 계약으로 감싼 어댑터
#   (기본 프로바이더는 아니지만, GEN_PROVIDER=naver 등으로 명시하면 계속 쓸 수 있다 - ADR 0005 참고)

import logging
import time
from typing import Any, Dict, List, Optional, Type

import openai
from openai import OpenAI
from pydantic import BaseModel, ValidationError

from core.llm.client import LLMClient, LLMResult, LLMUsage
from core.llm.kill_switch import check_kill_switch
from core.llm_client import (
    BACKOFF_MULTIPLIER,
    INITIAL_BACKOFF,
    MAX_BACKOFF,
    MAX_RETRIES,
    NaverHyperCLOVAClient,
    SimpleRateLimiter,
    extract_json_from_response,
)
from core.llm_metrics import get_metrics_collector

logger = logging.getLogger(__name__)


class OpenAICompatLLMClient(LLMClient):
    """OpenAI Chat Completions 호환 엔드포인트(OpenAI/Gemini/Upstage 등) 공용 어댑터.

    세 프로바이더 모두 openai 파이썬 SDK로 `base_url`만 바꿔 호출할 수 있다
    (docs/adr/0005-llm-provider-abstraction.md에 근거 URL과 확인 일자 기록).
    """

    def __init__(
        self,
        provider: str,
        model: str,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        supports_json_schema: bool = True,
        rate_limiter: Optional[SimpleRateLimiter] = None,
        client: Optional[Any] = None,
    ):
        self.provider = provider
        self.model = model
        self.supports_json_schema = supports_json_schema
        self._rate_limiter = rate_limiter or SimpleRateLimiter(0.0)

        if client is not None:
            self._client = client
        else:
            if not api_key:
                raise ValueError(f"{provider} 프로바이더에 API 키가 설정되지 않았습니다")
            self._client = OpenAI(api_key=api_key, base_url=base_url)

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        schema: Optional[Type[BaseModel]] = None,
        purpose: str = "unknown",
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResult:
        killed = check_kill_switch(self.provider, self.model, purpose)
        if killed is not None:
            return killed

        backoff = INITIAL_BACKOFF
        last_error = "unknown error"

        for attempt in range(1, MAX_RETRIES + 1):
            attempt_start = time.time()
            try:
                self._rate_limiter.wait()
                start = time.time()

                if schema is not None and self.supports_json_schema:
                    text, parsed, response_usage = self._call_structured(messages, schema, temperature, max_tokens)
                else:
                    text, parsed, response_usage = self._call_json_mode_or_plain(
                        messages, schema, temperature, max_tokens
                    )

                latency = time.time() - start
                usage = LLMUsage(
                    input_tokens=getattr(response_usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(response_usage, "completion_tokens", 0) or 0,
                )

                get_metrics_collector().record_call(
                    purpose=purpose,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    latency_seconds=latency,
                    success=True,
                    provider=self.provider,
                    model=self.model,
                )

                return LLMResult(
                    text=text,
                    parsed=parsed,
                    usage=usage,
                    latency_s=latency,
                    attempts=attempt,
                    provider=self.provider,
                    model=self.model,
                    error=None,
                )

            except (ValidationError, ValueError) as e:
                # 모델이 스키마를 못 지켰거나(JSON 모드 폴백) 검증에 실패한 경우.
                # 429/5xx는 아니지만 재시도할 가치가 있다(다음 시도에서 형식을 지킬 수 있음).
                last_error = str(e)
                latency = time.time() - attempt_start
                get_metrics_collector().record_call(
                    purpose=purpose, input_tokens=0, output_tokens=0,
                    latency_seconds=latency, success=False,
                    provider=self.provider, model=self.model,
                )
                logger.warning(
                    "%s/%s: 구조화 출력 검증 실패 (attempt %s/%s): %s",
                    self.provider, self.model, attempt, MAX_RETRIES, e,
                )
                time.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                continue

            except openai.APIStatusError as e:
                last_error = str(e)
                latency = time.time() - attempt_start
                get_metrics_collector().record_call(
                    purpose=purpose, input_tokens=0, output_tokens=0,
                    latency_seconds=latency, success=False,
                    provider=self.provider, model=self.model,
                )
                if e.status_code == 429 or e.status_code >= 500:
                    logger.warning(
                        "%s/%s: HTTP %s (attempt %s/%s), 재시도",
                        self.provider, self.model, e.status_code, attempt, MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                logger.error("%s/%s: HTTP %s (재시도 불가): %s", self.provider, self.model, e.status_code, e)
                return LLMResult(
                    text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                    attempts=attempt, provider=self.provider, model=self.model, error=last_error,
                )

            except openai.LengthFinishReasonError as e:
                # 길이 제한으로 구조화 출력을 못 채운 경우 - 프롬프트/max_tokens을 안 바꾸면
                # 재시도해도 같은 이유로 또 실패할 가능성이 높으므로 즉시 종료한다.
                latency = time.time() - attempt_start
                get_metrics_collector().record_call(
                    purpose=purpose, input_tokens=0, output_tokens=0,
                    latency_seconds=latency, success=False,
                    provider=self.provider, model=self.model, error_type="length",
                )
                logger.error(
                    "%s/%s: 길이 제한으로 구조화 출력 실패 (재시도 불가): %s",
                    self.provider, self.model, e,
                )
                return LLMResult(
                    text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                    attempts=attempt, provider=self.provider, model=self.model, error="length",
                )

            except openai.ContentFilterFinishReasonError as e:
                # 콘텐츠 필터가 응답을 막은 경우 - 같은 프롬프트로 재시도해도 다시
                # 걸릴 가능성이 높으므로 즉시 종료한다.
                latency = time.time() - attempt_start
                get_metrics_collector().record_call(
                    purpose=purpose, input_tokens=0, output_tokens=0,
                    latency_seconds=latency, success=False,
                    provider=self.provider, model=self.model, error_type="content_filter",
                )
                logger.error(
                    "%s/%s: 콘텐츠 필터에 의해 응답 거부됨 (재시도 불가): %s",
                    self.provider, self.model, e,
                )
                return LLMResult(
                    text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                    attempts=attempt, provider=self.provider, model=self.model, error="content_filter",
                )

            except (openai.APITimeoutError, openai.APIConnectionError) as e:
                last_error = str(e)
                latency = time.time() - attempt_start
                get_metrics_collector().record_call(
                    purpose=purpose, input_tokens=0, output_tokens=0,
                    latency_seconds=latency, success=False,
                    provider=self.provider, model=self.model,
                )
                logger.warning(
                    "%s/%s: 연결/타임아웃 오류 (attempt %s/%s), 재시도: %s",
                    self.provider, self.model, attempt, MAX_RETRIES, e,
                )
                time.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                continue

            except Exception as e:  # noqa: BLE001 - 알 수 없는 오류는 재시도하지 않고 즉시 반환
                last_error = str(e)
                latency = time.time() - attempt_start
                get_metrics_collector().record_call(
                    purpose=purpose, input_tokens=0, output_tokens=0,
                    latency_seconds=latency, success=False,
                    provider=self.provider, model=self.model,
                )
                logger.error("%s/%s: 예상치 못한 오류: %s", self.provider, self.model, e)
                return LLMResult(
                    text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                    attempts=attempt, provider=self.provider, model=self.model, error=last_error,
                )

        return LLMResult(
            text=None, parsed=None, usage=LLMUsage(), latency_s=0.0,
            attempts=MAX_RETRIES, provider=self.provider, model=self.model,
            error=f"max retries ({MAX_RETRIES}) exhausted: {last_error}",
        )

    def _call_structured(self, messages, schema, temperature, max_tokens):
        response = self._client.chat.completions.parse(
            model=self.model,
            messages=messages,
            response_format=schema,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        message = response.choices[0].message
        parsed = message.parsed
        if parsed is None:
            raise ValueError("structured output이 스키마 검증을 통과하지 못했습니다 (parsed=None)")
        return message.content, parsed, getattr(response, "usage", None)

    def _call_json_mode_or_plain(self, messages, schema, temperature, max_tokens):
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object"}

        response = self._client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content

        parsed = None
        if schema is not None:
            raw = extract_json_from_response(text)
            if raw is None:
                raise ValueError("JSON 모드 응답에서 JSON을 추출하지 못했습니다")
            parsed = schema.model_validate(raw)

        return text, parsed, getattr(response, "usage", None)


class HyperCLOVALLMClient(LLMClient):
    """레거시 HyperCLOVA X 어댑터.

    HyperCLOVA API는 OpenAI 호환이 아니고 response_format(json_schema)을 지원하지
    않으므로, 프롬프트에 의존하는 JSON 모드 + extract_json_from_response 복구 +
    pydantic 검증으로만 동작한다. 재시도/백오프/메트릭 기록은 기존
    NaverHyperCLOVAClient.chat_completion 내부에서 이미 처리하므로 여기서는
    한 번만 호출하고 결과를 LLMResult로 감싼다.
    """

    def __init__(self, model: Optional[str] = None, *, legacy_client: Optional[Any] = None):
        self.provider = "naver"
        self._legacy_client = legacy_client or NaverHyperCLOVAClient(model=model)
        self.model = getattr(self._legacy_client, "model", model or "HCX-003")

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        schema: Optional[Type[BaseModel]] = None,
        purpose: str = "unknown",
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResult:
        killed = check_kill_switch(self.provider, self.model, purpose)
        if killed is not None:
            return killed

        start = time.time()
        response_format = {"type": "json_object"} if schema is not None else None
        text = self._legacy_client.chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            purpose=purpose,
        )
        latency = time.time() - start

        if text is None:
            return LLMResult(
                text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                attempts=1, provider=self.provider, model=self.model,
                error="HyperCLOVA 응답 없음 (내부 재시도 소진)",
            )

        parsed = None
        error = None
        if schema is not None:
            raw = extract_json_from_response(text)
            if raw is None:
                error = "JSON 추출 실패"
            else:
                try:
                    parsed = schema.model_validate(raw)
                except ValidationError as e:
                    error = f"스키마 검증 실패: {e}"

        return LLMResult(
            text=text, parsed=parsed, usage=LLMUsage(), latency_s=latency,
            attempts=1, provider=self.provider, model=self.model, error=error,
        )
