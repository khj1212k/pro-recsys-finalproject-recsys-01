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

from core.llm.budget import get_budget_guard
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
from config.settings import Settings
from core.llm_metrics import get_metrics_collector

logger = logging.getLogger(__name__)


class _ResponseRejected(ValueError):
    """모델 응답은 받았지만 스키마 검증을 통과하지 못했다.

    응답을 받았으니 토큰은 과금됐다 - usage를 실어 보내 지출 원장이 실제 사용량으로 정산하게 한다.
    ValueError의 하위 클래스라 재시도 분기는 그대로다.
    """

    def __init__(self, message: str, usage: Any = None):
        super().__init__(message)
        self.usage = usage


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
            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=Settings.LLM_REQUEST_TIMEOUT_S,
                max_retries=0,
            )

    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        schema: Optional[Type[BaseModel]] = None,
        purpose: str = "unknown",
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResult:
        # 지출 상한이나 서킷브레이커로 이미 멈춘 런이면 같은 중단(LLMRunStop)을 다시 낸다. 킬 스위치보다
        # 먼저 본다 - 상한이 킬 스위치를 켠 프로세스에서 다른 스레드가 "kill_switch" 결과를 받아
        # 로컬 폴백으로 넘어가지 않게 (docs/adr/0035).
        guard = get_budget_guard()
        guard.ensure_run_active(self.provider, self.model, purpose)

        killed = check_kill_switch(self.provider, self.model, purpose)
        if killed is not None:
            return killed

        backoff = INITIAL_BACKOFF
        last_error = "unknown error"
        call_start = time.time()
        attempt = 0
        schema_failures = 0
        last_status: Optional[int] = None

        for attempt in range(1, MAX_RETRIES + 1):
            attempt_start = time.time()
            self._rate_limiter.wait()
            # 이 시도의 최악 비용을 지출 원장에 예약한다. 상한을 넘으면 요청을 보내기 전에
            # LLMBudgetExceeded가 나고, 아래 분기들은 결과에 맞게 정산한다(재시도도 시도마다 과금될 수 있다).
            spend = guard.begin_attempt(
                provider=self.provider, model=self.model, purpose=purpose,
                messages=messages, schema=schema, max_tokens=max_tokens,
            )
            try:
                start = time.time()

                if schema is not None and self.supports_json_schema:
                    text, parsed, response_usage = self._call_structured(messages, schema, temperature, max_tokens)
                else:
                    text, parsed, response_usage = self._call_json_mode_or_plain(
                        messages, schema, temperature, max_tokens
                    )

                latency = time.time() - start
                usage = LLMUsage.from_openai(response_usage)
                spend.settle("ok", usage)

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
                    schema_failures=schema_failures,
                )

            except (ValidationError, ValueError) as e:
                # 모델이 스키마를 못 지켰거나(JSON 모드 폴백) 검증에 실패한 경우.
                # 429/5xx는 아니지만 재시도할 가치가 있다(다음 시도에서 형식을 지킬 수 있음).
                last_error = str(e)
                schema_failures += 1
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
                # 응답은 왔다(과금됨). usage를 알면 실제 값으로, 모르면(SDK 안에서 난 검증 오류) 예약액으로.
                spend.settle("schema_invalid", LLMUsage.from_openai(getattr(e, "usage", None)))
                if not self._wait_before_retry(backoff, call_start):
                    break
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                continue

            except openai.APIStatusError as e:
                last_error = str(e)
                last_status = e.status_code
                latency = time.time() - attempt_start
                get_metrics_collector().record_call(
                    purpose=purpose, input_tokens=0, output_tokens=0,
                    latency_seconds=latency, success=False,
                    provider=self.provider, model=self.model,
                )
                # 5xx가 연속 실패 임계에 닿거나 402(선불 잔액 소진)면 여기서 LLMCircuitOpen이 난다 -
                # 백오프를 기다리며 재시도를 태우지 않는다.
                spend.settle(f"http_{e.status_code}")
                if e.status_code == 429 or e.status_code >= 500:
                    logger.warning(
                        "%s/%s: HTTP %s (attempt %s/%s), 재시도",
                        self.provider, self.model, e.status_code, attempt, MAX_RETRIES,
                    )
                    if not self._wait_before_retry(backoff, call_start):
                        break
                    backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF)
                    continue

                logger.error("%s/%s: HTTP %s (재시도 불가): %s", self.provider, self.model, e.status_code, e)
                return LLMResult(
                    text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                    attempts=attempt, provider=self.provider, model=self.model, error=last_error,
                    schema_failures=schema_failures, http_status=e.status_code,
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
                spend.settle("length", LLMUsage.from_openai(getattr(getattr(e, "completion", None), "usage", None)))
                return LLMResult(
                    text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                    attempts=attempt, provider=self.provider, model=self.model, error="length",
                    schema_failures=schema_failures,
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
                spend.settle(
                    "content_filter", LLMUsage.from_openai(getattr(getattr(e, "completion", None), "usage", None))
                )
                return LLMResult(
                    text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                    attempts=attempt, provider=self.provider, model=self.model, error="content_filter",
                    schema_failures=schema_failures,
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
                # 요청이 프로바이더에서 처리됐는지 알 수 없다 - 예약액 그대로 지출로 잡는다.
                spend.settle("timeout" if isinstance(e, openai.APITimeoutError) else "connection")
                if not self._wait_before_retry(backoff, call_start):
                    break
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
                spend.settle("error")
                return LLMResult(
                    text=None, parsed=None, usage=LLMUsage(), latency_s=latency,
                    attempts=attempt, provider=self.provider, model=self.model, error=last_error,
                    schema_failures=schema_failures,
                )

            finally:
                # 위 분기가 정산하지 않고 빠져나간 경우(KeyboardInterrupt 등)에도 예약을 닫는다
                spend.ensure_settled()

        elapsed = time.time() - call_start
        reason = (
            f"call deadline ({Settings.LLM_CALL_DEADLINE_S:.0f}s) reached"
            if attempt < MAX_RETRIES
            else f"max retries ({MAX_RETRIES}) exhausted"
        )
        return LLMResult(
            text=None, parsed=None, usage=LLMUsage(), latency_s=elapsed,
            attempts=attempt, provider=self.provider, model=self.model,
            error=f"{reason}: {last_error}",
            schema_failures=schema_failures, http_status=last_status,
        )

    @staticmethod
    def _wait_before_retry(backoff: float, call_start: float) -> bool:
        if time.time() - call_start + backoff > Settings.LLM_CALL_DEADLINE_S:
            return False
        time.sleep(backoff)
        return True

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
            raise _ResponseRejected(
                "structured output이 스키마 검증을 통과하지 못했습니다 (parsed=None)",
                usage=getattr(response, "usage", None),
            )
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
        usage = getattr(response, "usage", None)

        parsed = None
        if schema is not None:
            raw = extract_json_from_response(text)
            if raw is None:
                raise _ResponseRejected("JSON 모드 응답에서 JSON을 추출하지 못했습니다", usage=usage)
            try:
                parsed = schema.model_validate(raw)
            except ValidationError as e:
                raise _ResponseRejected(str(e), usage=usage) from e

        return text, parsed, usage


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
        # 지출 예약·정산은 요청을 실제로 보내는 NaverHyperCLOVAClient.chat_completion 안에서 한다
        # (core/llm_client.py). 여기서는 이미 멈춘 런인지만 본다.
        get_budget_guard().ensure_run_active(self.provider, self.model, purpose)

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
