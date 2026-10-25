"""국민일보 기사 페이지의 태그·class·id 구조만 흉내 낸 지어낸 페이지 (ADR 0023: 기사 본문·제목은 저장소에 넣지 않는다).

2026-10-19 개편 뒤 페이지의 뼈대(실제 페이지를 구조로만 읽은 것 - 글은 하나도 옮기지 않았다):

    div#wrap
      div#view
        div.article_top
          div.article_view
            h1.article_headline
            div#article.article_content
              div#articleBody.article_body        <- 기사 본문 컨테이너
                div  (figure.image > figcaption, p 여러 개, 광고 div > iframe, script, style, div.article_recommend)
                p    (서명)
                a
          div.aside
            div.col_lg12 > div.mn_kpick > div
              div#kpick_swiper.card_group.swiper  <- 기사와 상관없는 특집 카드 9장(카드마다 p.description이 길다)

본문 노드(#articleBody)는 글자가 1,000자 안팎인데 특집 카드 묶음(#kpick_swiper)은 25,000자쯤이라, trafilatura가 후보를
견줄 때 본문 대신 특집 카드 묶음을 고른다. 이 모듈의 글은 모두 지어낸 문장이고, 본문 문장에는 '본문표지', 특집 카드 문장에는
'특집표지'라는 말을 넣어 테스트가 어느 쪽이 뽑혔는지 글자 수가 아니라 표지로 가린다.
"""
import html
import random
from typing import List

BODY_MARK = "본문표지"
KPICK_MARK = "특집표지"
AD_MARK = "광고표지"
RECOMMEND_MARK = "추천표지"
FOOTER_MARK = "푸터표지"

_SUBJECT = ["가상시", "시험군", "예시구", "모의협회", "연습위원회", "표본기관", "임시연구소", "가정사업단"]
_OBJECT = ["점검 계획", "시범 사업", "예산 배분안", "운영 지침", "조사 결과", "개선 방안", "일정 조정안", "협의 내용"]
_VERB = ["발표했다", "공개했다", "설명했다", "검토하기로 했다", "확정했다", "논의했다", "제안했다", "정리했다"]
_TAIL = [
    "관계자는 단계별로 진행할 예정이라고 덧붙였다.",
    "이번 내용은 시험 목적의 가상 문장이다.",
    "세부 일정은 추후 알리기로 했다.",
    "참석자들은 의견을 나누고 다음 모임을 약속했다.",
]


def sentences(mark: str, count: int, seed: int) -> List[str]:
    rnd = random.Random(seed)
    return [
        f"{mark} {i + 1}번 문장에서 {rnd.choice(_SUBJECT)}은 {rnd.choice(_OBJECT)}을 {rnd.choice(_VERB)}. {rnd.choice(_TAIL)}"
        for i in range(count)
    ]


def _paragraph(mark: str, seed: int, count: int = 2) -> str:
    return " ".join(sentences(mark, count, seed))


def kmib_like_page(*, body_paragraphs: int = 8, kpick_sentences_per_card: int = 20, container_id: str = "articleBody",
                   seed: int = 1, kpick_seed: int = 0) -> str:
    """개편 뒤 국민일보 기사 페이지의 구조를 흉내 낸 HTML. container_id를 바꾸면 본문 컨테이너를 못 찾는 페이지가 된다.

    seed는 기사 본문 문장만 바꾼다. 특집 카드 묶음은 실제 페이지처럼 기사가 달라도 같다(kpick_seed가 같으면 같은 글) -
    그래서 전체 페이지를 추출하면 기사가 달라도 같은 특집 글이 나와 본문 해시가 하나로 모인다."""
    half = body_paragraphs // 2
    first = "".join(f"<p>{html.escape(_paragraph(BODY_MARK, seed * 100 + i))}</p>" for i in range(half))
    second = "".join(
        f"<p>{html.escape(_paragraph(BODY_MARK, seed * 100 + 50 + i))}</p>" for i in range(body_paragraphs - half)
    )
    cards = "".join(
        '<div class="card flex_fw rgap_lg2 swiper-slide"><div class="card_body rgap_lg2">'
        f'<p class="title">{html.escape(f"가상 특집 제목 {c + 1}")}</p>'
        f'<p class="description">{html.escape(" ".join(sentences(f"{KPICK_MARK}{c + 1}", kpick_sentences_per_card, 9000 + c + kpick_seed)))}</p>'
        "</div></div>"
        for c in range(9)
    )
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>가상 기사 제목</title>
<meta property="og:title" content="가상 기사 제목"><meta property="og:description" content="가상 요약">
<script type="application/ld+json">{{"@context":"https://schema.org","@type":"NewsArticle","headline":"가상 기사 제목"}}</script>
</head><body><div id="wrap"><header class="header"><a class="logo" href="/">가상일보</a></header>
<div id="view" class="view mt_lg4"><div class="article_top flex flex_fw">
<div class="article_view col_md12 col_sm12"><h1 class="article_headline fs_lg36 fs_sm24 fw_lgbd lh_lg15 mb_lg4">가상 기사 제목</h1>
<div id="article" class="article_content"><div id="{container_id}" class="article_body fs_lg18 lh_lg18 fw_lgrg"><div>
<figure class="image"><img src="/fake.jpg" storyid="1"><figcaption>가상 사진 설명입니다.</figcaption></figure>
{first}
<div id="ad_wrap_content3" class="ad_wrap_content"><div><iframe id="ad_iframe_content3" src="about:blank"></iframe></div></div>
<script>var fake = "{AD_MARK}";</script><style>.fake::after{{content:"{AD_MARK}"}}</style>
{second}
<div class="article_recommend flex flex_jccn pt_lg3"><div class="col_xs12 card flex_row border flex_jcsb"><div class="card_body rgap_lg1 tal"><a href="/x">{RECOMMEND_MARK} 가상 관련 기사 제목</a></div></div></div>
</div><p>가상 기자 서명</p><a href="/x" target="_blank">가상 링크</a></div></div></div>
<div class="aside col_md12 col_sm12 lline_lgnor hidden_md hidden_sm"><div class="col_lg12 mt_lg10"><div class="mn_kpick flex flex_col flex_jcsb h100p"><div>
<div id="kpick_swiper" class="card_group ovh swiper"><div class="swiper-wrapper">{cards}</div></div></div></div></div></div>
</div></div><footer>{FOOTER_MARK} 가상 푸터</footer></div></body></html>"""
