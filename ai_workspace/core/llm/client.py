# LLM 클라이언트 공통 인터페이스
# - 모든 프로바이더 어댑터(core/llm/adapters.py)가 구현하는 계약
# - 호출부(core/reconstruction/generator.py, workflow/evaluators.py, core/tone_converter.py)는
#   이 인터페이스만 알면 되고, 프로바이더별 REST/SDK 세부사항은 몰라도 된다.

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Type

from pydantic import BaseModel


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResult:
    text: Optional[str]
    parsed: Optional[BaseModel]
    usage: LLMUsage
    latency_s: float
    attempts: int
    provider: str
    model: str
    error: Optional[str] = None
    # 이번 complete() 동안 모델 응답이 스키마 검증에 실패한 횟수(전송 계층 재시도는 제외).
    # 0이면서 parsed가 있으면 "첫 응답이 스키마를 통과"했다는 뜻이다 (ADR 0009 G3 게이트).
    schema_failures: int = 0
    # 재시도하지 않고 끝난 HTTP 오류의 상태 코드(402 선불 미결제 등). 인프라 실패와
    # 모델 품질 실패를 구분하는 데 쓴다.
    http_status: Optional[int] = None

    @property
    def ok(self) -> bool:
        """text/parsed 중 하나라도 얻었고 error가 없으면 성공으로 취급한다."""
        return self.error is None and (self.text is not None or self.parsed is not None)


class LLMClient(ABC):
    """프로바이더 어댑터가 구현해야 하는 최소 계약.

    구현체는 `provider`/`model` 속성을 가져야 하고, 내부적으로 재시도/백오프/메트릭
    기록을 책임진다 - 호출부는 이 메서드를 한 번만 부르면 된다.
    """

    provider: str
    model: str

    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        schema: Optional[Type[BaseModel]] = None,
        purpose: str = "unknown",
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> LLMResult:
        """messages를 전달해 하나의 응답을 얻는다.

        schema가 주어지면 (가능한 경우) 네이티브 json_schema 구조화 출력을,
        아니면 JSON 모드 + pydantic 검증 + extract_json_from_response 복구로 폴백한다.
        429/5xx/timeout에 대해 지수 백오프로 유한 횟수만 재시도한다.
        """
        raise NotImplementedError
