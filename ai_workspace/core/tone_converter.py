# 문체 변환기
# - 딱딱한 뉴스 문체를 부드러운 대화체로 변환
# - 이모지 추가

from typing import Dict, Optional
import json
import logging

from core.llm import LLMClient, get_client
from core.llm.schemas import ToneResult
from core.llm_client import extract_json_from_response

logger = logging.getLogger(__name__)


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
1) JSON 외의 다른 텍스트는 출력하지 마세요. 코드블록도 금지.
2) JSON 문자열 안의 줄바꿈은 반드시 \\n 으로 이스케이프하세요.
3) 따옴표(\")는 반드시 \\\\\" 으로 이스케이프하세요.
4) 규칙을 지키기 어려우면 빈 문자열로라도 JSON 형식을 먼저 지키세요."""


class ToneConverter:

    def __init__(self, llm_client: Optional[LLMClient] = None):
        # role="tone" - TONE_PROVIDER/TONE_MODEL로 프로바이더를 정한다 (docs/adr/0005)
        self.llm_client: LLMClient = llm_client or get_client("tone")

    def create_prompt(self, newsletter: Dict) -> str:
        prompt = TONE_CONVERSION_PROMPT.format(
            title=newsletter.get("title", ""),
            summary=newsletter.get("summary", ""),
            content=newsletter.get("content", ""),
            keywords=json.dumps(newsletter.get("keywords", []), ensure_ascii=False)
        )
        
        return prompt
    
    def convert(self, newsletter: Dict) -> Optional[Dict]:
        prompt = self.create_prompt(newsletter)

        # 재시도/백오프는 LLMClient.complete() 내부에서 처리한다 (core/llm/adapters.py)
        result = self.llm_client.complete(
            messages=[{"role": "user", "content": prompt}],
            schema=ToneResult,
            temperature=0.4,
            max_tokens=4096,
            purpose="tone_convert",
        )

        converted = None
        if result.parsed is not None:
            converted = self._normalize_parsed(result.parsed.model_dump(), newsletter)
        elif result.text:
            converted = self._parse_response(result.text, newsletter)

        if converted and self.validate_conversion(newsletter, converted):
            return converted

        return self._fallback_convert(newsletter, converted)

    def _normalize_parsed(self, result: Dict, original: Dict) -> Dict:
        """네이티브 구조화 출력으로 이미 필드가 채워져 있어도, keywords가 비어 있으면
        원본 키워드로 보강한다 (JSON 모드 폴백 경로의 _parse_response와 동일한 보정)."""
        keywords = result.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            keywords = original.get("keywords", []) or []
        result["keywords"] = keywords
        return result

    def _parse_response(self, response: str, original: Dict) -> Optional[Dict]:
        if not response:
            return None

        result = extract_json_from_response(response)
        if not isinstance(result, dict):
            return None

        if not result.get("summary") and result.get("sentence"):
            result["summary"] = result.get("sentence")

        keywords = result.get("keywords")
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",") if k.strip()]
        if not isinstance(keywords, list) or not keywords:
            keywords = original.get("keywords", []) or []
        result["keywords"] = keywords

        for field in ("title", "summary", "content"):
            val = result.get(field)
            if not isinstance(val, str) or not val.strip():
                result[field] = original.get(field, "") or ""

        return result

    def _fallback_convert(self, original: Dict, last: Optional[Dict]) -> Dict:
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
        if not converted.get("title") or not converted.get("summary") or not converted.get("content"):
            return False
        return True


def test_tone_converter():
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
