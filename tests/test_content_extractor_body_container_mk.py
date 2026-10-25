"""매일경제 본문 컨테이너 선택 (ADR 0026 결정 9, 2026-10-25 절 - 매일경제).

매일경제 기사 페이지에는 매일경제가 붙이는 해설 모달(div.modal_ai_explain)이 본문 노드(itemprop="articleBody") 밖에 있다. 모달 글이
본문보다 몇 배 길어, 페이지 전체를 trafilatura에 넘기면 본문 길이가 3~4배가 되거나 본문 대신 모달만 뽑힌다. 같은 페이지도 시간이
지나면 모달을 갖는 것으로 보여 10-24에 저장한 기사도 다시 받으면 부푼다.
글은 모두 tests/mk_fake_pages.py가 구조만 흉내 내 지어낸 것이다(ADR 0023).
"""
from unittest.mock import MagicMock, patch

import pytest
import trafilatura

from crawler.content_extractor.body_container import (
    CONTAINER_FOUND,
    CONTAINER_MISSING,
    FULL_PAGE,
    PRESS_BODY_CONTAINERS,
    select_body_html,
)
from crawler.content_extractor.extractor import ContentExtractor, ExtractionResult
from crawler.content_extractor.fetch import FetchOutcome
from tests.kmib_fake_pages import kmib_like_page
from tests.mk_fake_pages import BODY_MARK, FOOTER_MARK, MODAL_MARK, TAIL_MARK, mk_like_page

MK = "매일경제"


def _extract(page):
    return trafilatura.extract(page, include_comments=False, include_tables=False) or ""


def test_the_fake_page_reproduces_the_failure_when_the_whole_page_is_handed_to_trafilatura():
    """이 테스트가 깨지면 trafilatura가 바뀌어 지어낸 페이지가 더는 실패를 재현하지 못하는 것이다 - 아래 테스트들이
    '고쳤다'를 보여 주지 못하므로 fixture를 다시 맞춘다(docker/requirements-ingest.txt가 버전을 고정한다)."""
    text = _extract(mk_like_page())
    assert MODAL_MARK in text and BODY_MARK not in text
    assert len(text) > 3 * len(_extract(mk_like_page(with_modal=False)))  # 길이가 몇 배로 부푼다


def test_mk_body_is_cut_from_the_article_node_so_the_modal_cannot_leak_in():
    selected = select_body_html(mk_like_page(), MK)

    assert selected.mode == CONTAINER_FOUND
    text = _extract(selected.html)
    assert text.count(BODY_MARK) == 6 * 2  # 본문 문단 6개 x 문장 2개 - 하나도 빠지지 않았다
    assert MODAL_MARK not in text and TAIL_MARK not in text and FOOTER_MARK not in text


def test_the_extracted_body_is_the_same_whether_or_not_the_page_has_the_modal_yet():
    """같은 기사가 시간이 지나 모달을 갖게 돼도 컨테이너로 추출한 본문은 글자까지 같다 - 다시 받아도 본문 해시가 그대로다."""
    without = _extract(select_body_html(mk_like_page(with_modal=False), MK).html)
    with_modal = _extract(select_body_html(mk_like_page(with_modal=True), MK).html)
    other_modal = _extract(select_body_html(mk_like_page(modal_seed=5, modal_items=30), MK).html)

    assert without and without == with_modal == other_modal


def test_without_the_article_node_falls_back_to_the_whole_page_and_says_so():
    page = mk_like_page(container_attr='itemprop="description"')

    selected = select_body_html(page, MK)

    assert selected.mode == CONTAINER_MISSING
    assert selected.html is page  # 지금까지와 같은 전체 페이지 추출로 되돌아간다


def test_only_the_first_article_node_is_used_when_the_attribute_is_repeated():
    first = mk_like_page(seed=1, with_modal=False)
    second_node = '<div itemprop="articleBody"><p>' + "다른 컨테이너 문장입니다. " * 30 + "</p></div>"
    page = first.replace("</footer>", "</footer>" + second_node)

    text = _extract(select_body_html(page, MK).html)

    assert BODY_MARK in text and "다른 컨테이너" not in text


@pytest.mark.parametrize("page", ["", "   ", "<<<>>>", '<?xml version="1.0" encoding="utf-8"?><html></html>', "<html/>", None])
def test_unparsable_or_empty_pages_fall_back_without_raising(page):
    selected = select_body_html(page, MK)
    assert selected.mode == CONTAINER_MISSING and selected.html is page


def test_the_rule_belongs_to_mk_only():
    """매일경제 규칙은 매일경제에만 적용되고, 국민일보 규칙은 매일경제 페이지에 적용되지 않는다."""
    page = mk_like_page()
    for press in ("동아일보", "경향신문", "한국경제", "세계일보", "전자신문", "AI타임스", "", "매일경제 "):
        selected = select_body_html(page, press)
        assert selected.mode == FULL_PAGE and selected.html is page, press

    assert select_body_html(page, "국민일보").mode == CONTAINER_MISSING  # 국민일보 규칙(#articleBody)은 이 페이지에 없다
    assert select_body_html(kmib_like_page(), MK).mode == CONTAINER_MISSING  # 반대로도 마찬가지
    assert set(PRESS_BODY_CONTAINERS) == {"국민일보", MK}  # 규칙이 있는 언론사가 늘면 이 목록과 ADR 0026을 함께 고친다


# ----------------------------------------------------------------------------- 추출기 연결


def _run_single(page, press_name=MK):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    with patch("crawler.content_extractor.extractor.get_connection", return_value=conn), \
         patch("crawler.content_extractor.extractor.release_connection"), \
         patch("crawler.content_extractor.extractor.fetch_page", return_value=FetchOutcome(page)):
        result = ContentExtractor.process_single_article((21, "http://example.com/mk", press_name, "일반 제목"))
    return result, cur


def test_process_single_article_saves_the_article_body_for_mk():
    result, cur = _run_single(mk_like_page())

    assert result.status == "ok" and result.container == CONTAINER_FOUND
    content, status, digest, length, raw_news_id = cur.execute.call_args[0][1]
    assert (status, raw_news_id) == ("ok", 21)
    assert BODY_MARK in content and MODAL_MARK not in content
    assert length == len(content) and digest


def test_the_stored_body_does_not_grow_when_the_modal_appears_on_a_refetched_page():
    """10-24에 저장한 기사(모달 없음)를 다시 받았더니 모달이 생긴 경우 - 저장되는 본문과 해시가 같다."""
    _, cur_before = _run_single(mk_like_page(with_modal=False))
    _, cur_after = _run_single(mk_like_page(with_modal=True))

    assert cur_before.execute.call_args[0][1][:4] == cur_after.execute.call_args[0][1][:4]  # 본문·상태·해시·길이


def test_process_single_article_reports_the_fallback_when_the_article_node_is_missing():
    result, cur = _run_single(mk_like_page(container_attr='itemprop="description"'))

    assert result.container == CONTAINER_MISSING
    # 되돌아간 전체 페이지 추출은 예전처럼 모달 글을 뽑는다 - 그래서 결과에 남기고 경고한다(jobs/tasks/ingest.py)
    assert MODAL_MARK in cur.execute.call_args[0][1][0]


def test_process_single_article_leaves_other_presses_alone():
    result, cur = _run_single(mk_like_page(), "세계일보")

    assert result.status == "ok" and result.container == ""  # 규칙이 없는 언론사는 비어 있다
    assert MODAL_MARK in cur.execute.call_args[0][1][0]  # 예전과 같다 - 이 변경은 매일경제 밖을 바꾸지 않는다


def test_empty_article_node_is_empty_status_not_a_fallback_to_the_modal():
    """본문 노드는 있는데 글이 없는 페이지(사진·영상 기사)는 '내용 없음'이다. 전체 페이지로 되돌아가면 모달 글을 본문으로
    저장하게 된다."""
    result, cur = _run_single(mk_like_page(body_paragraphs=0))

    assert result.container == CONTAINER_FOUND
    assert result.status in ("empty", "dropped")
    assert MODAL_MARK not in cur.execute.call_args[0][1][0]


def test_stats_count_the_extraction_path_for_mk_next_to_kmib():
    from tests.test_content_extractor_wiring import _extract as run_extract

    articles = [(1, "u1", MK, "t"), (2, "u2", MK, "t"), (3, "u3", MK, "t"), (4, "u4", "국민일보", "t"), (5, "u5", "동아일보", "t")]
    results = {
        1: ExtractionResult(1, MK, "ok", container=CONTAINER_FOUND),
        2: ExtractionResult(2, MK, "ok", container=CONTAINER_MISSING),
        3: ExtractionResult(3, MK, "fetch_failed"),  # 내려받지 못한 기사는 세지 않는다
        4: ExtractionResult(4, "국민일보", "ok", container=CONTAINER_FOUND),
        5: ExtractionResult(5, "동아일보", "ok"),
    }

    stats, _, _ = run_extract(articles, results, num_workers=1)

    assert stats["body_container"] == {MK: {CONTAINER_FOUND: 1, CONTAINER_MISSING: 1}, "국민일보": {CONTAINER_FOUND: 1}}


def test_ingest_warns_when_the_mk_article_node_is_missing(monkeypatch):
    """페이지가 개편돼 매일경제 본문 노드를 못 찾으면 ingest가 언론사 이름과 함께 경고한다(국민일보와 같은 경로)."""
    from tests.test_jobs_tasks import _extract_stats, _ingest_extract_only

    stats = _extract_stats(
        {MK: {"ok": 9}, "국민일보": {"ok": 4}},
        body_container={MK: {CONTAINER_FOUND: 6, CONTAINER_MISSING: 3}, "국민일보": {CONTAINER_FOUND: 4}},
    )
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)

    assert ctx.warnings == [f"{MK}: 본문 컨테이너를 못 찾아 전체 페이지로 추출한 기사 3건 (컨테이너로 추출 6건) - 페이지 개편 확인"]
