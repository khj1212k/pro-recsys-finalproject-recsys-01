# LLM 기반 평가기
# - 클러스터 품질 평가, 뉴스레터 품질 평가
from typing import Dict, List, Optional

from config.settings import Settings
from core.llm import LLMClient, get_client
from core.llm.schemas import (
    ClusterEval as ClusterEvalSchema,
    NewsletterEval as NewsletterEvalSchema,
    NewsletterEvalV2,
)
from core.reconstruction.generator import ARTICLE_PROMPT_CHARS, select_prompt_articles


class ClusterEvaluator:

    SYSTEM_PROMPT = """You are an expert news analyst evaluating clusters of news articles.
Your job is to determine if articles in a cluster represent a single coherent news event or topic.
Return ONLY valid JSON. No extra text, no code blocks, no markdown."""

    USER_PROMPT_TEMPLATE = """You are evaluating a cluster of {n_articles} news articles.
Determine if these articles represent a **single coherent news event** or topic.

**Criteria for PASS**:
1. All articles discuss the same event OR closely related sub-events of one issue.
2. No article is completely unrelated "noise".
3. The cluster has a clear central theme that can be summarized in one sentence.

**Criteria for FAIL**:
1. Articles discuss 2+ completely unrelated events.
2. An article is about a different country/topic/domain with no connection.

**Articles in this cluster:**
{articles_text}

**Output JSON only**:
{{
  "decision": "PASS" or "FAIL",
  "confidence": 0.0-1.0,
  "summary": "One-line theme of this cluster",
  "feedback": "If FAIL, explain why.",
  "outlier_indices": [indices of noise articles to remove],
  "sub_groups": [
      [indices of group A],
      [indices of group B]
  ] 
  // If FAIL due to multiple events mixed, provide 'sub_groups' to split them. 
  // Max 2 sub-groups. If just noise, leave sub_groups empty.
}}

Only output valid JSON. No other text.
If unsure, still return valid JSON with empty strings/lists."""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        """
        Initialize ClusterEvaluator

        Args:
            llm_client: 주입할 LLMClient (테스트/DI용). None이면 role="judge"로 레지스트리에서 가져온다.
        """
        # role="judge" - JUDGE_PROVIDER/JUDGE_MODEL로 프로바이더를 정한다 (docs/adr/0005).
        # 생성기(role="generator")와 다른 모델 계열을 쓰는 것이 LLM-as-judge 요건이다.
        self.client: LLMClient = llm_client or get_client("judge")

    def evaluate(self, articles: List[Dict]) -> Dict:
        """
        Evaluate a cluster of articles.

        Args:
            articles: List of article dicts with 'title', 'press_name', 'content'

        Returns:
            Evaluation result dict with decision, confidence, summary, feedback, outlier_indices
        """
        if not articles:
            return {
                "decision": "FAIL",
                "confidence": 0.0,
                "summary": "",
                "feedback": "Empty cluster",
                "outlier_indices": []
            }

        # Build articles text (title only for evaluation to save tokens)
        articles_text = ""
        for i, art in enumerate(articles):
            content_preview = (art.get('content', '') or '')[:500]
            articles_text += f"""
[Article {i}]
Source: {art.get('press_name', 'Unknown')}
Title: {art.get('title', '')}
Content Preview: {content_preview}...
"""

        prompt = self.USER_PROMPT_TEMPLATE.format(
            n_articles=len(articles),
            articles_text=articles_text
        )

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        # 재시도/백오프는 LLMClient.complete() 내부에서 처리한다 (core/llm/adapters.py)
        result = self.client.complete(
            messages=messages,
            schema=ClusterEvalSchema,
            temperature=0.1,
            max_tokens=2048,
            purpose="cluster_eval",
        )

        if result.parsed is not None:
            parsed = result.parsed
            return {
                "decision": parsed.decision,
                "confidence": parsed.confidence,
                "summary": parsed.summary,
                "feedback": parsed.feedback,
                "outlier_indices": parsed.outlier_indices,
                "sub_groups": parsed.sub_groups,
            }

        if result.text:
            upper = result.text.upper()
            decision = "FAIL" if "FAIL" in upper and "PASS" not in upper else "PASS" if "PASS" in upper else "FAIL"
            return {
                "decision": decision,
                "confidence": 0.1,
                "summary": "",
                "feedback": "JSON parsing failed",
                "outlier_indices": [],
                "sub_groups": []
            }

        return {
            "decision": "FAIL",
            "confidence": 0.0,
            "summary": "",
            "feedback": "LLM response empty",
            "outlier_indices": [],
            "sub_groups": []
        }


class NewsletterEvaluatorV1:
    """(구) judge v1: 원문 제목 최대 5개만 보고 score >= 5면 PASS.

    워크플로우는 더 이상 쓰지 않는다(NewsletterEvaluator = v2). bake-off에서 "본문을
    보여 준 v2가 사람 라벨과 더 잘 맞는가"를 재기 위한 기준선으로만 남겨 둔다(ADR 0009).
    """

    SYSTEM_PROMPT = """You are an expert news editor evaluating newsletter drafts.
Your job is to ensure the newsletter meets quality standards for publication.
Return ONLY valid JSON. No extra text, no code blocks, no markdown."""

    USER_PROMPT_TEMPLATE = """You are evaluating a generated newsletter draft.

**Criteria for PASS (score >= 5)**:
1. **Content**: Provides a clear summary of the news event.
2. **Completeness**: Covers the key points from source articles.

**Criteria for FAIL (score < 5)**:
1. Missing key facts from source articles.
2. Completely incoherent structure.
3. Contains hallucinatory or incorrectly verified facts.

**Source Articles Summary:**
{source_summary}

**Newsletter Draft:**
Title: {title}
Summary: {sentence}
Content:
{content}

**Output JSON only**:
{{
  "decision": "PASS" or "FAIL",
  "score": 0-10,
  "feedback": "Specific issues to fix if FAIL. Be very specific about what to change.",
  "issues": ["list", "of", "specific", "problems"]
}}

Only output valid JSON. No other text.
If unsure, still return valid JSON with empty strings/lists."""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        # role="judge" - ClusterEvaluator와 동일하게 생성기와 다른 모델 계열을 쓴다.
        self.client: LLMClient = llm_client or get_client("judge")

    def evaluate(self, newsletter: Dict, source_articles: List[Dict]) -> Dict:
        if not newsletter:
            return {
                "decision": "FAIL",
                "score": 0,
                "feedback": "Empty newsletter",
                "issues": ["No content to evaluate"]
            }

        source_summary = ""
        for i, art in enumerate(source_articles[:5]):
            source_summary += f"- [{art.get('press_name', '')}] {art.get('title', '')}\n"

        prompt = self.USER_PROMPT_TEMPLATE.format(
            source_summary=source_summary,
            title=newsletter.get('title', ''),
            sentence=newsletter.get('sentence', ''),
            content=newsletter.get('content', '')
        )

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]

        result = self.client.complete(
            messages=messages,
            schema=NewsletterEvalSchema,
            temperature=0.1,
            max_tokens=2048,
            purpose="newsletter_eval",
        )

        if result.parsed is not None:
            parsed = result.parsed
            score = parsed.score
            decision = "PASS" if score >= 5 else "FAIL"
            return {
                "decision": decision,
                "score": score,
                "feedback": parsed.feedback,
                "issues": parsed.issues,
            }

        if result.text:
            upper = result.text.upper()
            decision = "PASS" if "PASS" in upper and "FAIL" not in upper else "FAIL"
            return {
                "decision": decision,
                "score": 5 if decision == "PASS" else 0,
                "feedback": "JSON parsing failed",
                "issues": []
            }

        return {
            "decision": "FAIL",
            "score": 0,
            "feedback": "LLM response empty",
            "issues": ["API call returned empty"]
        }


class NewsletterEvaluator:
    """judge v2 (docs/adr/0010).

    v1과의 차이:
      - 원문 제목이 아니라 생성기가 본 것과 같은 기사·같은 길이의 **본문 발췌**를 본다.
      - 기준별 점수(사실 충실성/핵심 사실/구성/문체, 1~5)와 근거 없는 주장 목록을 받는다.
      - PASS/FAIL은 모델이 아니라 Settings의 임계값으로 코드가 결정한다(보정 대상).
    """

    VERSION = "v2"

    SYSTEM_PROMPT = """당신은 뉴스 팩트체크를 담당하는 편집자입니다.
뉴스레터 초안을 원문 기사와 대조해 평가합니다. JSON만 출력하세요."""

    USER_PROMPT_TEMPLATE = """아래 [원문 기사]만을 근거로 [뉴스레터 초안]을 평가하세요.
외부 지식으로 사실 여부를 판단하지 마세요. 원문에서 확인할 수 없는 내용은 실제로 사실이더라도
"근거 없음"입니다. 원문 기사는 각 본문의 앞부분 발췌이며, 초안 작성자도 이 발췌만 보았습니다.

[평가 기준] 각 1~5점
- faithfulness (사실 충실성): 수치·날짜·인명·기관명·인용문이 원문과 일치하는가.
  5=모두 일치 / 4=표현만 다르고 의미 동일 / 3=사소한 근거 없음 1개 / 2=근거 없는 사실 여러 개 / 1=핵심 사실 왜곡 또는 지어냄
- coverage (핵심 사실): 원문들이 공통으로 전하는 핵심 사실을 담았는가.
  5=핵심 모두 포함 / 3=일부 누락 / 1=핵심 대부분 누락
- coherence (구성): 여러 기사를 하나의 흐름으로 통합했는가(단순 나열·모순·억지 인과 없음).
  5=자연스러운 통합 / 3=부분적 나열 / 1=모순 또는 뒤죽박죽
- style (문체): 객관적 톤, 선정적 표현(충격, 경악, 파문 등) 없음, 바로 발행할 수 있는 문장.
  5=그대로 발행 가능 / 3=손볼 곳 있음 / 1=발행 불가

[unsupported_claims]
원문에서 근거를 찾을 수 없는 주장을 초안에서 **그대로 복사해** claim에 넣고, reason에 이유를 적으세요.
없으면 빈 배열입니다.

[feedback]
재작성하는 사람이 바로 고칠 수 있도록 구체적으로 적으세요(무엇을, 어떻게).

[원문 기사]
{articles_text}

[뉴스레터 초안]
제목: {title}
한줄소개: {sentence}
본문:
{content}

출력 JSON 형식:
{{"scores": {{"faithfulness": 1-5, "coverage": 1-5, "coherence": 1-5, "style": 1-5}},
  "unsupported_claims": [{{"claim": "초안의 정확한 부분 문자열", "reason": "..."}}],
  "feedback": "..."}}"""

    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.client: LLMClient = llm_client or get_client("judge")

    @staticmethod
    def build_articles_text(source_articles: List[Dict]) -> str:
        parts = []
        for i, art in enumerate(select_prompt_articles(source_articles), 1):
            body = (art.get("content") or "")[:ARTICLE_PROMPT_CHARS] or "(본문 없음)"
            parts.append(
                f"[기사 {i}] 출처: {art.get('press_name', '') or '알 수 없음'}\n"
                f"제목: {art.get('title', '')}\n본문(발췌):\n{body}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def thresholds() -> Dict[str, int]:
        return {
            "min_criterion_score": int(Settings.JUDGE_MIN_CRITERION_SCORE),
            "max_unsupported_claims": int(Settings.JUDGE_MAX_UNSUPPORTED_CLAIMS),
        }

    def evaluate(self, newsletter: Dict, source_articles: List[Dict]) -> Dict:
        if not newsletter:
            return self._fail("Empty newsletter", ["No content to evaluate"])

        prompt = self.USER_PROMPT_TEMPLATE.format(
            articles_text=self.build_articles_text(source_articles),
            title=newsletter.get("title", ""),
            sentence=newsletter.get("sentence", ""),
            content=newsletter.get("content", ""),
        )
        result = self.client.complete(
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            schema=NewsletterEvalV2,
            temperature=0.1,
            max_tokens=2048,
            purpose="newsletter_eval",
        )

        if result.parsed is None:
            # v1은 원문 텍스트에서 PASS/FAIL 문자열을 찾는 휴리스틱으로 통과시키기도 했지만,
            # 기준별 점수가 없는 응답으로 발행을 허용할 근거가 없다 - 실패로 닫는다.
            reason = "judge 응답을 스키마로 해석하지 못함" if result.text else "LLM response empty"
            return self._fail(reason, [result.error or reason])

        parsed: NewsletterEvalV2 = result.parsed
        criteria = parsed.scores.model_dump()
        claims = [c.model_dump() for c in parsed.unsupported_claims]
        th = self.thresholds()
        passed = min(criteria.values()) >= th["min_criterion_score"] and len(claims) <= th["max_unsupported_claims"]

        issues = [f"{name}={score}" for name, score in criteria.items() if score < th["min_criterion_score"]]
        issues += [f"근거 없는 주장: {c['claim']} ({c['reason']})" for c in claims]
        feedback = parsed.feedback or ""
        if claims:
            feedback += "\n원문에서 근거를 찾을 수 없는 주장(삭제하거나 원문에 있는 표현으로 고칠 것):\n" + "\n".join(
                f"- \"{c['claim']}\"" for c in claims
            )

        mean = sum(criteria.values()) / len(criteria)
        return {
            "decision": "PASS" if passed else "FAIL",
            "score": round((mean - 1) / 4 * 10, 2),
            "feedback": feedback.strip(),
            "issues": issues,
            "criteria": criteria,
            "unsupported_claims": claims,
            "thresholds": th,
            "judge_version": self.VERSION,
        }

    def _fail(self, feedback: str, issues: List[str]) -> Dict:
        return {
            "decision": "FAIL",
            "score": 0,
            "feedback": feedback,
            "issues": issues,
            "criteria": None,
            "unsupported_claims": [],
            "thresholds": self.thresholds(),
            "judge_version": self.VERSION,
        }
