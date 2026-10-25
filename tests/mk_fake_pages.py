"""매일경제 기사 페이지의 태그·class·itemprop 구조만 흉내 낸 지어낸 페이지 (ADR 0023: 기사 본문·제목은 저장소에 넣지 않는다).

2026-10 읽기 전용 측정으로 본 페이지의 뼈대(실제 페이지를 구조로만 읽은 것 - 글은 하나도 옮기지 않았다):

    div#wrapper
      main#container > section.viewpage > div.grid-3col
        div.g-2col
          div.article
            section.news_ttl_sec                      (제목·바이라인)
            section.sec_body
              div.news_cnt_detail_wrap[itemprop=articleBody]   <- 기사 본문 컨테이너
                div.mid_title, div.thumb_area.img, 그 뒤로 <br>로 나뉜 본문 글(문단이 <p>가 아니라 글 노드다)
              div.tags
        div.g-3col.g-3col-view
          div.layer_right_stock_new > div.stock_story
            div.modal_ai_explain > div.ai_explain_wrap > div.ai_explain_body > div.content_section
              ul.timeline_list > li.timeline_item ...   <- 기사와 상관없이 매일경제가 붙이는 해설 모달

본문 노드는 글자가 1,000자 안팎인데 해설 모달은 5,000자 안팎이라, 페이지 전체를 trafilatura에 넘기면 모달 글이 본문에 섞여 길이가
몇 배가 된다(같은 페이지도 시간이 지나면 모달이 생긴다). 이 모듈의 글은 모두 지어낸 문장이고, 본문 문장에는 '본문표지', 모달 문장에는
'모달표지'라는 말을 넣어 테스트가 어느 쪽이 뽑혔는지 글자 수가 아니라 표지로 가린다.
"""
import html

from tests.kmib_fake_pages import AD_MARK, sentences  # noqa: F401 - AD_MARK는 테스트가 같이 쓴다

BODY_MARK = "본문표지"
MODAL_MARK = "모달표지"
TAIL_MARK = "추천표지"
FOOTER_MARK = "푸터표지"


def mk_like_page(*, body_paragraphs: int = 6, modal_items: int = 12, with_modal: bool = True, seed: int = 1,
                 container_attr: str = 'itemprop="articleBody"', modal_seed: int = 0) -> str:
    """매일경제 기사 페이지의 구조를 흉내 낸 HTML.

    container_attr를 바꾸면 본문 컨테이너를 못 찾는 페이지가 된다. with_modal=False는 해설 모달이 아직 없는 페이지(같은
    페이지도 시간이 지나면 모달을 갖는다). 모달 글은 modal_seed가 같으면 같다. 본문 글은 seed로 바뀐다."""
    paragraphs = [
        " ".join(sentences(BODY_MARK, 2, seed * 100 + i)) for i in range(body_paragraphs)
    ]
    # 실제 페이지처럼 문단은 <p>가 아니라 <br> 앞뒤의 글 노드다
    body_text = "<br><br>".join(html.escape(p) for p in paragraphs)
    items = "".join(
        '<li class="timeline_item"><span class="time">가상 시각</span>'
        f'<p class="desc">{html.escape(" ".join(sentences(f"{MODAL_MARK}{i + 1}", 6, 7000 + i + modal_seed)))}</p></li>'
        for i in range(modal_items)
    )
    modal = (
        '<div class="modal_ai_explain active"><div class="ai_explain_wrap"><div class="ai_explain_body">'
        f'<div class="content_section"><ul class="timeline_list">{items}</ul></div></div></div></div>'
        if with_modal else ""
    )
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>가상 기사 제목 : 가상경제</title>
<meta property="og:title" content="가상 기사 제목"><meta property="og:description" content="가상 요약">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"NewsArticle","headline":"가상 기사 제목"}}</script>
</head><body><div id="wrapper"><header id="header"><section class="gnb"><a class="logo" href="/">가상경제</a></section></header>
<main id="container"><section class="viewpage"><div class="grid-3col"><div class="g-2col"><div class="article">
<section class="news_ttl_sec"><h2 class="news_ttl">가상 기사 제목</h2><div class="byline-box">가상 기자 입력 시각</div></section>
<section class="sec_body"><div class="news_cnt_detail_wrap" {container_attr}><div class="mid_title">가상 중간 제목입니다</div>
<div class="thumb_area img">가상 사진</div><br>{body_text}</div><div class="tags"><a href="/t">가상태그</a></div></section>
</div><div class="t_topnews_ai"><ul class="top_news_list"><li><a href="/x">{TAIL_MARK} 가상 관련 기사 제목</a></li></ul></div></div>
<div class="g-3col g-3col-view"><div class="layer_right_stock_new"><div class="stock_story">{modal}</div></div></div>
</div></section></main><footer>{FOOTER_MARK} 가상 푸터</footer></div></body></html>"""
