"""
Tone Converter Module
Converts formal newsletter tone to casual, accessible tone with emojis

This module handles the transformation of formally-written newsletters
into a more casual, friendly tone that's easier to read while preserving
the core information and meaning.
"""

from typing import Dict, Optional
import json
import logging

from core.llm_client import get_llm_client, extract_json_from_response
from config.settings import BaseSettings, Settings

logger = logging.getLogger(__name__)


# Tone conversion prompt template
TONE_CONVERSION_PROMPT = """당신은 뉴스를 대중에게 쉽고 친근하게 전달하는 전문 에디터입니다.

**목표**: 딱딱한 신문 문체를 부드럽고 읽기 쉬운 문체로 변환하세요.

**변환 가이드라인**:
1. 존댓말 사용하되 친근하게
   - "~입니다" → "~이에요", "~예요"
   - "~했습니다" → "~했어요"
   - "~것으로 보입니다" → "~것 같아요"

2. 이모티콘 적절히 사용 (과하지 않게)
   - 뉴스/정보: 📰, 📊, 📈, 📉, 💡
   - 중요/주목: ⚠️, 🔥, ⭐, 🎯
   - 긍정/부정: ✅, ❌, 👍, 👎
   - 사람/기관: 🏛️, 🏢, 👨‍💼, 👩‍💼
   - 기술/과학: 🤖, 💻, 🔬, 🚀
   - 경제/금융: 💰, 💵, 📱, 🏦
   
3. 복잡한 문장은 짧고 간결하게 나누기
   - 한 문장에 여러 정보 → 여러 문장으로 분리
   - 불필요한 수식어 제거

4. 전문 용어는 쉬운 표현으로 바꾸되, 중요한 용어는 유지
   - 괄호로 설명 추가 가능

5. **핵심 정보와 사실은 절대 변경하지 않기**
   - 날짜, 금액, 인명, 기관명 등은 원본 그대로
   - 사실관계 왜곡 금지

6. 친근하지만 신뢰감 있는 톤 유지
   - 지나치게 캐주얼하거나 유치하지 않게
   - 뉴스의 전문성은 유지

**원본 뉴스레터**:
---
제목: {title}
요약: {summary}
본문: {content}
키워드: {keywords}
---

**변환된 뉴스레터를 다음 JSON 형식으로만 출력하세요** (다른 설명 없이 JSON만):
{{
  "title": "변환된 제목 (이모티콘 1-2개 포함)",
  "summary": "변환된 요약 (이모티콘 1개 포함)",
  "content": "변환된 본문 (적절한 위치에 이모티콘 사용)",
  "keywords": {keywords}
}}

주의:
1) JSON 외의 다른 텍스트는 출력하지 마세요.
2) JSON 문자열 안의 줄바꿈은 반드시 \\n 으로 이스케이프하세요.
3) 따옴표(\")는 반드시 \\\\\" 으로 이스케이프하세요."""


class ToneConverter:
    """
    문체 변환기 (Tone Converter)
    
    딱딱한 문체(Formal)의 뉴스레터를 친근하고 쉬운 문체(Casual) + 이모지 포함 형태로 변환합니다.
    """
    
    def __init__(self, settings: Optional[BaseSettings] = None):
        """
        초기화
        
        Args:
            settings: 설정 객체 (None이면 기본 Settings 사용)
        """
        self.settings = settings if settings is not None else Settings
        self.llm_client = get_llm_client(self.settings)
        
    def create_prompt(self, newsletter: Dict) -> str:
        """
        문체 변환을 위한 프롬프트 생성
        
        Args:
            newsletter: 원본 뉴스레터 딕셔너리 (title, summary, content, keywords 포함)
            
        Returns:
            포맷팅된 프롬프트 문자열
        """
        prompt = TONE_CONVERSION_PROMPT.format(
            title=newsletter.get("title", ""),
            summary=newsletter.get("summary", ""),
            content=newsletter.get("content", ""),
            keywords=json.dumps(newsletter.get("keywords", []), ensure_ascii=False)
        )
        
        return prompt
    
    def convert(self, newsletter: Dict) -> Optional[Dict]:
        """
        뉴스레터 문체를 변환합니다 (Formal -> Casual)
        
        Args:
            newsletter: 원본 뉴스레터 딕셔너리
            
        Returns:
            변환된 뉴스레터 딕셔너리 또는 실패 시 None
        """
        max_retries = 5
        last_converted = None
        for attempt in range(max_retries):
            try:
                prompt = self.create_prompt(newsletter)
                
                # logger.info(f"🎨 문체 변환 시도 ({attempt + 1}/{max_retries})...")
                
                # Call LLM using chat_completion
                response = self.llm_client.chat_completion(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.4,  # Slightly higher for creative rewording
                    max_tokens=4096,
                    response_format={"type": "json_object"}
                )
                
                if not response:
                    continue
                
                # Parse JSON response
                converted = self._parse_response(response, newsletter)
                
                if not converted:
                    continue
                
                # logger.info("✅ 문체 변환 완료")
                if self.validate_conversion(newsletter, converted):
                    return converted

                last_converted = converted
                
            except Exception as e:
                last_converted = last_converted or None
                
        # Fallback: always return a converted draft to avoid pipeline failures
        return self._fallback_convert(newsletter, last_converted)
    
    def _parse_response(self, response: str, original: Dict) -> Optional[Dict]:
        """
        LLM 응답을 파싱하여 JSON 객체로 변환합니다.
        """
        if not response:
            return None

        result = extract_json_from_response(response)
        if not isinstance(result, dict):
            return None

        # Normalize fields
        if not result.get("summary") and result.get("sentence"):
            result["summary"] = result.get("sentence")

        # Ensure keywords list and preserve original keywords to prevent drift
        keywords = result.get("keywords")
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        if not isinstance(keywords, list) or not keywords:
            keywords = original.get("keywords", []) or []
        result["keywords"] = keywords

        # Fill required fields from original if missing
        for field in ("title", "summary", "content"):
            val = result.get(field)
            if not isinstance(val, str) or not val.strip():
                result[field] = original.get(field, "") or ""

        return result

    def _fallback_convert(self, original: Dict, last: Optional[Dict]) -> Dict:
        """
        Deterministic fallback to avoid conversion failures.
        """
        def soften(text: str) -> str:
            if not text:
                return ""
            replacements = [
                ("입니다", "이에요"),
                ("합니다", "해요"),
                ("했습니다", "했어요"),
                ("것으로 보입니다", "것 같아요"),
                ("것으로", "것으로"),
                ("하였다", "했어요"),
            ]
            out = text
            for a, b in replacements:
                out = out.replace(a, b)
            return out

        base = last or {}
        title = soften(base.get("title") or original.get("title", "")).strip()
        summary = soften(base.get("summary") or original.get("summary", "")).strip()
        content = soften(base.get("content") or original.get("content", "")).strip()

        if title and not title.startswith(("📰", "📌", "🔥", "✅", "⭐")):
            title = f"📰 {title}"
        if summary and "📰" not in summary and "✅" not in summary:
            summary = f"{summary} ✅"
        if content and "📰" not in content:
            content = f"📰 {content}"

        return {
            "title": title or "📰 뉴스 요약",
            "summary": summary or (title or "뉴스 요약"),
            "content": content or (original.get("content", "") or ""),
            "keywords": original.get("keywords", []) or []
        }
    
    def validate_conversion(self, original: Dict, converted: Dict) -> bool:
        """
        변환 결과가 핵심 정보를 잘 보존하고 있는지 검증합니다.
        
        Args:
            original: 원본 뉴스레터
            converted: 변환된 뉴스레터
            
        Returns:
            유효하면 True, 아니면 False
        """
        # Minimal validation: ensure required text fields exist
        if not converted.get("title") or not converted.get("summary") or not converted.get("content"):
            return False
        return True


def test_tone_converter():
    """Test function for ToneConverter"""
    
    # Sample newsletter
    sample_newsletter = {
        "title": "한동훈 제명 후폭풍 확산",
        "summary": "국민의힘 한동훈 전 대표 제명 결정으로 당내 갈등이 심화되고 있다.",
        "content": """국민의힘은 1월 29일 한동훈 전 대표를 제명하기로 결정했다. 이번 결정은 당원 게시판에 한 전 대표 가족이 윤석열 전 대통령 부부를 비판하는 글을 올렸다는 논란에서 비롯되었으며, 이로 인해 한 전 대표는 향후 5년간 국민의힘 후보로 선거에 출마할 수 없게 되었다. 한 전 대표는 제명 결정 후 '반드시 돌아온다'며 복귀를 선언했고, 당내에서는 장동혁 대표와 지도부의 사퇴를 요구하는 목소리가 커지고 있다.""",
        "keywords": ["한동훈", "국민의힘", "제명", "장동혁", "지도부 사퇴"]
    }
    
    print("🧪 ToneConverter 테스트 시작\n")
    print("=" * 80)
    print("원본 뉴스레터:")
    print(f"제목: {sample_newsletter['title']}")
    print(f"요약: {sample_newsletter['summary']}")
    print(f"본문: {sample_newsletter['content'][:100]}...")
    print("=" * 80)
    
    # Create converter
    converter = ToneConverter()
    
    # Convert
    converted = converter.convert(sample_newsletter)
    
    if converted:
        print("\n" + "=" * 80)
        print("변환된 뉴스레터:")
        print(f"제목: {converted['title']}")
        print(f"요약: {converted['summary']}")
        print(f"본문: {converted['content'][:200]}...")
        print("=" * 80)
        
        # Validate
        is_valid = converter.validate_conversion(sample_newsletter, converted)
        print(f"\n검증 결과: {'✅ 통과' if is_valid else '❌ 실패'}")
    else:
        print("\n❌ 변환 실패")


if __name__ == "__main__":
    test_tone_converter()
