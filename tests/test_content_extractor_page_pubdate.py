"""기사 페이지의 발행 시각 읽기와 "미래 발행 시각" 판정 (ADR 0008 "2026-10-25 갱신 2").

국민일보 RSS가 2026-10-19부터 수집 시각보다 최대 24시간 뒤의 pubDate를 준다. 본문 추출기는 페이지를 받으므로, 저장된 발행 시각이
수집 시각보다 10분 넘게 뒤인 행만 페이지의 JSON-LD datePublished(없으면 meta article:published_time)로 바꾼다.
페이지는 모두 구조만 흉내 낸 지어낸 HTML이다(ADR 0023: 기사 본문·제목을 저장소에 넣지 않는다).
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler.content_extractor import page_pubdate as pp
from crawler.content_extractor.page_pubdate import (
    AFTER_CRAWL,
    CREATED_AT_FIXED,
    NO_PAGE_TIME,
    NO_TZ,
    NOT_FUTURE,
    TOO_OLD,
    decide_created_at,
    page_published_candidates,
)

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
CRAWLED = datetime(2026, 10, 25, 10, 5, 8)  # raw_news_crawled_at: 시간대 없는 UTC
STORED_FUTURE = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)  # RSS가 준 값: 수집보다 23시간 뒤


def ld_script(payload, attrs='type="application/ld+json"') -> str:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"<script {attrs}>{body}</script>"


def page(*head_parts: str, body: str = "<p>지어낸 본문 문장이다.</p>") -> str:
    return f"<html><head><title>시험 페이지</title>{''.join(head_parts)}</head><body>{body}</body></html>"


def news_article(published, **extra) -> dict:
    return {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "시험 제목", "datePublished": published,
            "dateModified": "2099-01-01T00:00:00+09:00", **extra}


# ----------------------------------------------------------------------------- 페이지에서 읽기


def test_reads_json_ld_date_published():
    html = page(ld_script(news_article("2026-10-25T18:50:00+09:00")))
    assert page_published_candidates(html) == ["2026-10-25T18:50:00+09:00"]


def test_modified_time_is_not_taken_for_the_published_time():
    html = page(ld_script({"@type": "NewsArticle", "dateModified": "2026-10-25T19:00:00+09:00"}))
    assert page_published_candidates(html) == []


@pytest.mark.parametrize("attrs", [
    'type="application/ld+json"',
    "type='application/ld+json' id=\"seo\"",
    'id="seo" data-x="1" type="Application/LD+JSON"',
    'type=application/ld+json',
])
def test_script_attribute_order_and_quoting_do_not_matter(attrs):
    html = page(ld_script(news_article("2026-10-25T18:50:00+09:00"), attrs))
    assert page_published_candidates(html) == ["2026-10-25T18:50:00+09:00"]


def test_other_scripts_are_ignored():
    html = page('<script type="text/javascript">var datePublished = "2000-01-01T00:00:00Z";</script>',
                '<script>{"datePublished": "2000-01-01T00:00:00Z"}</script>')
    assert page_published_candidates(html) == []


def test_graph_prefers_the_article_node_over_other_nodes_even_when_it_comes_later():
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "datePublished": "2026-01-01T00:00:00+09:00"},
        {"@type": "BreadcrumbList"},
        {"@type": ["Thing", "NewsArticle"], "datePublished": "2026-10-25T18:50:00+09:00"},
    ]}
    assert page_published_candidates(page(ld_script(graph))) == [
        "2026-10-25T18:50:00+09:00", "2026-01-01T00:00:00+09:00"]


def test_a_nested_related_article_loses_to_the_page_own_article():
    article = news_article("2026-10-25T18:50:00+09:00", isRelatedTo=[news_article("2026-10-20T08:00:00+09:00")])
    # 관련 기사가 문서에서 먼저 나와도(키 순서) 얕은 노드가 앞선다
    article = {"isRelatedTo": article.pop("isRelatedTo"), **article}
    assert page_published_candidates(page(ld_script(article)))[0] == "2026-10-25T18:50:00+09:00"


def test_top_level_array_and_several_blocks_keep_document_order():
    html = page(
        ld_script({"@type": "Organization", "name": "시험 신문사"}),
        ld_script([news_article("2026-10-25T18:50:00+09:00"), news_article("2026-10-24T08:00:00+09:00")]),
    )
    assert page_published_candidates(html) == ["2026-10-25T18:50:00+09:00", "2026-10-24T08:00:00+09:00"]


def test_json_ld_with_a_raw_newline_inside_a_string_is_still_read():
    broken = '{"@type": "NewsArticle", "description": "첫 줄\n둘째 줄", "datePublished": "2026-10-25T18:50:00+09:00"}'
    assert page_published_candidates(page(ld_script(broken))) == ["2026-10-25T18:50:00+09:00"]


def test_invalid_json_falls_back_to_the_value_pattern_and_never_raises():
    broken = '{"@type": "NewsArticle", "datePublished": "2026-10-25T18:50:00+09:00", "headline": "끊긴 JSON'
    assert page_published_candidates(page(ld_script(broken))) == ["2026-10-25T18:50:00+09:00"]
    assert page_published_candidates(page(ld_script("{{{"))) == []


def test_meta_published_time_is_the_fallback_after_json_ld():
    meta = '<meta property="article:published_time" content="2026-10-25T18:49:00+09:00">'
    assert page_published_candidates(page(meta)) == ["2026-10-25T18:49:00+09:00"]
    both = page(meta, ld_script(news_article("2026-10-25T18:50:00+09:00")))
    assert page_published_candidates(both) == ["2026-10-25T18:50:00+09:00", "2026-10-25T18:49:00+09:00"]
    by_name = page('<meta content="2026-10-25T18:49:00+09:00" name="article:published_time"/>')
    assert page_published_candidates(by_name) == ["2026-10-25T18:49:00+09:00"]
    assert page_published_candidates(page('<meta property="article:modified_time" content="2026-10-25T19:00:00+09:00">')) == []


def test_html_entities_in_the_meta_content_are_decoded():
    html = page('<meta property="article:published_time" content="2026-10-25T18:49:00&#43;09:00">')
    assert page_published_candidates(html) == ["2026-10-25T18:49:00+09:00"]


def test_script_without_a_closing_tag_is_read_as_far_as_it_goes():
    html = '<html><head><script type="application/ld+json">' + json.dumps(news_article("2026-10-25T18:50:00+09:00"))
    assert page_published_candidates(html) == ["2026-10-25T18:50:00+09:00"]


@pytest.mark.parametrize("html", [None, "", "   ", "<<<>>>", b"bytes", 123, "<html/>"])
def test_unusable_pages_give_no_candidates(html):
    assert page_published_candidates(html) == []


def test_non_string_date_published_is_ignored():
    html = page(ld_script({"@type": "NewsArticle", "datePublished": 1761385800}))
    assert page_published_candidates(html) == []


# ----------------------------------------------------------------------------- 미래인 행만, 조건을 지킬 때만 바꾼다


def good_page(published="2026-10-25T18:50:00+09:00") -> str:
    return page(ld_script(news_article(published)))


def test_a_future_row_takes_the_page_time_converted_to_utc():
    decision = decide_created_at(STORED_FUTURE, CRAWLED, good_page("2026-10-25T18:50:00+09:00"))
    assert decision == pp.PageCreatedAt(CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))
    assert decision.value.tzinfo is UTC


def test_rows_that_are_not_in_the_future_are_never_touched_and_the_page_is_not_even_read(monkeypatch):
    """정상 피드의 값을 페이지 값으로 덮지 않는다. 미래가 아닌 행은 페이지를 읽지도 않는다."""
    reads = []
    monkeypatch.setattr(pp, "page_published_candidates", lambda html: reads.append(html) or ["2026-10-25T18:50:00+09:00"])
    for stored in (
        datetime(2026, 10, 25, 9, 0, tzinfo=UTC),                          # 수집 1시간 전(정상)
        datetime(2026, 10, 25, 10, 5, 8, tzinfo=UTC),                      # 수집과 같은 순간
        datetime(2026, 10, 25, 10, 15, 8, tzinfo=UTC),                     # 정확히 +10분 - 아직 미래가 아니다
        datetime(2026, 10, 25, 19, 14, tzinfo=KST),                        # 같은 순간(10:14Z)을 KST로 적은 값
    ):
        assert decide_created_at(stored, CRAWLED, good_page()) == pp.PageCreatedAt(NOT_FUTURE), stored
    assert reads == []


def test_ten_minutes_and_one_second_ahead_is_the_first_future_value():
    stored = datetime(2026, 10, 25, 10, 15, 9, tzinfo=UTC)
    assert decide_created_at(stored, CRAWLED, good_page()).outcome == CREATED_AT_FIXED


def test_future_is_judged_in_absolute_time_whatever_the_offset_of_the_stored_value():
    stored = datetime(2026, 10, 26, 18, 0, tzinfo=KST)  # 10-26 09:00Z: 수집보다 약 23시간 뒤
    assert decide_created_at(stored, CRAWLED, good_page()).outcome == CREATED_AT_FIXED


@pytest.mark.parametrize("stored, crawled", [(None, CRAWLED), (STORED_FUTURE, None), (None, None)])
def test_missing_stored_or_crawl_time_is_left_alone(stored, crawled):
    assert decide_created_at(stored, crawled, good_page()) == pp.PageCreatedAt(NOT_FUTURE)


@pytest.mark.parametrize("html", [None, "", "<html></html>", page(ld_script(news_article("어제 오후"))),
                                  page(ld_script(news_article("2026-13-45T25:61:00+09:00"))),
                                  page(ld_script(news_article(""))), page('<meta property="article:published_time" content="">')])
def test_future_row_without_a_usable_page_time_is_reported_not_changed(html):
    decision = decide_created_at(STORED_FUTURE, CRAWLED, html)
    assert decision == pp.PageCreatedAt(NO_PAGE_TIME)


def test_page_time_without_a_time_zone_is_not_used():
    for published in ("2026-10-25T18:50:00", "2026-10-25 18:50:00", "2026-10-25"):
        assert decide_created_at(STORED_FUTURE, CRAWLED, good_page(published)) == pp.PageCreatedAt(NO_TZ), published


def test_a_meta_time_with_a_zone_is_used_when_the_json_ld_value_has_none():
    html = page(ld_script(news_article("2026-10-25T18:50:00")),
                '<meta property="article:published_time" content="2026-10-25T18:49:00+09:00">')
    decision = decide_created_at(STORED_FUTURE, CRAWLED, html)
    assert decision == pp.PageCreatedAt(CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 49, tzinfo=UTC))


def test_meta_is_used_when_there_is_no_json_ld():
    html = page('<meta property="article:published_time" content="2026-10-25T09:40:00Z">')
    assert decide_created_at(STORED_FUTURE, CRAWLED, html) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 40, tzinfo=UTC))


def test_page_time_after_the_crawl_is_not_used():
    # 수집 10:05:08Z + 10분 = 10:15:08Z. 그 뒤의 페이지 값은 같은 이상일 수 있어 쓰지 않는다.
    assert decide_created_at(STORED_FUTURE, CRAWLED, good_page("2026-10-25T19:15:09+09:00")) == pp.PageCreatedAt(AFTER_CRAWL)
    assert decide_created_at(STORED_FUTURE, CRAWLED, good_page("2026-10-25T19:15:08+09:00")).outcome == CREATED_AT_FIXED  # 경계는 쓴다


def test_page_time_more_than_seven_days_before_the_crawl_is_not_used():
    # 수집 10:05:08Z - 7일 = 10-18 10:05:08Z
    assert decide_created_at(STORED_FUTURE, CRAWLED, good_page("2026-10-18T19:05:08+09:00")).outcome == CREATED_AT_FIXED  # 경계는 쓴다
    assert decide_created_at(STORED_FUTURE, CRAWLED, good_page("2026-10-18T19:05:07+09:00")) == pp.PageCreatedAt(TOO_OLD)


def test_stored_time_may_already_carry_a_naive_value():
    # 대역이 시간대 없는 값을 주면 UTC로 읽는다(psycopg2는 timestamptz에 시간대 있는 값을 준다)
    decision = decide_created_at(datetime(2026, 10, 26, 9, 0), CRAWLED, good_page())
    assert decision.outcome == CREATED_AT_FIXED


def test_a_failure_while_reading_never_escapes(monkeypatch):
    def boom(html):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(pp, "page_published_candidates", boom)
    assert decide_created_at(STORED_FUTURE, CRAWLED, good_page()) == pp.PageCreatedAt(NO_PAGE_TIME)


def test_only_the_first_two_million_characters_are_read():
    late = "x" * (pp._MAX_HTML_CHARS + 10) + good_page()
    assert page_published_candidates(late) == []


# ----------------------------------------------------------------------------- 일회성 보정 생성기와 같은 조건


def test_thresholds_match_the_one_off_fix_generator():
    """scripts/oci/build_kmib_created_at_fix.py는 저장소 밖 의존성 없이 돌려서 상수를 복사해 갖는다 - 어긋나면 수집기와 2차 보정이
    같은 행을 다르게 판정한다."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "oci" / "build_kmib_created_at_fix.py"
    spec = importlib.util.spec_from_file_location("build_kmib_created_at_fix_for_parity", path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    assert gen.SLACK_AFTER_CRAWL == pp.FUTURE_SLACK
    assert gen.MAX_BEFORE_CRAWL == pp.MAX_AGE_BEFORE_CRAWL
    assert pp.FUTURE_SLACK == timedelta(minutes=pp.FUTURE_SLACK_MINUTES)
