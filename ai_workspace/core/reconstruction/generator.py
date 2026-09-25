# 뉴스레터 본문 생성기
# - 여러 기사를 통합하여 하나의 뉴스레터로 재구성
# - 2-call 방식: 본문 생성 → 메타데이터 생성

from typing import Dict, List, Optional
from core.llm_client import get_llm_client, extract_json_from_response, BaseLLMClient


from .prompts import SYSTEM_EDITOR_ROLE, SYSTEM_META_ROLE, CONTENT_GEN_PROMPT, META_GEN_PROMPT
from .validator import cleanup_content_text, normalize_meta


class NewsReconstructor:
    """
    여러 개의 뉴스를 종합하여 하나의 뉴스레터로 재구성

    2-call 방식(토큰 초과 문제 완화)
    - Call 1: content만 생성
    - Call 2: meta만 생성 (title, keyword, sentence, category)
    """

    def __init__(self, provider: Optional[str] = None):
        self.client: BaseLLMClient = get_llm_client(provider)

    def reconstruct(self, articles: List[Dict], feedback: Optional[str] = None) -> Optional[Dict]:
        """
        여러 기사를 통합하여 하나의 뉴스레터로 재구성
        """
        if not articles:
            return None

        # 기사 개수 제한 (컨텍스트 길이 초과 방지)
        # 주의: articles는 LangGraph state["current_articles"]를 그대로 참조하므로
        # 원본 리스트를 in-place 정렬(articles.sort())하면 호출자의 state가 의도치 않게
        # 변형된다. sorted()로 새 리스트를 만들어 원본은 건드리지 않는다.
        MAX_ARTICLES = 10
        if len(articles) > MAX_ARTICLES:
            articles = sorted(articles, key=lambda x: len(x.get('content') or ''), reverse=True)[:MAX_ARTICLES]

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

        return result

    def _build_articles_text(self, articles: List[Dict]) -> str:
        """기사 목록을 프롬프트용 텍스트로 변환"""
        articles_text = ""
        for i, art in enumerate(articles, 1):
            content_preview = art.get('content', '')[:1500] if art.get('content') else "(본문 없음)"
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

        for _ in range(5):
            try:
                response = self.client.chat_completion(
                    messages=messages,
                    temperature=0.2,
                    max_tokens=8192,
                    purpose="newsletter_content_gen"
                )
                if response:
                    return cleanup_content_text(response)
            except Exception:
                continue

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

        for _ in range(5):
            try:
                response = self.client.chat_completion(
                    messages=messages,
                    temperature=0.2,
                    max_tokens=1024,
                    purpose="newsletter_meta_gen"
                )
                if not response:
                    continue
                result = extract_json_from_response(response)
                if result:
                    return result
            except Exception:
                continue

        return fallback_meta()
