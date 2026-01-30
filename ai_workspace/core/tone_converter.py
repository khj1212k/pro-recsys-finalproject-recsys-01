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

from core.llm_client import get_llm_client
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

주의: JSON 외의 다른 텍스트는 출력하지 마세요."""


class ToneConverter:
    """
    Converts formal newsletter tone to casual, accessible tone with emojis
    """
    
    def __init__(self, settings: Optional[BaseSettings] = None):
        """
        Initialize ToneConverter
        
        Args:
            settings: Settings object (if None, will use default Settings)
        """
        self.settings = settings if settings is not None else Settings
        self.llm_client = get_llm_client(self.settings)
        
    def create_prompt(self, newsletter: Dict) -> str:
        """
        Create tone conversion prompt
        
        Args:
            newsletter: Original newsletter dict with title, summary, content, keywords
            
        Returns:
            Formatted prompt string
        """
        keywords_str = ", ".join(newsletter.get("keywords", []))
        
        prompt = TONE_CONVERSION_PROMPT.format(
            title=newsletter.get("title", ""),
            summary=newsletter.get("summary", ""),
            content=newsletter.get("content", ""),
            keywords=json.dumps(newsletter.get("keywords", []), ensure_ascii=False)
        )
        
        return prompt
    
    def convert(self, newsletter: Dict) -> Optional[Dict]:
        """
        Convert newsletter tone from formal to casual
        
        Args:
            newsletter: Original newsletter dict
            
        Returns:
            Converted newsletter dict or None if failed
        """
        try:
            prompt = self.create_prompt(newsletter)
            
            logger.info("🎨 문체 변환 시작...")
            
            # Call LLM using chat_completion
            response = self.llm_client.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,  # Slightly higher for creative rewording
                max_tokens=3000
            )
            
            if not response:
                logger.error("❌ LLM 응답 없음")
                return None
            
            # Parse JSON response
            converted = self._parse_response(response)
            
            if not converted:
                logger.error("❌ JSON 파싱 실패")
                return None
            
            logger.info("✅ 문체 변환 완료")
            return converted
            
        except Exception as e:
            logger.error(f"❌ 문체 변환 실패: {e}")
            return None
    
    def _parse_response(self, response: str) -> Optional[Dict]:
        """
        Parse LLM response and extract JSON
        
        Args:
            response: LLM response string
            
        Returns:
            Parsed dict or None if parsing failed
        """
        try:
            # Try direct JSON parsing
            result = json.loads(response)
            
            # Validate required fields
            required_fields = ["title", "summary", "content", "keywords"]
            if all(field in result for field in required_fields):
                return result
            else:
                logger.warning(f"⚠️ 필수 필드 누락: {result.keys()}")
                return None
                
        except json.JSONDecodeError:
            # Try to extract JSON from markdown code block
            try:
                if "```json" in response:
                    json_str = response.split("```json")[1].split("```")[0].strip()
                elif "```" in response:
                    json_str = response.split("```")[1].split("```")[0].strip()
                else:
                    # Try to find JSON pattern
                    start = response.find("{")
                    end = response.rfind("}") + 1
                    if start != -1 and end != 0:
                        json_str = response[start:end]
                    else:
                        return None
                
                result = json.loads(json_str)
                
                # Validate required fields
                required_fields = ["title", "summary", "content", "keywords"]
                if all(field in result for field in required_fields):
                    return result
                else:
                    logger.warning(f"⚠️ 필수 필드 누락: {result.keys()}")
                    return None
                    
            except Exception as e:
                logger.error(f"❌ JSON 추출 실패: {e}")
                return None
    
    def validate_conversion(self, original: Dict, converted: Dict) -> bool:
        """
        Validate that conversion preserved key information
        
        Args:
            original: Original newsletter
            converted: Converted newsletter
            
        Returns:
            True if valid, False otherwise
        """
        # Check that keywords are preserved
        if set(original.get("keywords", [])) != set(converted.get("keywords", [])):
            logger.warning("⚠️ 키워드가 변경되었습니다")
            return False
        
        # Check that converted text is not empty
        if not converted.get("title") or not converted.get("summary") or not converted.get("content"):
            logger.warning("⚠️ 변환된 텍스트가 비어있습니다")
            return False
        
        # Check that converted text is not too short (should be at least 50% of original)
        orig_len = len(original.get("content", ""))
        conv_len = len(converted.get("content", ""))
        
        if orig_len > 0 and conv_len < orig_len * 0.5:
            logger.warning(f"⚠️ 변환된 본문이 너무 짧습니다 (원본: {orig_len}, 변환: {conv_len})")
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
