# LLM 기반 평가기
# - 클러스터 품질 평가, 뉴스레터 품질 평가
from typing import Dict, List, Optional

from core.llm import LLMClient, get_client
from core.llm.schemas import ClusterEval as ClusterEvalSchema, NewsletterEval as NewsletterEvalSchema


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


class NewsletterEvaluator:

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
