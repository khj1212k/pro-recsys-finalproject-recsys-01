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
#
# 매일경제: 기사 페이지에 매일경제가 붙이는 해설 모달(div.modal_ai_explain > div.ai_explain_wrap > div.ai_explain_body >
# div.content_section 안의 ul.timeline_list)이 있다. 모달은 기사 본문 노드 밖(오른쪽 단 div.g-3col)에 있고 글이 본문보다 몇 배
# 길다(2026-10-25 측정: 본문 1,000자 안팎, 모달 5,000자 안팎). 페이지 전체를 넘기면 trafilatura가 모달 글을 본문에 섞거나(길이가
# 3~4배) 본문 대신 모달만 뽑는다. 같은 페이지도 시간이 지나면 모달을 갖는 것으로 보여, 10-24에 저장한 기사도 지금 다시 받으면
# 부푼다. 본문 노드(//*[@itemprop="articleBody"] = div.news_cnt_detail_wrap)만 넘기면 모달과 head 안 글이 빠진다.
from typing import Dict, NamedTuple

import lxml.html
from trafilatura.utils import load_html

FULL_PAGE = "full_page"  # 규칙이 없는 언론사 - 페이지 전체를 그대로 넘긴다(예전과 같다)
CONTAINER_FOUND = "container"  # 본문 컨테이너만 잘라 넘겼다
CONTAINER_MISSING = "container_missing"  # 규칙이 있는데 컨테이너를 못 찾아 전체 페이지로 되돌아갔다(페이지 개편 신호)

# 언론사 -> 본문 컨테이너를 고르는 XPath. 문서 순서로 첫 번째 노드를 쓴다.
PRESS_BODY_CONTAINERS: Dict[str, str] = {
    "국민일보": '//*[@id="articleBody"]',
    "매일경제": '//*[@itemprop="articleBody"]',
}


class BodySelection(NamedTuple):
    html: str  # trafilatura에 넘길 HTML
    mode: str  # FULL_PAGE | CONTAINER_FOUND | CONTAINER_MISSING


def select_body_html(html: str, press_name: str) -> BodySelection:
    """trafilatura에 넘길 HTML을 고른다. 예외를 던지지 않는다 - 파싱이 안 되는 페이지도 전체 페이지로 되돌아간다."""
    xpath = PRESS_BODY_CONTAINERS.get(press_name)
    if xpath is None:
        return BodySelection(html, FULL_PAGE)
    try:
        # trafilatura가 페이지를 읽는 방식 그대로 읽는다. lxml.html.document_fromstring은 XML 선언(<?xml ... encoding=...?>)이
        # 앞에 붙은 str에서 ValueError를 내, 본문 컨테이너가 있는 페이지도 '못 찾음'으로 보고 전체 페이지 추출(= 특집 글)로
        # 되돌아가게 한다. load_html은 같은 입력을 읽고, 읽을 수 없는 입력(빈 문자열 등)에는 None을 돌려준다.
        tree = load_html(html)
        nodes = tree.xpath(xpath) if tree is not None else []
        if not nodes:
            return BodySelection(html, CONTAINER_MISSING)
        fragment = lxml.html.tostring(nodes[0], encoding="unicode")
    except Exception:  # noqa: BLE001 - 빈 문자열·깨진 마크업·str이 아닌 값: 컨테이너를 못 찾은 것과 같이 다룬다
        return BodySelection(html, CONTAINER_MISSING)
    return BodySelection(
        f'<html><head><meta charset="utf-8"></head><body><article>{fragment}</article></body></html>',
        CONTAINER_FOUND,
    )
