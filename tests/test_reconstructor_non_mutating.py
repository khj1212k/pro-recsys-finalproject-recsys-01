import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


def _make_reconstructor():
    from core.reconstruction.generator import NewsReconstructor

    with patch("core.reconstruction.generator.get_llm_client", return_value=MagicMock()):
        return NewsReconstructor()


def test_reconstruct_does_not_mutate_input_article_list():
    """NewsReconstructor.reconstruct()는 기사 11개 이상(>10)을 넘기면 내부적으로
    정렬 후 상위 10개만 사용한다. 이전에는 articles.sort()로 호출자의 리스트를
    in-place 정렬해 LangGraph state["current_articles"]까지 변형시켰다.
    reconstruct 호출 후에도 원본 리스트의 순서가 바뀌지 않아야 한다."""
    reconstructor = _make_reconstructor()

    # content 길이를 서로 다르게 만들어 정렬이 실제로 순서를 바꾸도록 유도
    articles = [
        {"id": i, "title": f"제목{i}", "press_name": "언론사", "content": "x" * i}
        for i in range(1, 15)  # 14개 (MAX_ARTICLES=10 초과)
    ]
    original_order = list(articles)  # 얕은 복사: 원본 순서/참조 기록

    with patch.object(reconstructor, "_generate_content", return_value="본문 요약"), \
         patch.object(reconstructor, "_generate_meta", return_value={
             "title": "제목", "sentence": "한줄요약", "keywords": ["k1"], "categories": ["사회"]
         }):
        result = reconstructor.reconstruct(articles)

    assert result is not None
    # 버그가 있었다면 articles.sort()가 원본 리스트 순서를 뒤집어 놓았을 것이다
    assert articles == original_order
    assert [a["id"] for a in articles] == list(range(1, 15))


def test_reconstruct_still_limits_to_max_articles_in_output():
    """비-mutating으로 바뀌어도 프롬프트에 들어가는 기사 수는 여전히 10개로 제한돼야 한다."""
    reconstructor = _make_reconstructor()

    articles = [
        {"id": i, "title": f"제목{i}", "press_name": "언론사", "content": "x" * i}
        for i in range(1, 15)
    ]

    captured = {}

    def fake_generate_content(articles_text, feedback=None):
        captured["articles_text"] = articles_text
        return "본문 요약"

    with patch.object(reconstructor, "_generate_content", side_effect=fake_generate_content), \
         patch.object(reconstructor, "_generate_meta", return_value={
             "title": "제목", "sentence": "한줄요약", "keywords": ["k1"], "categories": ["사회"]
         }):
        reconstructor.reconstruct(articles)

    # 가장 긴 content를 가진 기사(id=14 ~ id=5, 10개)만 프롬프트 텍스트에 포함돼야 한다
    assert captured["articles_text"].count("[기사") == 10
    assert "제목14" in captured["articles_text"]
    assert "제목4" not in captured["articles_text"]
