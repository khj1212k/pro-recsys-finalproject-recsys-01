# LLM 구조화 출력 스키마
# - 각 파이프라인 호출 지점(클러스터 평가, 본문 생성, 메타 생성, 뉴스레터 평가, 문체 변환)의
#   기존 프롬프트가 요구하던 출력 형태를 그대로 pydantic 모델로 옮긴 것.
# - 필드는 core/reconstruction/prompts.py, workflow/evaluators.py, core/tone_converter.py의
#   프롬프트 텍스트에서 그대로 도출했다 (이 PR에서는 프롬프트 문구 자체를 바꾸지 않는다).

from typing import List, Literal

from pydantic import BaseModel, Field, field_validator


class ClusterEval(BaseModel):
    """workflow/evaluators.py::ClusterEvaluator.USER_PROMPT_TEMPLATE의 출력 JSON"""

    decision: Literal["PASS", "FAIL"]
    confidence: float = 0.0
    summary: str = ""
    feedback: str = ""
    outlier_indices: List[int] = Field(default_factory=list)
    sub_groups: List[List[int]] = Field(default_factory=list)


class NewsletterContent(BaseModel):
    """core/reconstruction/prompts.py::CONTENT_GEN_PROMPT(Call #1)의 출력.

    원 프롬프트는 "본문 텍스트만 출력, JSON 금지"를 요구하지만, 구조화 출력은
    디코딩 단계에서 스키마를 강제하는 것이라 프롬프트 문구와 무관하게 동작한다.
    단일 필드 스키마로 감싸도 실제 산출물(본문 텍스트)은 동일하다.
    """

    content: str


class NewsletterMeta(BaseModel):
    """core/reconstruction/prompts.py::META_GEN_PROMPT(Call #2)의 출력"""

    title: str
    sentence: str
    keywords: List[str] = Field(default_factory=list)
    categories: List[str] = Field(default_factory=list)


class NewsletterEval(BaseModel):
    """workflow/evaluators.py::NewsletterEvaluator.USER_PROMPT_TEMPLATE의 출력 JSON"""

    decision: Literal["PASS", "FAIL"]
    score: int = 0
    feedback: str = ""
    issues: List[str] = Field(default_factory=list)


class CriterionScores(BaseModel):
    """judge v2 기준별 점수(1~5). 사람 라벨(docs/eval/labeling-guide.md)과 대응시켜 보정한다
    (ADR 0009/0010): faithfulness↔사실 오류 구간, coverage↔핵심 사실 커버리지,
    coherence·style↔문장·구성 품질 1~5(가이드 2.3의 1/3/5 기준점이 프롬프트의 기준점과 같다)."""

    faithfulness: int
    coverage: int
    coherence: int
    style: int

    # 범위 제약을 JSON 스키마(minimum/maximum)로 걸지 않고 여기서 자른다: 프로바이더별
    # 구조화 출력이 지원하는 JSON 스키마 키워드가 달라(Upstage는 OpenAI 스펙의 부분집합)
    # 스키마를 가장 단순한 형태로 유지하기 위함.
    @field_validator("faithfulness", "coverage", "coherence", "style", mode="after")
    @classmethod
    def _clamp(cls, v: int) -> int:
        return max(1, min(5, int(v)))


class UnsupportedClaim(BaseModel):
    claim: str
    reason: str = ""


class NewsletterEvalV2(BaseModel):
    """workflow/evaluators.py::NewsletterEvaluator(judge v2)의 출력. PASS/FAIL은 모델이
    아니라 코드가 Settings의 임계값으로 결정한다."""

    scores: CriterionScores
    unsupported_claims: List[UnsupportedClaim] = Field(default_factory=list)
    feedback: str = ""


class ToneResult(BaseModel):
    """core/tone_converter.py::TONE_CONVERSION_PROMPT의 출력 JSON"""

    title: str
    summary: str
    content: str
    keywords: List[str] = Field(default_factory=list)


__all__ = [
    "ClusterEval",
    "NewsletterContent",
    "NewsletterMeta",
    "NewsletterEval",
    "CriterionScores",
    "UnsupportedClaim",
    "NewsletterEvalV2",
    "ToneResult",
]
