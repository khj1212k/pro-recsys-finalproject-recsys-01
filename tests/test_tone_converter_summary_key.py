# 생성기 초안(core/reconstruction/generator.py)은 한줄 요약을 "sentence" 키로 내놓고,
# DB 저장(core/reconstruction/repository.py::save_news_letter)도 "sentence"를 읽는다.
# 반면 ToneConverter는 "summary" 키만 읽고 써서 (1) 프롬프트의 "요약:" 칸이 항상
# 비었고, (2) 변환된 캐주얼 요약은 save_newsletter_to_db에서 초안의 sentence에 밀려
# 버려졌다 - 출력 토큰을 사서 버리는 셈이었다. 이 테스트들은 두 키를 잇는 경계를 고정한다.
import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeLLMClient

import workflow.nodes as nodes_module
from core.tone_converter import ToneConverter
from core.llm.schemas import ToneResult
from config.settings import Settings


# 생성기 초안과 같은 모양(요약은 sentence 키)
DRAFT = {
    "title": "삼성전자 HBM 증설",
    "sentence": "삼성전자가 HBM 생산 능력을 늘리기로 했다.",
    "content": "삼성전자가 HBM 생산을 확대하기로 했다. 투자 규모는 공개하지 않았다.",
    "keywords": ["삼성전자", "HBM"],
    "categories": ["IT/과학"],
}


def _tone_result(summary="삼성전자가 HBM을 더 만든대요 ✅"):
    return ToneResult(
        title="📰 삼성전자 HBM 증설",
        summary=summary,
        content="📰 삼성전자가 HBM 생산을 늘려요.",
        keywords=["삼성전자", "HBM"],
    )


def test_prompt_receives_the_draft_sentence_as_the_summary_to_convert():
    fake = FakeLLMClient(results=[{"parsed": _tone_result()}])

    ToneConverter(llm_client=fake).convert(DRAFT)

    prompt = fake.calls[0]["messages"][0]["content"]
    assert "요약: 삼성전자가 HBM 생산 능력을 늘리기로 했다." in prompt


def test_converted_summary_is_returned_under_the_sentence_key_too():
    fake = FakeLLMClient(results=[{"parsed": _tone_result(summary="HBM, 더 많이 만든대요 ✅")}])

    converted = ToneConverter(llm_client=fake).convert(DRAFT)

    assert converted["summary"] == "HBM, 더 많이 만든대요 ✅"
    assert converted["sentence"] == "HBM, 더 많이 만든대요 ✅"


def test_fallback_softens_the_draft_sentence_instead_of_reusing_the_title(monkeypatch):
    monkeypatch.setattr(Settings, "MAX_RETRY_TONE_VALIDATION", 0)
    fake = FakeLLMClient(results=[{"parsed": ToneResult(title="", summary="", content="", keywords=[])}])

    converted = ToneConverter(llm_client=fake).convert(DRAFT)

    assert converted["sentence"].startswith("삼성전자가 HBM 생산 능력을 늘리기로 했다.")
    assert converted["summary"] == converted["sentence"]


def test_fallback_softener_applies_the_longer_phrase_before_its_suffix(monkeypatch):
    # "것으로 보입니다"가 "입니다" 치환에 먼저 먹혀 "것으로 보이에요"가 되던 순서 문제
    monkeypatch.setattr(Settings, "MAX_RETRY_TONE_VALIDATION", 0)
    fake = FakeLLMClient(results=[{"parsed": ToneResult(title="", summary="", content="", keywords=[])}])
    draft = dict(DRAFT, content="수요가 늘어날 것으로 보입니다. 정부는 대책을 발표했습니다.")

    converted = ToneConverter(llm_client=fake).convert(draft)

    assert converted["content"] == "📰 수요가 늘어날 것 같아요. 정부는 대책을 발표했어요."


class _FakeDBConn:
    def cursor(self):
        raise AssertionError("임베딩이 없으면 cursor()를 부르지 않는다")

    def commit(self):
        pass


def _save_with(monkeypatch, converted):
    saved = {}

    def fake_save_news_letter(conn, article_ids, newsletter, run_id=None, generation_history=None):
        saved.update(newsletter)
        return 1

    monkeypatch.setattr(nodes_module, "get_connection", lambda: _FakeDBConn())
    monkeypatch.setattr(nodes_module, "release_connection", lambda conn: None)
    monkeypatch.setattr(nodes_module, "save_news_letter", fake_save_news_letter)

    nodes_module.save_newsletter_to_db({
        "newsletter_draft": dict(DRAFT),
        "converted_newsletter": converted,
        "newsletter_embedding": None,
        "current_article_ids": [1, 2, 3],
        "current_cluster_id": 7,
        "run_id": 1,
        "completed_newsletters": [],
    })
    return saved


def test_save_node_stores_the_converted_summary_not_the_formal_draft_sentence(monkeypatch):
    # 문체 변환 결과가 summary 키만 가진 경우(구 형태)에도 캐주얼 요약이 저장돼야 한다
    saved = _save_with(monkeypatch, {
        "title": "📰 삼성전자 HBM 증설",
        "summary": "HBM, 더 많이 만든대요 ✅",
        "content": "📰 삼성전자가 HBM 생산을 늘려요.",
        "keywords": ["삼성전자", "HBM"],
    })

    assert saved["sentence"] == "HBM, 더 많이 만든대요 ✅"
    assert saved["title"] == "📰 삼성전자 HBM 증설"
    # 문체 변환은 카테고리를 만들지 않는다 - 초안의 값이 그대로 남아야 한다
    assert saved["categories"] == ["IT/과학"]


def test_save_node_keeps_the_draft_sentence_when_there_is_no_conversion(monkeypatch):
    saved = _save_with(monkeypatch, None)

    assert saved["sentence"] == "삼성전자가 HBM 생산 능력을 늘리기로 했다."
