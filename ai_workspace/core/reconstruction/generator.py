# 뉴스레터 본문 생성기
# - 여러 기사를 통합하여 하나의 뉴스레터로 재구성
# - 2-call 방식: 본문 생성 → 메타데이터 생성

from typing import Dict, List, Optional

from core.llm import LLMClient, get_client
from core.llm.schemas import NewsletterContent, NewsletterMeta

from .prompts import SYSTEM_EDITOR_ROLE, SYSTEM_META_ROLE, CONTENT_GEN_PROMPT, META_GEN_PROMPT
from .validator import cleanup_content_text, normalize_meta

# 생성 프롬프트에 넣는 기사 수/기사당 본문 길이. judge v2(workflow/evaluators.py)도
# 같은 값·같은 선택 규칙을 써서 "생성기가 본 것과 같은 원문"으로 평가한다 - judge가
# 더 적게 보면 생성기가 올바르게 옮긴 사실을 근거 없음으로 오판한다.
MAX_PROMPT_ARTICLES = 10
ARTICLE_PROMPT_CHARS = 1500


def select_prompt_articles(articles: List[Dict]) -> List[Dict]:
    """컨텍스트 길이 제한용 기사 선택: 본문이 긴 순서로 MAX_PROMPT_ARTICLES개.

    articles는 LangGraph state["current_articles"]를 그대로 참조하므로 in-place 정렬
    (articles.sort())하면 호출자의 state가 변형된다. sorted()로 새 리스트를 만든다.
    """
    if len(articles) <= MAX_PROMPT_ARTICLES:
        return list(articles)
    return sorted(articles, key=lambda x: len(x.get('content') or ''), reverse=True)[:MAX_PROMPT_ARTICLES]


class NewsReconstructor:
    """
    여러 개의 뉴스를 종합하여 하나의 뉴스레터로 재구성

    2-call 방식(토큰 초과 문제 완화)
    - Call 1: content만 생성
    - Call 2: meta만 생성 (title, keyword, sentence, category)
    """

    def __init__(self, llm_client: Optional[LLMClient] = None):
        # role="generator" - GEN_PROVIDER/GEN_MODEL로 프로바이더를 정한다 (docs/adr/0005)
        self.client: LLMClient = llm_client or get_client("generator")
        self._fallback_parts: List[str] = []

    def reconstruct(self, articles: List[Dict], feedback: Optional[str] = None) -> Optional[Dict]:
        """
        여러 기사를 통합하여 하나의 뉴스레터로 재구성
        """
        if not articles:
            return None
        # LLM 호출이 실패해 로컬 휴리스틱으로 채운 부분("content"/"meta"). 워크플로우는
        # 이 표시가 있는 초안을 발행하지 않는다(workflow/nodes.py::check_faithfulness).
        self._fallback_parts = []

        # 기사 개수 제한 (컨텍스트 길이 초과 방지)
        articles = select_prompt_articles(articles)

        # 프롬프트에 넣을 기사 텍스트 구성
        articles_text = self._build_articles_text(articles)

        # -------------------------
        # Call #1: 본문 생성기
        # -------------------------
        content_text = self._generate_content(articles_text, feedback)
        if not content_text:
            return None

        # -------------------------
        # Call #2: title, category, keyword, sentence
        # -------------------------
        meta = self._generate_meta(content_text)
        if not meta:
            return None

        # 1 call, 2 call 결과 합치기
        result = {
            "title": (meta.get("title") or "").strip(),
            "sentence": (meta.get("sentence") or "").strip(),
            "content": content_text.strip(),
            "keywords": meta.get("keywords") or [],
            "categories": meta.get("categories") or [],
        }

        # 최소 검증 
        result = normalize_meta(result)
        if not result:
            return None
        if self._fallback_parts:
            result["_fallback"] = list(self._fallback_parts)

        return result

    def _build_articles_text(self, articles: List[Dict]) -> str:
        """기사 목록을 프롬프트용 텍스트로 변환"""
        articles_text = ""
        for i, art in enumerate(articles, 1):
            content_preview = art.get('content', '')[:ARTICLE_PROMPT_CHARS] if art.get('content') else "(본문 없음)"
            articles_text += f"""
---
[기사 {i}]
출처: {art.get('press_name', '알 수 없음')}
제목: {art.get('title', '')}
본문:
{content_preview}
---
"""
        return articles_text

    def _generate_content(self, articles_text: str, feedback: Optional[str] = None) -> Optional[str]:
        # Call 1: 본문만 생성

        # Feedback 문자열 구성
        feedback_instruction = ""
        if feedback:
            feedback_instruction = f"""
⚠️ 이전 작성이 다음 이유로 거부되었습니다:
{feedback}

위 피드백을 반영하여 수정된 본문을 작성하세요.
"""

        # Prompts 모듈의 템플릿 사용 
        prompt = CONTENT_GEN_PROMPT.format(
            feedback_instruction=feedback_instruction,
            articles_text=articles_text
        )

        def fallback_content() -> str:
            # Build a simple summary from titles in articles_text
            lines = []
            for line in articles_text.splitlines():
                if line.strip().startswith("제목:"):
                    lines.append(line.strip().replace("제목:", "").strip())
            if not lines:
                return "관련 기사들의 핵심 내용을 요약했습니다."
            bullets = "\n".join([f"- {t}" for t in lines[:10]])
            return f"다음은 관련 기사들의 핵심 요약입니다.\n{bullets}"

        messages = [
            {"role": "system", "content": SYSTEM_EDITOR_ROLE},
            {"role": "user", "content": prompt},
        ]

        # 재시도/백오프는 LLMClient.complete() 내부에서 처리한다 (core/llm/adapters.py)
        result = self.client.complete(
            messages=messages,
            schema=NewsletterContent,
            temperature=0.2,
            max_tokens=8192,
            purpose="newsletter_content_gen",
        )
        if result.parsed is not None:
            return cleanup_content_text(result.parsed.content)

        self._fallback_parts.append("content")
        return fallback_content()

    def _generate_meta(self, content_text: str) -> Optional[Dict]:
        # Call 2: 메타데이터(title/sentence/keywords/categories)만 생성

        # Prompts 모듈의 템플릿 사용
        prompt = META_GEN_PROMPT.format(content_text=content_text)

        def fallback_meta() -> Dict:
            import re
            from collections import Counter
            from core.reconstruction.repository import CATEGORY_MAP

            title = (content_text.strip().split("\n")[0] if content_text else "뉴스 요약").strip()
            sentence = title
            tokens = re.findall(r"[A-Za-z0-9가-힣]{2,}", content_text or "")
            freq = Counter(tokens)
            keywords = [w for w, _ in freq.most_common(5)]
            if len(keywords) < 5:
                keywords += ["뉴스"] * (5 - len(keywords))

            # infer category from keywords
            categories = []
            for k in keywords:
                mapped = CATEGORY_MAP.get(k)
                if mapped and mapped not in categories:
                    categories.append(mapped)
                if len(categories) == 2:
                    break
            if not categories:
                categories = ["사회"]

            return {
                "title": title,
                "sentence": sentence,
                "keywords": keywords[:5],
                "categories": categories
            }

        messages = [
            {"role": "system", "content": SYSTEM_META_ROLE},
            {"role": "user", "content": prompt},
        ]

        result = self.client.complete(
            messages=messages,
            schema=NewsletterMeta,
            temperature=0.2,
            max_tokens=1024,
            purpose="newsletter_meta_gen",
        )
        if result.parsed is not None:
            return result.parsed.model_dump()

        self._fallback_parts.append("meta")
        return fallback_meta()
