"""
LLM-based Evaluators for LangGraph workflow
Supports Naver HyperCLOVA X and OpenAI
"""
import os
import json
from typing import Dict, List, Optional

from core.llm_client import get_llm_client, extract_json_from_response, BaseLLMClient


class ClusterEvaluator:
    """Evaluates if a cluster represents a single coherent news event"""

    SYSTEM_PROMPT = """You are an expert news analyst evaluating clusters of news articles.
Your job is to determine if articles in a cluster represent a single coherent news event or topic.
Always respond in valid JSON format only. No other text."""

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
  "feedback": "If FAIL, explain which articles don't belong and why. If PASS, leave empty.",
  "outlier_indices": [indices of outlier articles (0-indexed), empty if PASS]
}}

Only output valid JSON. No other text."""

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

        try:
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]

            response = self.client.chat_completion(
                messages=messages,
                temperature=0.1,
                max_tokens=2048
            )

            if not response:
                return {
                    "decision": "FAIL",
                    "confidence": 0.0,
                    "summary": "",
                    "feedback": "LLM response empty",
                    "outlier_indices": []
                }

            result = extract_json_from_response(response)

            if not result:
                return {
                    "decision": "FAIL",
                    "confidence": 0.0,
                    "summary": "",
                    "feedback": f"JSON parsing failed: {response[:100]}...",
                    "outlier_indices": []
                }

            # Ensure all required fields exist
            return {
                "decision": result.get("decision", "FAIL"),
                "confidence": float(result.get("confidence", 0.0)),
                "summary": result.get("summary", ""),
                "feedback": result.get("feedback", ""),
                "outlier_indices": result.get("outlier_indices", [])
            }

        except Exception as e:
            print(f"Cluster evaluation failed: {e}")
            return {
                "decision": "FAIL",
                "confidence": 0.0,
                "summary": "",
                "feedback": f"Evaluation error: {str(e)}",
                "outlier_indices": []
            }


class NewsletterEvaluator:
    """Evaluates newsletter quality against defined criteria"""

    SYSTEM_PROMPT = """You are an expert news editor evaluating newsletter drafts.
Your job is to ensure the newsletter meets quality standards for publication.
Always respond in valid JSON format only. No other text."""

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

Only output valid JSON. No other text."""

    def __init__(self, provider: Optional[str] = None):
        """
        Initialize NewsletterEvaluator

        Args:
            provider: LLM provider ('naver', 'openai', or None for auto-detect)
        """
        self.client: BaseLLMClient = get_llm_client(provider)

    def evaluate(self, newsletter: Dict, source_articles: List[Dict]) -> Dict:
        """
        Evaluate a newsletter draft.

        Args:
            newsletter: Dict with title, sentence, content, keywords, categories
            source_articles: Original articles used to generate the newsletter

        Returns:
            Evaluation result dict with decision, score, feedback, issues
        """
        if not newsletter:
            return {
                "decision": "FAIL",
                "score": 0,
                "feedback": "Empty newsletter",
                "issues": ["No content to evaluate"]
            }

        # Build source summary
        source_summary = ""
        for i, art in enumerate(source_articles[:5]):  # Limit to 5 for token efficiency
            source_summary += f"- [{art.get('press_name', '')}] {art.get('title', '')}\n"

        prompt = self.USER_PROMPT_TEMPLATE.format(
            source_summary=source_summary,
            title=newsletter.get('title', ''),
            sentence=newsletter.get('sentence', ''),
            content=newsletter.get('content', '')
        )

        try:
            messages = [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]

            response = self.client.chat_completion(
                messages=messages,
                temperature=0.1,
                max_tokens=2048
            )

            if not response:
                return {
                    "decision": "FAIL",
                    "score": 0,
                    "feedback": "LLM response empty",
                    "issues": ["API call returned empty"]
                }

            result = extract_json_from_response(response)

            if not result:
                return {
                    "decision": "FAIL",
                    "score": 0,
                    "feedback": f"JSON parsing failed: {response[:100]}...",
                    "issues": ["Response not valid JSON"]
                }

            score = int(result.get("score", 0))
            decision = "PASS" if score >= 5 else "FAIL"

            return {
                "decision": decision,
                "score": score,
                "feedback": result.get("feedback", ""),
                "issues": result.get("issues", [])
            }

        except Exception as e:
            print(f"Newsletter evaluation failed: {e}")
            return {
                "decision": "FAIL",
                "score": 0,
                "feedback": f"Evaluation error: {str(e)}",
                "issues": ["API call failed"]
            }
