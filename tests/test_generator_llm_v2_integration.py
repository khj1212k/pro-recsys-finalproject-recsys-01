import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeLLMClient

from core.reconstruction.generator import NewsReconstructor
from core.llm.schemas import NewsletterContent, NewsletterMeta


ARTICLES = [
    {"title": "삼성전자 HBM 증설 발표", "press_name": "동아일보", "content": "삼성전자가 HBM 생산을 확대한다. " * 10},
    {"title": "SK하이닉스도 투자 확대", "press_name": "한국경제", "content": "SK하이닉스도 투자를 늘린다. " * 10},
]


def test_reconstruct_uses_injected_llm_client_and_two_calls_in_order():
    fake = FakeLLMClient(results=[
        {"parsed": NewsletterContent(content="AI 반도체 수요 급증으로 HBM 경쟁이 본격화되고 있다. " * 10)},
        {"parsed": NewsletterMeta(
            title="HBM 경쟁 본격화",
            sentence="반도체 업계, 새 국면 맞이했어요",
            keywords=["삼성전자", "SK하이닉스", "HBM", "메모리", "반도체"],
            categories=["IT/과학"],
        )},
    ])
    reconstructor = NewsReconstructor(llm_client=fake)

    result = reconstructor.reconstruct(ARTICLES)

    assert fake.call_count == 2
    # Call #1: 본문 생성 - schema=NewsletterContent
    assert fake.calls[0]["schema"] is NewsletterContent
    assert fake.calls[0]["purpose"] == "newsletter_content_gen"
    # Call #2: 메타 생성 - schema=NewsletterMeta
    assert fake.calls[1]["schema"] is NewsletterMeta
    assert fake.calls[1]["purpose"] == "newsletter_meta_gen"

    assert result["title"] == "HBM 경쟁 본격화"
    assert result["keywords"] == ["삼성전자", "SK하이닉스", "HBM", "메모리", "반도체"]
    assert result["categories"] == ["IT/과학"]
    assert "AI 반도체 수요 급증" in result["content"]


def test_reconstruct_falls_back_to_heuristic_content_and_meta_when_both_calls_fail():
    # Call #1(본문)이 완전히 실패해도 fallback_content()는 항상 비어있지 않은 문자열을
    # 반환하므로, reconstruct()는 이어서 Call #2(메타)도 시도한다 - 그것도 실패하면
    # fallback_meta()로 끝난다.
    fake = FakeLLMClient(results=[
        {"parsed": None, "text": None, "error": "max retries exhausted"},
        {"parsed": None, "text": None, "error": "max retries exhausted"},
    ])
    reconstructor = NewsReconstructor(llm_client=fake)

    result = reconstructor.reconstruct(ARTICLES)

    assert fake.call_count == 2
    assert result is not None
    # fallback_content()는 articles_text의 "제목:" 줄에서 기사 제목을 뽑아 불릿으로 나열한다
    assert "삼성전자 HBM 증설 발표" in result["content"]
    # fallback_meta()는 제목이 없으면 본문 첫 줄을 title로 쓴다
    assert result["title"]
    assert len(result["keywords"]) == 5
