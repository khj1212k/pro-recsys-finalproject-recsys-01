# LLM 기반 평가기
# - 클러스터 품질 평가, 뉴스레터 품질 평가
import os
import json
from typing import Dict, List, Optional

from core.llm_client import get_llm_client, extract_json_from_response, BaseLLMClient
from config.settings import Settings


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

    def __init__(self, provider: Optional[str] = None):
        """
        Initialize ClusterEvaluator

        Args:
            provider: LLM provider ('naver', 'openai', or None for auto-detect)
        """
        self.client: BaseLLMClient = get_llm_client(provider)

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

        max_retries = Settings.MAX_JSON_PARSE_RETRIES
        last_response = None
        for _ in range(max_retries):
            response = self.client.chat_completion(
                messages=messages,
                temperature=0.1,
                max_tokens=2048,
                response_format={"type": "json_object"},
                purpose="cluster_eval"
            )
            if not response:
                continue
            last_response = response
            result = extract_json_from_response(response)
            if not result:
                continue

            decision = (result.get("decision") or "FAIL").upper()
            if decision not in ("PASS", "FAIL"):
                decision = "FAIL"
            def _to_int_list(items):
                out = []
                for x in items or []:
                    try:
                        out.append(int(x))
                    except Exception:
                        continue
                return out

            def _to_int_groups(groups):
                out = []
                for g in groups or []:
                    if not isinstance(g, list):
                        continue
                    converted = _to_int_list(g)
                    if converted:
                        out.append(converted)
                return out

            return {
                "decision": decision,
                "confidence": float(result.get("confidence", 0.0)),
                "summary": result.get("summary", ""),
                "feedback": result.get("feedback", ""),
                "outlier_indices": _to_int_list(result.get("outlier_indices", [])),
                "sub_groups": _to_int_groups(result.get("sub_groups", []))
            }

        if last_response:
            upper = last_response.upper()
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

    def __init__(self, provider: Optional[str] = None):
        self.client: BaseLLMClient = get_llm_client(provider)

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

        max_retries = Settings.MAX_JSON_PARSE_RETRIES
        last_response = None
        for _ in range(max_retries):
            response = self.client.chat_completion(
                messages=messages,
                temperature=0.1,
                max_tokens=2048,
                response_format={"type": "json_object"},
                purpose="newsletter_eval"
            )
            if not response:
                continue
            last_response = response
            result = extract_json_from_response(response)
            if not result:
                continue

            score = int(result.get("score", 0))
            decision = "PASS" if score >= 5 else "FAIL"
            return {
                "decision": decision,
                "score": score,
                "feedback": result.get("feedback", ""),
                "issues": result.get("issues", [])
            }

        if last_response:
            upper = last_response.upper()
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
