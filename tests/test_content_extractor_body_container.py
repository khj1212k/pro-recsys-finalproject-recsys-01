"""국민일보 본문 컨테이너 선택 (ADR 0026 결정 9, 2026-10-25 절).

2026-10-19 국민일보가 URL 형식과 페이지 템플릿을 바꿨다. 새 페이지는 기사 본문(#articleBody, 1,000자 안팎)보다 훨씬 긴
특집 카드 묶음(#kpick_swiper, 25,000자쯤)이 같이 있고, 전체 페이지를 넘긴 trafilatura가 후보를 견주다 특집 카드 묶음을
본문으로 뽑았다. 모든 기사가 같은 특집 글을 받아 본문 해시 중복 제거가 나머지를 duplicate로 지웠다.
글은 모두 tests/kmib_fake_pages.py가 구조만 흉내 내 지어낸 것이다(ADR 0023).
"""
from unittest.mock import MagicMock, patch

import pytest
import trafilatura

from crawler.content_extractor.body_container import (
    CONTAINER_FOUND,
    CONTAINER_MISSING,
    FULL_PAGE,
    select_body_html,
)
from crawler.content_extractor.extractor import ContentExtractor
from crawler.content_extractor.fetch import FetchOutcome
from tests.kmib_fake_pages import AD_MARK, BODY_MARK, FOOTER_MARK, KPICK_MARK, RECOMMEND_MARK, kmib_like_page


def _extract(page):
    return trafilatura.extract(page, include_comments=False, include_tables=False) or ""


def test_the_fake_page_reproduces_the_failure_when_the_whole_page_is_handed_to_trafilatura():
    """이 테스트가 깨지면 trafilatura가 바뀌어 지어낸 페이지가 더는 실패를 재현하지 못하는 것이다 - 아래 테스트들이
    '고쳤다'를 보여 주지 못하므로 fixture를 다시 맞춘다(docker/requirements-ingest.txt가 버전을 고정한다)."""
    text = _extract(kmib_like_page())
    assert KPICK_MARK in text and BODY_MARK not in text


def test_kmib_body_is_cut_from_the_article_container_so_the_feature_cards_cannot_win():
    selected = select_body_html(kmib_like_page(), "국민일보")

    assert selected.mode == CONTAINER_FOUND
    text = _extract(selected.html)
    assert text.count(BODY_MARK) == 8 * 2  # 본문 문단 8개 x 문장 2개 - 하나도 빠지지 않았다
    assert KPICK_MARK not in text and FOOTER_MARK not in text
    assert AD_MARK not in text and RECOMMEND_MARK not in text


def test_other_presses_get_the_page_unchanged():
    """규칙은 국민일보에만 있다. 다른 언론사는 예전과 같은 문자열(같은 객체)을 받는다."""
    page = kmib_like_page()
    for press in ("동아일보", "경향신문", "매일경제", "한국경제", "세계일보", "전자신문", "AI타임스", "", "국민일보 "):
        selected = select_body_html(page, press)
        assert selected.mode == FULL_PAGE and selected.html is page


def test_without_the_container_falls_back_to_the_whole_page_and_says_so():
    page = kmib_like_page(container_id="articleText")

    selected = select_body_html(page, "국민일보")

    assert selected.mode == CONTAINER_MISSING
    assert selected.html is page  # 지금까지와 같은 전체 페이지 추출로 되돌아간다


@pytest.mark.parametrize("page", ["", "   ", "<<<>>>", '<?xml version="1.0" encoding="utf-8"?><html></html>', "<html/>", None])
def test_unparsable_or_empty_pages_fall_back_without_raising(page):
    selected = select_body_html(page, "국민일보")
    assert selected.mode == CONTAINER_MISSING and selected.html is page


@pytest.mark.parametrize("prolog", [
    '<?xml version="1.0" encoding="utf-8"?>\n',
    '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
    '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n',
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
    '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n',
])
def test_a_page_with_an_xml_declaration_or_xhtml_doctype_still_finds_the_container(prolog):
    """XML 선언이 붙은 str을 lxml이 직접 읽으면 ValueError가 나 컨테이너가 있는데도 전체 페이지(= 특집 글)로 되돌아갔다.
    trafilatura가 읽는 방식 그대로 읽으므로 같은 입력에서 컨테이너를 찾는다."""
    selected = select_body_html(prolog + kmib_like_page(), "국민일보")

    assert selected.mode == CONTAINER_FOUND
    text = _extract(selected.html)
    assert text.count(BODY_MARK) == 8 * 2 and KPICK_MARK not in text


def test_only_the_first_container_is_used_when_the_id_is_repeated():
    first = kmib_like_page(seed=1)
    second_container = '<div id="articleBody"><p>' + "다른 컨테이너 문장입니다. " * 30 + "</p></div>"
    page = first.replace("</footer>", "</footer>" + second_container)

    text = _extract(select_body_html(page, "국민일보").html)

    assert BODY_MARK in text and "다른 컨테이너" not in text


# ----------------------------------------------------------------------------- 추출기 연결


def _run_single(page, press_name):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_page", return_value=FetchOutcome(page)):
        result = ContentExtractor.process_single_article((11, "http://example.com/kmib", press_name, "일반 제목"))
    return result, cur


def test_process_single_article_saves_the_article_body_for_kmib():
    result, cur = _run_single(kmib_like_page(), "국민일보")

    assert result.status == "ok" and result.container == CONTAINER_FOUND
    content, status, digest, length, raw_news_id = cur.execute.call_args[0][1]
    assert (status, raw_news_id) == ("ok", 11)
    assert BODY_MARK in content and KPICK_MARK not in content
    assert length == len(content) and digest


def test_kmib_pages_with_different_articles_no_longer_share_one_body_hash():
    """예전에는 기사가 달라도 모든 페이지가 같은 특집 글을 받아 본문 해시가 하나로 모였고, 해시 중복 제거(ADR 0006)가
    나머지를 duplicate로 지웠다. 특집 카드 묶음은 기사마다 같은 글이다(fixture의 kpick_seed가 같다)."""
    seeds = (1, 2, 3)
    pages = [kmib_like_page(seed=seed) for seed in seeds]
    assert len({_extract(page) for page in pages}) == 1  # 전제: 전체 페이지를 넘기면 기사가 달라도 글이 하나로 모인다

    digests = set()
    for page in pages:
        _, cur = _run_single(page, "국민일보")
        digests.add(cur.execute.call_args[0][1][2])
    assert len(digests) == len(seeds)


def test_process_single_article_reports_the_fallback_when_the_container_is_missing():
    result, cur = _run_single(kmib_like_page(container_id="articleText"), "국민일보")

    assert result.container == CONTAINER_MISSING
    # 되돌아간 전체 페이지 추출은 예전처럼 특집 글을 뽑는다 - 그래서 결과에 남기고 경고한다(jobs/tasks/ingest.py)
    assert KPICK_MARK in cur.execute.call_args[0][1][0]


def test_process_single_article_leaves_other_presses_alone():
    result, cur = _run_single(kmib_like_page(), "세계일보")

    assert result.status == "ok" and result.container == ""  # 규칙이 없는 언론사는 비어 있다
    assert KPICK_MARK in cur.execute.call_args[0][1][0]  # 예전과 같다 - 이 변경은 국민일보 밖을 바꾸지 않는다


def test_empty_container_is_empty_status_not_a_fallback_to_the_feature_cards():
    """본문 노드는 있는데 글이 없는 페이지(사진·영상 기사)는 '내용 없음'이다. 전체 페이지로 되돌아가면 특집 글을
    본문으로 저장하게 된다."""
    page = kmib_like_page(body_paragraphs=0)

    result, cur = _run_single(page, "국민일보")

    assert result.container == CONTAINER_FOUND
    assert result.status in ("empty", "dropped")
    assert KPICK_MARK not in cur.execute.call_args[0][1][0]


def test_stats_count_the_extraction_path_per_press_that_has_a_container_rule():
    from crawler.content_extractor.extractor import ExtractionResult
    from tests.test_content_extractor_wiring import _extract

    articles = [(1, "u1", "국민일보", "t"), (2, "u2", "국민일보", "t"), (3, "u3", "국민일보", "t"), (4, "u4", "동아일보", "t")]
    results = {
        1: ExtractionResult(1, "국민일보", "ok", container=CONTAINER_FOUND),
        2: ExtractionResult(2, "국민일보", "dropped", ("too_short<350",), container=CONTAINER_FOUND),
        3: ExtractionResult(3, "국민일보", "ok", container=CONTAINER_MISSING),
        4: ExtractionResult(4, "동아일보", "ok"),
    }

    stats, _, _ = _extract(articles, results, num_workers=1)

    # 규칙이 없는 언론사(동아일보)와 내려받지 못한 기사는 세지 않는다
    assert stats["body_container"] == {"국민일보": {CONTAINER_FOUND: 2, CONTAINER_MISSING: 1}}


def test_stats_have_no_container_counts_when_no_press_has_a_rule_in_the_run():
    from crawler.content_extractor.extractor import ExtractionResult
    from tests.test_content_extractor_wiring import _extract

    stats, _, _ = _extract([(1, "u1", "동아일보", "t")], {1: ExtractionResult(1, "동아일보", "ok")}, num_workers=1)

    assert stats["body_container"] == {}
