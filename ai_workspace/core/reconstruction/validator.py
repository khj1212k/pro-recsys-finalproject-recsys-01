# 뉴스레터 검증/정제 유틸
# - 본문 정리, 메타데이터 검증, 텍스트 정규화

import re
from typing import Dict, Optional

def cleanup_content_text(text: str) -> str:
    """본문에서 코드블록 등 불필요한 형식 제거"""
    if not text:
        return text

    t = text.strip()

    # 코드펜스 제거
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)

    # 모델이 'content:' 라벨을 달고 오면 제거
    t = re.sub(r"^\s*content\s*:\s*", "", t, flags=re.IGNORECASE)

    return t.strip()


def normalize_meta(result: Dict) -> Optional[Dict]:
    """메타데이터 최소 검증/정리"""
    # title
    title = (result.get("title") or "").strip()
    if not title:
        content = (result.get("content") or "").strip()
        title = content.split("\n")[0].strip() if content else "뉴스 요약"
    result["title"] = title

    # sentence
    sentence = (result.get("sentence") or "").strip()
    if not sentence:
        sentence = title
    result["sentence"] = sentence

    # keywords
    kws = result.get("keywords") or []
    if not isinstance(kws, list):
        kws = []
    kws = [str(k).strip() for k in kws if str(k).strip()]
    if len(kws) < 5:
        tokens = re.findall(r"[A-Za-z0-9가-힣]{2,}", f"{title} {sentence}")
        for t in tokens:
            if t not in kws:
                kws.append(t)
            if len(kws) == 5:
                break
    if len(kws) > 5:
        kws = kws[:5]
    result["keywords"] = kws

    # categories
    allowed = {"정치", "경제", "사회", "세계", "IT/과학", "생활/문화", "스포츠"}
    cats = result.get("categories") or []
    if not isinstance(cats, list):
        cats = []
    cats = [str(c).strip() for c in cats if str(c).strip()]
    cats = [c for c in cats if c in allowed][:2]
    if not cats:
        cats = ["사회"]
    result["categories"] = cats

    return result


def sanitize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace('\x00', '')
    text = text.encode('utf-8', errors='ignore').decode('utf-8')
    text = text.encode('utf-8', errors='surrogatepass').decode('utf-8', errors='ignore')
    return text
