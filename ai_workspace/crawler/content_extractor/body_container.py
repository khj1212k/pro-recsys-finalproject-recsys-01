# 언론사별 본문 컨테이너 선택 (ADR 0026 결정 9, 2026-10-25 절)
# - trafilatura에 페이지 전체를 넘기면 본문이 아닌 블록(특집 카드 묶음 등)을 본문으로 고르는 사이트가 있다.
#   그런 언론사는 본문 노드만 잘라 넘긴다. 컨테이너가 없는 페이지는 지금까지처럼 전체 페이지를 넘기고, 그 사실을
#   결과(ExtractionResult.container)에 남겨 ingest가 경고하게 한다.
# - 규칙이 없는 언론사는 아무것도 하지 않는다(파싱도 하지 않는다) - 예전 동작과 같다.
#
# 국민일보: 2026-10-19 URL 형식(/article/view.asp?arcid=... -> /article/<14자리>)과 템플릿이 함께 바뀌었다. 새 페이지는
# 기사 본문(div#articleBody, 1,000자 안팎)과 함께 기사와 상관없는 특집 카드 묶음(div#kpick_swiper, 25,000자쯤)을
# 담는다. trafilatura 2.2.0은 #articleBody를 본문 후보로 정확히 집지만(BODY_XPATH 첫 규칙), 그 결과가 짧다고 보고 폴백
# 알고리즘(readability·justext)의 후보와 견주다 더 긴 특집 카드 묶음을 고른다. 모든 기사가 같은 특집 글을 받아 본문
# 해시 하나로 모였다. #articleBody만 잘라 넘기면 폴백도 그 안에서만 후보를 찾는다.
from typing import Dict, NamedTuple

import lxml.html

FULL_PAGE = "full_page"  # 규칙이 없는 언론사 - 페이지 전체를 그대로 넘긴다(예전과 같다)
CONTAINER_FOUND = "container"  # 본문 컨테이너만 잘라 넘겼다
CONTAINER_MISSING = "container_missing"  # 규칙이 있는데 컨테이너를 못 찾아 전체 페이지로 되돌아갔다(페이지 개편 신호)

# 언론사 -> 본문 컨테이너를 고르는 XPath. 문서 순서로 첫 번째 노드를 쓴다.
PRESS_BODY_CONTAINERS: Dict[str, str] = {
    "국민일보": '//*[@id="articleBody"]',
}


class BodySelection(NamedTuple):
    html: str  # trafilatura에 넘길 HTML
    mode: str  # FULL_PAGE | CONTAINER_FOUND | CONTAINER_MISSING


def has_container_rule(press_name: str) -> bool:
    return press_name in PRESS_BODY_CONTAINERS


def select_body_html(html: str, press_name: str) -> BodySelection:
    """trafilatura에 넘길 HTML을 고른다. 예외를 던지지 않는다 - 파싱이 안 되는 페이지도 전체 페이지로 되돌아간다."""
    xpath = PRESS_BODY_CONTAINERS.get(press_name)
    if xpath is None:
        return BodySelection(html, FULL_PAGE)
    try:
        nodes = lxml.html.document_fromstring(html).xpath(xpath)
        if not nodes:
            return BodySelection(html, CONTAINER_MISSING)
        fragment = lxml.html.tostring(nodes[0], encoding="unicode")
    except Exception:  # noqa: BLE001 - 빈 문자열·인코딩 선언이 든 str·깨진 마크업: 컨테이너를 못 찾은 것과 같이 다룬다
        return BodySelection(html, CONTAINER_MISSING)
    return BodySelection(
        f'<html><head><meta charset="utf-8"></head><body><article>{fragment}</article></body></html>',
        CONTAINER_FOUND,
    )
