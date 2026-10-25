"""기사 페이지의 발행 시각 읽기와 "미래 발행 시각" 판정 (ADR 0008 "2026-10-25 갱신 2").

국민일보 RSS가 2026-10-19부터 수집 시각보다 최대 24시간 뒤의 pubDate를 준다. 본문 추출기는 페이지를 받으므로, 저장된 발행 시각이
수집 시각보다 10분 넘게 뒤인 행만 페이지의 JSON-LD datePublished(없으면 meta article:published_time)로 바꾼다. 쓰는 값은 그 기사
자신의 값뿐이다 - JSON-LD는 최상위 기사 노드의 문자열 datePublished만 받고, 중첩 노드·기사가 아닌 노드의 값은 읽지 않는다. meta는 <head>의 것만 본다.
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
    CONFLICT,
    CREATED_AT_FIXED,
    NO_PAGE_TIME,
    NO_TZ,
    NOT_FUTURE,
    TOO_OLD,
    CreatedAtChange,
    PageValues,
    decide_created_at,
    read_page_values,
    unfixed_if_future,
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

OWN = "2026-10-25T18:50:00+09:00"      # 그 기사 자신의 발행 시각
OTHER = "2026-10-23T08:00:00+09:00"    # 같은 페이지에 있는 다른 것(관련 기사 등)의 발행 시각
META = f'<meta property="article:published_time" content="{OWN}">'


def json_ld_of(html: str):
    return read_page_values(html).json_ld


def related_list(published) -> dict:
    """관련 기사 목록: ItemList > ListItem > item(NewsArticle). 페이지 자신의 기사가 아니다."""
    return {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "item": {"@type": "NewsArticle", "datePublished": published}}]}


def test_reads_json_ld_date_published():
    assert read_page_values(page(ld_script(news_article(OWN)))) == PageValues(OWN, ())


def test_modified_time_is_not_taken_for_the_published_time():
    html = page(ld_script({"@type": "NewsArticle", "dateModified": "2026-10-25T19:00:00+09:00"}))
    assert read_page_values(html) == PageValues(None, ())


@pytest.mark.parametrize("attrs", [
    'type="application/ld+json"',
    "type='application/ld+json' id=\"seo\"",
    'id="seo" data-x="1" type="Application/LD+JSON"',
    'type=application/ld+json',
])
def test_script_attribute_order_and_quoting_do_not_matter(attrs):
    assert json_ld_of(page(ld_script(news_article(OWN), attrs))) == OWN


def test_other_scripts_are_ignored():
    html = page('<script type="text/javascript">var datePublished = "2000-01-01T00:00:00Z";</script>',
                '<script>{"datePublished": "2000-01-01T00:00:00Z"}</script>')
    assert read_page_values(html) == PageValues(None, ())


@pytest.mark.parametrize("kind", [
    "NewsArticle", "Article", "BlogPosting", "LiveBlogPosting", "SocialMediaPosting", "DiscussionForumPosting", "ReportageNewsArticle",
    "http://schema.org/NewsArticle", "https://schema.org/Article", "schema:NewsArticle",
    ["Thing", "NewsArticle"], ["http://schema.org/NewsArticle", "WebPage"],
])
def test_article_and_posting_types_in_all_their_spellings_are_article_nodes(kind):
    assert json_ld_of(page(ld_script({"@type": kind, "datePublished": OWN}))) == OWN


@pytest.mark.parametrize("kind", [None, "WebPage", "ItemList", "Organization", "ArticleBody", "BreadcrumbList", ["WebPage", "Thing"], 7,
                                  "JobPosting", "http://schema.org/JobPosting", ["Thing", "JobPosting"]])
def test_nodes_that_are_not_articles_give_no_value(kind):
    node = {"datePublished": OWN} if kind is None else {"@type": kind, "datePublished": OWN}
    assert json_ld_of(page(ld_script(node))) is None


def test_an_article_node_in_the_top_level_graph_is_used_and_other_graph_nodes_are_not():
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "datePublished": OTHER},
        {"@type": "BreadcrumbList"},
        {"@type": ["Thing", "NewsArticle"], "datePublished": OWN},
    ]}
    assert json_ld_of(page(ld_script(graph))) == OWN
    # 기사 노드가 @graph에 없으면 WebPage의 값을 쓰지 않는다
    assert json_ld_of(page(ld_script({"@graph": [{"@type": "WebPage", "datePublished": OTHER}]}))) is None
    # @graph가 객체 하나여도 같다
    assert json_ld_of(page(ld_script({"@graph": {"@type": "NewsArticle", "datePublished": OWN}}))) == OWN


def test_a_job_posting_before_the_article_does_not_decide_in_its_place():
    """JobPosting(구인 공고)은 Article이 아니다 - 첫 기사 노드로 잘못 잡히면 뒤의 진짜 기사 값을 못 읽는다."""
    job = {"@context": "https://schema.org", "@type": "JobPosting", "title": "시험 공고", "datePosted": OTHER}
    assert json_ld_of(page(ld_script(job), ld_script(news_article(OWN)))) == OWN
    assert json_ld_of(page(ld_script([job, news_article(OWN)]))) == OWN
    assert json_ld_of(page(ld_script(job))) is None


def test_an_article_node_in_a_top_level_array_is_used():
    html = page(ld_script([{"@type": "Organization", "name": "시험 신문사"}, news_article(OWN)]))
    assert json_ld_of(html) == OWN


def test_the_first_top_level_article_node_in_document_order_decides_across_blocks():
    """덩어리 번호·배열·@graph 순서로 첫 기사 노드 - 뒤의 기사 노드(같은 층의 다른 기사)는 보지 않는다."""
    html = page(
        ld_script({"@type": "Organization", "name": "시험 신문사"}),
        ld_script([news_article(OWN), news_article(OTHER)]),
        ld_script(news_article("2026-10-22T00:00:00+09:00")),
    )
    assert json_ld_of(html) == OWN
    in_graph_then_block = page(ld_script({"@graph": [news_article(OWN)]}), ld_script(news_article(OTHER)))
    assert json_ld_of(in_graph_then_block) == OWN
    root_then_graph = page(ld_script({**news_article(OWN), "@graph": [news_article(OTHER)]}))
    assert json_ld_of(root_then_graph) == OWN


@pytest.mark.parametrize("own_node", [
    {"@type": "NewsArticle", "headline": "시험 제목"},                                           # 키 없음
    {"@type": "NewsArticle", "datePublished": {"@value": OWN}},                              # JSON-LD 값 객체
    {"@type": "NewsArticle", "datePublished": 1761385800},                                   # 문자열이 아님
    {"@type": "NewsArticle", "datePublished": ""},
    {"@type": "NewsArticle", "datePublished": ["2026-10-25T18:50:00+09:00"]},
])
def test_an_own_article_without_a_string_date_gives_no_json_ld_value_even_when_nested_nodes_carry_dates(own_node):
    """기사 자신의 노드에 문자열 datePublished가 없으면 중첩된 관련 기사·ItemList·image의 값을 대신 쓰지 않는다(다른 기사의 시각이 된다)."""
    nested = {"isRelatedTo": [news_article(OTHER)], "mentions": related_list(OTHER),
              "image": {"@type": "ImageObject", "datePublished": OTHER}}
    assert json_ld_of(page(ld_script({**own_node, **nested}))) is None
    # 덩어리가 나뉘어 있어도 같다: 관련 기사 목록이 따로 있는 덩어리
    assert json_ld_of(page(ld_script(own_node), ld_script(related_list(OTHER)))) is None
    # 그리고 맞는 meta가 있으면 그 값이 쓰인다
    assert decide_created_at(STORED_FUTURE, CRAWLED, page(ld_script({**own_node, **nested}), META)) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))


def test_a_first_article_node_without_a_date_does_not_hand_over_to_the_next_article_node():
    html = page(ld_script([{"@type": "NewsArticle", "headline": "시험 제목"}, news_article(OTHER)]))
    assert json_ld_of(html) is None


def test_nested_nodes_are_never_read_even_when_there_is_no_other_value():
    nested_only = {"@type": "WebPage", "mainEntity": news_article(OTHER), "hasPart": [related_list(OTHER)]}
    assert json_ld_of(page(ld_script(nested_only))) is None
    assert json_ld_of(page(ld_script(related_list(OTHER)))) is None


def test_a_web_page_at_the_top_with_a_nested_article_gives_the_nested_article_no_priority():
    """WebPage 최상위 + 깊이 2의 NewsArticle: 깊이만 보던 예전 순위가 아니라 최상위 기사 노드만 받는다 - 둘 다 아니므로 값이 없다."""
    html = page(ld_script([{"@type": "WebPage", "datePublished": OWN}, related_list(OTHER)]))
    assert json_ld_of(html) is None
    assert decide_created_at(STORED_FUTURE, CRAWLED, html) == pp.PageCreatedAt(NO_PAGE_TIME)


def test_the_page_own_article_in_a_later_block_beats_a_related_list_in_an_earlier_block():
    html = page(ld_script(related_list(OTHER)), ld_script(news_article(OWN)))
    assert json_ld_of(html) == OWN
    assert decide_created_at(STORED_FUTURE, CRAWLED, html) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))


def test_a_nested_related_article_is_not_chosen_whatever_the_key_order():
    article = news_article(OWN, isRelatedTo=[news_article(OTHER)])
    article = {"isRelatedTo": article.pop("isRelatedTo"), **article}  # 관련 기사가 문서에서 먼저 나온다
    assert json_ld_of(page(ld_script(article))) == OWN


# 깨진 JSON-LD(m2): 정상으로 읽힌 기사 노드가 없을 때만, 모호하지 않을 때만 값을 정규식으로 읽는다
BROKEN_OWN = '{"@type": "NewsArticle", "datePublished": "%s", "headline": "끊긴 JSON' % OWN


def test_a_broken_block_alone_is_read_by_pattern_when_it_names_one_value_and_an_article_type():
    assert json_ld_of(page(ld_script(BROKEN_OWN))) == OWN
    assert json_ld_of(page(ld_script(BROKEN_OWN.replace('"NewsArticle"', '["Thing", "http://schema.org/BlogPosting"]')))) == OWN
    assert json_ld_of(page(ld_script("{{{"))) is None


def test_json_ld_with_a_raw_newline_inside_a_string_is_still_parsed():
    text = '{"@type": "NewsArticle", "description": "첫 줄\n둘째 줄", "datePublished": "%s"}' % OWN
    assert json_ld_of(page(ld_script(text))) == OWN


def test_a_broken_block_is_ignored_when_a_top_level_article_node_was_read_properly():
    related_broken = '{"@type":"ItemList","itemListElement":[{"@type":"NewsArticle","datePublished":"%s"},]}' % OTHER
    # 앞 덩어리가 깨진 관련 기사 목록이어도 뒤 덩어리의 정상 기사 노드가 쓰인다
    assert json_ld_of(page(ld_script(related_broken), ld_script(news_article(OWN)))) == OWN
    # 정상 기사 노드에 날짜가 없으면 깨진 덩어리의 값으로 넘어가지 않는다 - 깨진 덩어리가 앞에 있든 뒤에 있든
    no_date = {"@type": "NewsArticle", "headline": "시험 제목"}
    assert json_ld_of(page(ld_script(no_date), ld_script(BROKEN_OWN))) is None
    assert json_ld_of(page(ld_script(BROKEN_OWN), ld_script(no_date))) is None


# 국민일보 모양의 깨진 덩어리: @type NewsArticle, 문자열 image, datePublished, 그다음에 author·publisher의 중첩 @type
BROKEN_KMIB_SHAPE = ('{"@context": "https://schema.org", "@type": "NewsArticle", "headline": "끊긴 "제목" 줄", "image": ["https://example.test/a.jpg"], '
                     '"datePublished": "%s", "author": [{"@type": "Person", "name": "시험 기자"}], '
                     '"publisher": {"@type": "Organization", "logo": {"@type": "ImageObject", "url": "u"}}}' % OWN)


def test_a_broken_block_shaped_like_the_real_article_template_is_read():
    with pytest.raises(ValueError):  # JSON이 아니다 - 아래는 정규식 경로다
        json.loads(BROKEN_KMIB_SHAPE)
    assert json_ld_of(page(ld_script(BROKEN_KMIB_SHAPE))) == OWN


@pytest.mark.parametrize("broken", [
    # 첫 @type이 기사가 아니다: 안쪽 기사의 값
    '{"@context":"https://schema.org","@type":"ItemList","itemListElement":[{"@type":"ListItem","item":{"@type":"NewsArticle","datePublished":"%s"}},]}' % OTHER,
    # 기사 자신의 datePublished가 없고 중첩된 관련 기사만 있다
    '{"@type":"NewsArticle","headline":"끊긴 "제목" 줄","isRelatedTo":[{"@type":"NewsArticle","datePublished":"%s"}]}' % OTHER,
    # 기사 자신의 datePublished가 없고 image의 ImageObject에만 값이 있다
    '{"@type":"NewsArticle","headline":"끊긴 "제목" 줄","image":{"@type":"ImageObject","datePublished":"%s"}}' % OTHER,
    # 기사 노드의 @type이 둘째 @type 앞에 오지만 값은 그 뒤 중첩 노드에 있다
    '{"@type":["NewsArticle"],"headline":"끊긴 "제목" 줄","author":{"@type":"Person","datePublished":"%s"}}' % OTHER,
])
def test_a_broken_block_never_hands_out_the_date_of_a_nested_node(broken):
    assert json_ld_of(page(ld_script(broken))) is None
    assert decide_created_at(STORED_FUTURE, CRAWLED, page(ld_script(broken))) == pp.PageCreatedAt(NO_PAGE_TIME)
    # 맞는 meta가 있으면 그 값이 쓰인다
    assert decide_created_at(STORED_FUTURE, CRAWLED, page(ld_script(broken), META)) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))


def test_a_broken_block_whose_first_type_is_not_an_article_is_dropped_even_if_a_later_type_is():
    web_page_first = '{"@type": "WebPage", "datePublished": "%s", "mainEntity": {"@type": "NewsArticle"}, "headline": "끊' % OWN
    assert json_ld_of(page(ld_script(web_page_first))) is None
    job_first = '{"@type": "JobPosting", "datePublished": "%s", "headline": "끊' % OWN
    assert json_ld_of(page(ld_script(job_first))) is None


def test_the_type_name_list_of_a_broken_block_is_read_up_to_the_closing_bracket_only():
    # 둘째 @type은 첫 배열이 닫힌 뒤에 있다 - 값이 둘째 @type 앞이면 쓴다
    ok = '{"@type": ["Thing", "NewsArticle"], "datePublished": "%s", "author": {"@type": "Person"}, "headline": "끊' % OWN
    assert json_ld_of(page(ld_script(ok))) == OWN
    # 기사 이름이 첫 배열이 아니라 그 뒤 객체에만 있으면 기사 노드가 아니다
    late = '{"@type": ["Thing", "WebPage"], "datePublished": "%s", "author": {"@type": "NewsArticle"}, "headline": "끊' % OWN
    assert json_ld_of(page(ld_script(late))) is None


def test_a_type_name_list_is_read_only_within_the_first_few_hundred_characters():
    names = ", ".join('"x"' for _ in range(200))  # 800글자
    far = '{"@type": [%s, "NewsArticle"], "datePublished": "%s", "headline": "끊' % (names, OWN)
    near = '{"@type": ["NewsArticle", %s], "datePublished": "%s", "headline": "끊' % (names, OWN)
    assert json_ld_of(page(ld_script(far))) is None
    assert json_ld_of(page(ld_script(near))) == OWN


def test_a_block_full_of_unclosed_type_arrays_is_read_in_time_proportional_to_its_size():
    """닫히지 않은 "@type": [ 가 많으면 예전 정규식이 매번 끝까지 훑어 시간이 크기의 제곱으로 늘었다(450 KB에 5초 넘게)."""
    import time

    evil = '{"datePublished":"%s",' % OWN + '"@type":[' * 50_000
    started = time.perf_counter()
    values = read_page_values(page(ld_script(evil)))
    decision = decide_created_at(STORED_FUTURE, CRAWLED, page(ld_script(evil)))
    assert time.perf_counter() - started < 1.5
    assert values == PageValues(None, ()) and decision == pp.PageCreatedAt(NO_PAGE_TIME)
    # 이름 칸을 읽는 범위도 상한이 있다: 긴 한 줄짜리 닫히지 않은 배열
    long_array = '{"@type": ["NewsArticle", ' + ('"x", ' * 200_000) + '"datePublished": "%s"' % OWN
    started = time.perf_counter()
    read_page_values(page(ld_script(long_array)))
    assert time.perf_counter() - started < 1.5


def test_a_broken_block_with_several_values_or_without_an_article_type_is_dropped():
    two_values = '{"@type":"NewsArticle","isRelatedTo":[{"@type":"NewsArticle","datePublished":"%s"}],"datePublished":"%s","headline":"끊' % (OTHER, OWN)
    assert json_ld_of(page(ld_script(two_values))) is None
    same_twice = '{"@type":"NewsArticle","datePublished":"%s","x":{"datePublished":"%s"},"headline":"끊' % (OWN, OWN)
    assert json_ld_of(page(ld_script(same_twice))) == OWN  # 서로 다른 값이 하나뿐이다
    no_type = '{"datePublished": "%s", "headline": "끊긴 JSON' % OWN
    web_page = '{"@type": "WebPage", "datePublished": "%s", "headline": "끊긴 JSON' % OWN
    assert json_ld_of(page(ld_script(no_type))) is None
    assert json_ld_of(page(ld_script(web_page))) is None


def test_several_broken_blocks_with_values_are_ambiguous_and_dropped():
    other_broken = BROKEN_OWN.replace(OWN, OTHER)
    assert json_ld_of(page(ld_script(other_broken), ld_script(BROKEN_OWN))) is None
    # 값이 없는 깨진 덩어리는 세지 않는다
    assert json_ld_of(page(ld_script('{"@type": "Organization", "name": "끊'), ld_script(BROKEN_OWN))) == OWN


def test_a_broken_block_beside_properly_read_non_article_blocks_is_still_read():
    html = page(ld_script({"@type": "Organization", "name": "시험 신문사"}), ld_script(BROKEN_OWN))
    assert json_ld_of(html) == OWN


def test_json_ld_nested_so_deeply_that_the_parser_gives_up_never_raises_and_meta_is_still_read():
    deep = "[" * 200_000 + "]" * 200_000
    html = page(ld_script(deep), META)
    assert read_page_values(html) == PageValues(None, (OWN,))
    assert decide_created_at(STORED_FUTURE, CRAWLED, html) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))
    # 뒤 덩어리의 정상 기사 노드는 깊은 덩어리 때문에 버려지지 않는다
    assert json_ld_of(page(ld_script(deep), ld_script(news_article(OWN)))) == OWN


def test_meta_published_time_is_collected_in_document_order_beside_json_ld():
    assert read_page_values(page(META)) == PageValues(None, (OWN,))
    assert read_page_values(page(META, ld_script(news_article(OWN)))) == PageValues(OWN, (OWN,))
    by_name = page('<meta content="2026-10-25T18:49:00+09:00" name="article:published_time"/>')
    assert read_page_values(by_name) == PageValues(None, ("2026-10-25T18:49:00+09:00",))
    assert read_page_values(page('<meta property="article:modified_time" content="2026-10-25T19:00:00+09:00">')) == PageValues(None, ())


def test_only_the_meta_in_the_head_is_read_not_the_ones_in_the_body():
    """본문 안(관련 기사 카드 등)의 meta는 그 기사의 것이 아닐 수 있다 - 기사 자신의 JSON-LD 값이 없을 때 그 값이 쓰이면 안 된다."""
    card = f'<div class="related"><meta property="article:published_time" content="{OTHER}"><a>관련 기사</a></div>'
    assert read_page_values(page(body=card)) == PageValues(None, ())
    html = page(ld_script({"@type": "NewsArticle", "headline": "시험 제목"}), body=card)
    assert read_page_values(html) == PageValues(None, ())
    assert decide_created_at(STORED_FUTURE, CRAWLED, html) == pp.PageCreatedAt(NO_PAGE_TIME)
    # head의 meta는 그대로 읽고, body 뒤의 것만 버린다
    mixed = page(META, body=card)
    assert read_page_values(mixed) == PageValues(None, (OWN,))
    assert decide_created_at(STORED_FUTURE, CRAWLED, mixed) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))
    # <body> 속성이 붙어 있어도 같다
    attributed = f'<html><head>{META}</head><body class="x" id="y">{card}</body></html>'
    assert read_page_values(attributed) == PageValues(None, (OWN,))
    # JSON-LD는 본문 안에 있어도 읽는다(meta만 head로 좁혔다)
    in_body = f'<html><head></head><body>{ld_script(news_article(OWN))}</body></html>'
    assert read_page_values(in_body) == PageValues(OWN, ())


def test_a_page_without_head_and_body_tags_still_has_its_meta_read():
    assert read_page_values(f"{META}<p>지어낸 문장</p>") == PageValues(None, (OWN,))


def test_only_the_first_fifty_meta_values_are_collected():
    many = "".join(meta_tag(f"2026-10-25T18:{i % 60:02d}:00+09:00") for i in range(120))
    assert len(read_page_values(page(many)).meta) == pp._MAX_META_VALUES == 50


def test_html_entities_in_the_meta_content_are_decoded():
    html = page('<meta property="article:published_time" content="2026-10-25T18:49:00&#43;09:00">')
    assert read_page_values(html).meta == ("2026-10-25T18:49:00+09:00",)


def test_script_without_a_closing_tag_is_read_as_far_as_it_goes():
    html = '<html><head><script type="application/ld+json">' + json.dumps(news_article(OWN))
    assert json_ld_of(html) == OWN


@pytest.mark.parametrize("html", [None, "", "   ", "<<<>>>", b"bytes", 123, "<html/>"])
def test_unusable_pages_give_no_values(html):
    assert read_page_values(html) == PageValues(None, ())


def test_non_string_date_published_is_ignored():
    assert json_ld_of(page(ld_script({"@type": "NewsArticle", "datePublished": 1761385800}))) is None


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
    monkeypatch.setattr(pp, "read_page_values", lambda html: reads.append(html) or PageValues(OWN, ()))
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


def test_the_fallback_never_moves_to_another_node_of_the_page():
    """기사 자신의 JSON-LD 값(우선순위 1위)을 못 쓴다고 같은 페이지의 관련 기사 노드 값으로 넘어가면 다른 기사의 시각을 쓰게 된다."""
    related = [news_article("2026-10-23T08:00:00+09:00")]
    # 시간대가 없다 -> NO_TZ (관련 기사의 시간대 있는 값을 쓰지 않는다)
    no_tz = page(ld_script(news_article("2026-10-25T18:50:00", isRelatedTo=related)))
    assert decide_created_at(STORED_FUTURE, CRAWLED, no_tz) == pp.PageCreatedAt(NO_TZ)
    # 형식이 깨졌다 -> NO_PAGE_TIME
    broken = page(ld_script(news_article("2026.10.25 18:50", isRelatedTo=related)))
    assert decide_created_at(STORED_FUTURE, CRAWLED, broken) == pp.PageCreatedAt(NO_PAGE_TIME)
    # 덩어리가 달라도 같다: 앞 덩어리가 기사 자신(시간대 없음), 뒤 덩어리가 관련 기사 목록
    split = page(ld_script(news_article("2026-10-25T18:50:00")), ld_script(related_list("2026-10-23T08:00:00+09:00")))
    assert decide_created_at(STORED_FUTURE, CRAWLED, split) == pp.PageCreatedAt(NO_TZ)
    # 같은 층의 다른 기사도 아니다: 최상위 배열의 둘째 기사
    sibling = page(ld_script([news_article("2026-10-25T18:50:00"), news_article("2026-10-23T08:00:00+09:00")]))
    assert decide_created_at(STORED_FUTURE, CRAWLED, sibling) == pp.PageCreatedAt(NO_TZ)


def test_a_meta_time_is_still_used_after_an_unusable_json_ld_value_of_the_same_article():
    meta = '<meta property="article:published_time" content="2026-10-25T18:49:00+09:00">'
    for published in ("2026-10-25T18:50:00", "2026.10.25 18:50"):
        html = page(ld_script(news_article(published, isRelatedTo=[news_article("2026-10-23T08:00:00+09:00")])), meta)
        assert decide_created_at(STORED_FUTURE, CRAWLED, html) == pp.PageCreatedAt(
            CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 49, tzinfo=UTC)), published


def test_meta_is_used_when_there_is_no_json_ld():
    html = page('<meta property="article:published_time" content="2026-10-25T09:40:00Z">')
    assert decide_created_at(STORED_FUTURE, CRAWLED, html) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 40, tzinfo=UTC))


# JSON-LD 값과 meta 값이 서로 다르면(10분 넘게) 어느 쪽도 쓰지 않는다 - 한 페이지가 두 시각을 말한다


def meta_tag(value: str) -> str:
    return f'<meta property="article:published_time" content="{value}">'


def test_json_ld_and_meta_that_disagree_by_more_than_ten_minutes_are_a_conflict_and_nothing_is_written():
    html = page(ld_script(news_article("2026-10-25T18:50:00+09:00")), meta_tag("2026-10-25T18:30:00+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, html) == pp.PageCreatedAt(CONFLICT)
    # 방향·오프셋이 달라도 절대 시각으로 견준다: 같은 순간을 UTC와 KST로 적은 값은 충돌이 아니다
    same_instant = page(ld_script(news_article("2026-10-25T09:50:00Z")), meta_tag("2026-10-25T18:50:00+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, same_instant) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))
    # 전날 값(수정 시각이 섞인 meta 등)이라도 충돌이다
    day_apart = page(ld_script(news_article("2026-10-25T18:50:00+09:00")), meta_tag("2026-10-24T18:50:00+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, day_apart).outcome == CONFLICT


def test_the_ten_minute_disagreement_boundary_is_not_yet_a_conflict_and_the_json_ld_value_is_the_one_used():
    edge = page(ld_script(news_article("2026-10-25T18:50:00+09:00")), meta_tag("2026-10-25T18:40:00+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, edge) == pp.PageCreatedAt(
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))  # 정확히 10분: 충돌이 아니다, JSON-LD 값
    past = page(ld_script(news_article("2026-10-25T18:50:00+09:00")), meta_tag("2026-10-25T18:39:59+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, past).outcome == CONFLICT
    later = page(ld_script(news_article("2026-10-25T18:40:00+09:00")), meta_tag("2026-10-25T18:50:01+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, later).outcome == CONFLICT


def test_a_conflict_is_judged_before_the_crawl_window_and_does_not_apply_without_both_values():
    after_crawl_ld = page(ld_script(news_article("2026-10-26T18:50:00+09:00")), meta_tag(OWN))
    assert decide_created_at(STORED_FUTURE, CRAWLED, after_crawl_ld).outcome == CONFLICT
    assert decide_created_at(STORED_FUTURE, CRAWLED, good_page()).outcome == CREATED_AT_FIXED  # JSON-LD만
    assert decide_created_at(STORED_FUTURE, CRAWLED, page(meta_tag(OWN))).outcome == CREATED_AT_FIXED  # meta만
    # 한쪽을 읽을 수 없으면 견줄 수 없다 - 읽을 수 있는 쪽을 쓴다
    unreadable_ld = page(ld_script(news_article("어제 저녁")), meta_tag(OWN))
    assert decide_created_at(STORED_FUTURE, CRAWLED, unreadable_ld).outcome == CREATED_AT_FIXED


def test_a_json_ld_value_without_a_zone_is_compared_with_the_meta_wall_clock():
    far = page(ld_script(news_article("2026-10-20T18:50:00")), meta_tag(OWN))
    assert decide_created_at(STORED_FUTURE, CRAWLED, far).outcome == CONFLICT
    near = page(ld_script(news_article("2026-10-25T18:55:00")), meta_tag(OWN))
    assert decide_created_at(STORED_FUTURE, CRAWLED, near) == pp.PageCreatedAt(  # 시간대 없는 JSON-LD 대신 meta의 시각
        CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))
    both_naive = page(ld_script(news_article("2026-10-25T18:50:00")), meta_tag("2026-10-25T18:50:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, both_naive).outcome == NO_TZ


def test_metas_that_disagree_with_each_other_are_a_conflict_even_without_json_ld():
    two = page(meta_tag("2026-10-25T18:50:00+09:00"), meta_tag("2026-10-23T08:00:00+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, two) == pp.PageCreatedAt(CONFLICT)
    # 10분 안이면 충돌이 아니고 첫 값(시간대 있는 값)을 쓴다 / 같은 값을 두 번 적은 페이지도 충돌이 아니다
    near = page(meta_tag("2026-10-25T18:50:00+09:00"), meta_tag("2026-10-25T18:40:00+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, near) == pp.PageCreatedAt(CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))
    same = page(meta_tag(OWN), meta_tag("2026-10-25T09:50:00Z"), meta_tag(OWN))
    assert decide_created_at(STORED_FUTURE, CRAWLED, same) == pp.PageCreatedAt(CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))
    just_over = page(meta_tag("2026-10-25T18:50:00+09:00"), meta_tag("2026-10-25T18:39:59+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, just_over).outcome == CONFLICT
    # 셋째 값만 어긋나도 충돌이다(둘씩 모두 견준다)
    third = page(meta_tag("2026-10-25T18:50:00+09:00"), meta_tag("2026-10-25T18:45:00+09:00"), meta_tag("2026-10-25T18:36:00+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, third).outcome == CONFLICT
    # JSON-LD가 meta 둘과 모두 맞아도, meta끼리 어긋나면 충돌이다
    with_ld = page(ld_script(news_article(OWN)), meta_tag(OWN), meta_tag("2026-10-24T18:50:00+09:00"))
    assert decide_created_at(STORED_FUTURE, CRAWLED, with_ld).outcome == CONFLICT


@pytest.mark.parametrize("date_only", ["2026-10-25", "2026-10-20"])
def test_a_date_only_value_is_not_compared_with_a_full_time_so_the_other_source_is_used(date_only):
    """날짜만 적은 값은 자정으로 읽힐 뿐이다 - 시각 있는 값과 18시간쯤 다르다고 충돌로 세면 고칠 수 있던 행이 미래로 남는다."""
    expected = pp.PageCreatedAt(CREATED_AT_FIXED, datetime(2026, 10, 25, 9, 50, tzinfo=UTC))
    date_in_json_ld = page(ld_script(news_article(date_only)), meta_tag(OWN))
    assert decide_created_at(STORED_FUTURE, CRAWLED, date_in_json_ld) == expected
    date_in_meta = page(ld_script(news_article(OWN)), meta_tag(date_only))
    assert decide_created_at(STORED_FUTURE, CRAWLED, date_in_meta) == expected
    # 날짜만 있는 값 둘뿐이면 시각을 정할 수 없다
    assert decide_created_at(STORED_FUTURE, CRAWLED, page(ld_script(news_article(date_only)), meta_tag(date_only))).outcome == NO_TZ
    # 시각이 있는 값끼리는 그대로 견준다
    assert decide_created_at(STORED_FUTURE, CRAWLED, page(ld_script(news_article("2026-10-20T18:50:00+09:00")), meta_tag(OWN))).outcome == CONFLICT


@pytest.mark.parametrize("without_zone", ["2026-10-20T18:50:00", "2026-10-20 18:50:00", "2026-10-20T18:50", "2026-10-20 18"])
def test_a_value_with_a_time_but_no_zone_is_still_compared_by_wall_clock_unlike_a_date_only_value(without_zone):
    html = page(ld_script(news_article(without_zone)), meta_tag(OWN))
    assert decide_created_at(STORED_FUTURE, CRAWLED, html).outcome == CONFLICT


def test_conflict_is_one_of_the_reasons_the_statistics_count():
    assert CONFLICT == "conflict" and CONFLICT in pp.UNFIXED_REASONS


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

    monkeypatch.setattr(pp, "read_page_values", boom)
    assert decide_created_at(STORED_FUTURE, CRAWLED, good_page()) == pp.PageCreatedAt(NO_PAGE_TIME)


def test_only_the_first_two_million_characters_are_read():
    late = "x" * (pp._MAX_HTML_CHARS + 10) + good_page()
    assert read_page_values(late) == PageValues(None, ())


# ----------------------------------------------------------------------------- 바꾼 행의 기록(되돌리기 근거)


def test_a_change_is_recorded_as_row_id_and_utc_values_whatever_the_session_time_zone():
    seoul_session = timezone(timedelta(hours=9))
    before = datetime(2026, 10, 26, 18, 0, tzinfo=seoul_session)  # 세션 시간대 +09:00로 돌아온 timestamptz
    after = datetime(2026, 10, 25, 9, 50, tzinfo=UTC)
    assert CreatedAtChange(42, before, after).as_stats_row() == [42, "2026-10-26T09:00:00Z", "2026-10-25T09:50:00Z"]


def test_a_recorded_value_keeps_its_microseconds_and_reads_back_as_the_same_instant():
    before = datetime(2026, 10, 26, 9, 0, 1, 234567, tzinfo=UTC)
    row = CreatedAtChange(7, before, datetime(2026, 10, 25, 9, 50, tzinfo=UTC)).as_stats_row()
    assert row[1] == "2026-10-26T09:00:01.234567Z"
    assert datetime.fromisoformat(row[1].replace("Z", "+00:00")) == before
    assert json.loads(json.dumps(row)) == row  # job_runs.stats(jsonb)에 그대로 들어간다


# ----------------------------------------------------------------------------- 페이지를 읽기 전에 끝난 시도


def test_a_future_row_whose_page_could_not_be_read_is_classified_with_the_given_reason():
    assert unfixed_if_future(STORED_FUTURE, CRAWLED, pp.FETCH_FAILED) == "fetch_failed"
    assert unfixed_if_future(STORED_FUTURE, CRAWLED, pp.ERROR) == "error"
    assert pp.FETCH_FAILED in pp.UNFIXED_REASONS and pp.ERROR in pp.UNFIXED_REASONS


def test_a_row_that_is_not_in_the_future_or_has_no_times_is_not_classified():
    normal = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    assert unfixed_if_future(normal, CRAWLED, pp.ERROR) == NOT_FUTURE
    assert unfixed_if_future(None, CRAWLED, pp.ERROR) == NOT_FUTURE
    assert unfixed_if_future(STORED_FUTURE, None, pp.ERROR) == NOT_FUTURE
    assert unfixed_if_future("not a time", CRAWLED, pp.ERROR) == NOT_FUTURE  # 예외를 던지지 않는다


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
