# ADR 0010: 생성 직후의 결정론적 사실성 게이트(check_faithfulness)와 문체 변환 직후의
# 드리프트 게이트(check_tone_drift)를 LangGraph 서브그래프 전체로 검증한다.
# 모든 LLM은 페이크이고 DB 저장도 페이크다(로컬에 Postgres 없음).
import sys
import os

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeLLMClient

import workflow.evaluators as evaluators_module
import workflow.nodes as nodes_module
import core.reconstruction.generator as generator_module
import core.tone_converter as tone_module
from core.llm.schemas import (
    ClusterEval, CriterionScores, NewsletterContent, NewsletterEvalV2, NewsletterMeta, ToneResult,
)
from workflow.gates import check_newsletter_faithfulness, check_tone_drift
from workflow.graph import compile_workflow

TITLES = ["삼성전자 HBM 증설", "SK하이닉스 투자 확대", "메모리 업황 개선"]
BODIES = [
    "삼성전자는 평택 공장에 HBM 라인을 증설한다고 밝혔다. 투자 규모는 2조 원이다.",
    "SK하이닉스도 청주 공장 투자를 늘린다. 회사는 투자 규모를 1조5000억 원으로 잡았다.",
    "메모리 업황이 개선되면서 D램 가격이 10% 올랐다.",
]
CLEAN = (
    "삼성전자와 SK하이닉스가 HBM 투자를 늘리고 있다. 삼성전자는 평택 공장에 2조 원을 투입하고, "
    "SK하이닉스는 1조5000억 원을 투자한다. 메모리 업황 개선으로 D램 가격은 10% 올랐다."
)
HALLUCINATED = CLEAN.replace("2조 원을", "3조 원을")
CASUAL_OK = "📰 삼성전자와 SK하이닉스가 HBM 투자를 늘려요. 삼성전자는 2조 원, SK하이닉스는 1조5000억 원이에요. D램 가격은 10% 올랐어요."
CASUAL_DRIFT = CASUAL_OK.replace("2조 원", "20조 원")


def _state():
    return {
        "run_id": 7,
        "all_cluster_groups": {1: [10, 11, 12]},
        "all_cluster_ids": [1],
        "current_cluster_index": 0,
        "data": {"ids": [10, 11, 12], "titles": TITLES, "contents": BODIES,
                 "press_names": ["동아일보", "경향신문", "한국경제"]},
        "completed_newsletters": [],
        "failed_clusters": [],
        "skipped_clusters": [],
    }


def _content(text):
    return {"parsed": NewsletterContent(content=text)}


def _meta():
    return {"parsed": NewsletterMeta(title="HBM 투자 확대", sentence="반도체, 다시 달릴까요?",
                                     keywords=["삼성전자", "SK하이닉스", "HBM", "D램", "투자"],
                                     categories=["IT/과학"])}


def _tone(content):
    return {"parsed": ToneResult(title="📰 HBM 투자 확대", summary="반도체 소식이에요", content=content,
                                 keywords=["HBM"])}


def _judge_pass():
    return {"parsed": NewsletterEvalV2(scores=CriterionScores(faithfulness=5, coverage=4, coherence=4, style=4))}


def _cluster_pass():
    return {"parsed": ClusterEval(decision="PASS", confidence=0.9, summary="HBM 투자")}


@pytest.fixture
def wire(monkeypatch):
    saved = []

    def fake_save(conn, article_ids, newsletter, run_id=None, generation_history=None):
        saved.append({"newsletter": newsletter, "history": generation_history})
        return 100 + len(saved)

    class _Conn:
        def cursor(self):
            raise AssertionError("임베딩이 없으므로 cursor()가 불리면 안 된다")

        def commit(self):
            pass

    monkeypatch.setattr(nodes_module, "get_connection", lambda: _Conn())
    monkeypatch.setattr(nodes_module, "release_connection", lambda conn: None)
    monkeypatch.setattr(nodes_module, "save_news_letter", fake_save)
    monkeypatch.setattr(nodes_module.Settings, "FAITHFULNESS_GATE_MODE", "enforce")
    monkeypatch.setattr(nodes_module.Settings, "TONE_DRIFT_GATE_MODE", "enforce")
    monkeypatch.setattr(nodes_module.Settings, "JUDGE_GATE_MODE", "enforce")

    def _wire(judge, generator, tone):
        monkeypatch.setattr(evaluators_module, "get_client", lambda role: judge)
        monkeypatch.setattr(generator_module, "get_client", lambda role: generator)
        monkeypatch.setattr(tone_module, "get_client", lambda role: tone)
        return saved

    return _wire


def test_hallucinated_number_triggers_regeneration_with_concrete_feedback(wire):
    judge = FakeLLMClient(results=[_cluster_pass(), _judge_pass()])
    generator = FakeLLMClient(results=[_content(HALLUCINATED), _meta(), _content(CLEAN), _meta()])
    tone = FakeLLMClient(results=[_tone(CASUAL_OK)])
    saved = wire(judge, generator, tone)

    final = compile_workflow().invoke(_state())

    assert generator.call_count == 4  # 본문+메타 × 2회
    retry_prompt = generator.calls[2]["messages"][1]["content"]
    assert "3조 원" in retry_prompt  # 어떤 수치가 문제인지 구체적으로 알려준다
    # 사실성 게이트를 통과하지 못한 초안은 judge에게 가지 않는다(클러스터 평가 1 + 뉴스레터 평가 1)
    assert judge.call_count == 2
    assert final["completed_newsletters"] == [101]
    assert final["newsletter_draft"]["content"] == CLEAN
    attempts = saved[0]["history"]["attempts"]
    assert attempts[0]["faithfulness"]["passed"] is False
    assert attempts[0]["faithfulness"]["blocking"]["numbers"][0]["surface"] == "3조 원"
    assert attempts[1]["faithfulness"]["passed"] is True


def test_persistent_hallucination_fails_the_cluster_without_publishing(wire):
    judge = FakeLLMClient(results=[_cluster_pass()])
    generator = FakeLLMClient(results=[_content(HALLUCINATED), _meta()] * 3)
    tone = FakeLLMClient(results=[])
    saved = wire(judge, generator, tone)

    final = compile_workflow().invoke(_state())

    assert generator.call_count == 6  # MAX_RETRY_NEWSLETTER_EVAL(3)회 생성 후 중단
    assert judge.call_count == 1
    assert saved == []
    assert final["failed_clusters"] == [1]
    assert final["failure_reason"] == "faithfulness"


def test_shadow_mode_records_the_report_but_does_not_regenerate(wire, monkeypatch):
    monkeypatch.setattr(nodes_module.Settings, "FAITHFULNESS_GATE_MODE", "shadow")
    judge = FakeLLMClient(results=[_cluster_pass(), _judge_pass()])
    generator = FakeLLMClient(results=[_content(HALLUCINATED), _meta()])
    tone = FakeLLMClient(results=[_tone(CASUAL_OK.replace("2조", "3조"))])
    saved = wire(judge, generator, tone)

    final = compile_workflow().invoke(_state())

    assert generator.call_count == 2
    assert final["faithfulness_report"]["passed"] is False
    assert final["faithfulness_report"]["gate_passed"] is True
    assert len(saved) == 1


def test_drifted_tone_is_reconverted_once_then_falls_back_to_formal_draft(wire):
    judge = FakeLLMClient(results=[_cluster_pass(), _judge_pass()])
    generator = FakeLLMClient(results=[_content(CLEAN), _meta()])
    tone = FakeLLMClient(results=[_tone(CASUAL_DRIFT), _tone(CASUAL_DRIFT)])
    saved = wire(judge, generator, tone)

    final = compile_workflow().invoke(_state())

    assert tone.call_count == 2  # 최초 1회 + MAX_RETRY_TONE_DRIFT(1)회
    assert "20조 원" in tone.calls[1]["messages"][0]["content"]  # 재변환 프롬프트에 드리프트 피드백
    assert final["tone_fallback"] == "formal"
    assert saved[0]["newsletter"]["content"] == CLEAN  # 캐주얼본 대신 형식체 초안을 저장
    assert saved[0]["newsletter"]["title"] == "HBM 투자 확대"
    assert saved[0]["history"]["tone_drift"]["fallback"] == "formal"


def test_tone_drift_fixed_on_reconversion_saves_the_casual_version(wire):
    judge = FakeLLMClient(results=[_cluster_pass(), _judge_pass()])
    generator = FakeLLMClient(results=[_content(CLEAN), _meta()])
    tone = FakeLLMClient(results=[_tone(CASUAL_DRIFT), _tone(CASUAL_OK)])
    saved = wire(judge, generator, tone)

    compile_workflow().invoke(_state())

    assert tone.call_count == 2
    assert saved[0]["newsletter"]["content"] == CASUAL_OK


def test_clean_run_calls_each_role_once(wire):
    judge = FakeLLMClient(results=[_cluster_pass(), _judge_pass()])
    generator = FakeLLMClient(results=[_content(CLEAN), _meta()])
    tone = FakeLLMClient(results=[_tone(CASUAL_OK)])
    saved = wire(judge, generator, tone)

    final = compile_workflow().invoke(_state())

    assert (judge.call_count, generator.call_count, tone.call_count) == (2, 2, 1)
    assert saved[0]["newsletter"]["content"] == CASUAL_OK
    assert final["faithfulness_report"]["passed"] is True
    assert final["tone_drift_report"]["passed"] is True


# ---------------------------------------------------------------------------
# 게이트 함수 단위
# ---------------------------------------------------------------------------

ARTICLES = [{"title": t, "content": b} for t, b in zip(TITLES, BODIES)]


def test_numbers_in_article_titles_count_as_supported():
    arts = [{"title": "HBM 투자 5조 원 돌파", "content": "본문에는 수치가 없다."}]
    draft = {"title": "t", "sentence": "s", "content": "HBM 투자가 5조 원을 넘었다."}
    assert check_newsletter_faithfulness(draft, arts).passed is True


def test_unsupported_items_are_attributed_to_the_field_they_appear_in():
    draft = {"title": "투자 7조 원", "sentence": "s", "content": CLEAN}
    result = check_newsletter_faithfulness(draft, ARTICLES)
    item = result.blocking["numbers"][0]
    assert item["field"] == "title"
    assert draft["title"][item["span"][0]:item["span"][1]] == item["surface"]


def test_entities_are_advisory_by_default_and_blocking_when_configured():
    draft = {"title": "t", "sentence": "s", "content": CLEAN + " 카카오도 참여했다."}
    advisory = check_newsletter_faithfulness(draft, ARTICLES)
    assert advisory.passed is True
    assert any(e["surface"] == "카카오" for e in advisory.advisory["entities"])

    strict = check_newsletter_faithfulness(draft, ARTICLES, blocking_types=("numbers", "quotes", "entities"))
    assert strict.passed is False


def test_fabricated_quote_blocks():
    draft = {"title": "t", "sentence": "s", "content": CLEAN + ' 회사 측은 "시장을 선도하겠다"고 말했다.'}
    result = check_newsletter_faithfulness(draft, ARTICLES)
    assert result.passed is False
    assert result.blocking["quotes"][0]["surface"] == "시장을 선도하겠다"


def test_tone_drift_ignores_condensed_repetition_but_blocks_changed_numbers():
    formal = {"title": "HBM 투자", "content": "투자는 2조 원이다. 삼성전자의 투자는 2조 원 규모다."}
    assert check_tone_drift(formal, {"title": "📰 HBM 투자", "content": "삼성전자가 2조 원을 투자해요."}).passed
    changed = check_tone_drift(formal, {"title": "📰 HBM 투자", "content": "삼성전자가 3조 원을 투자해요."})
    assert changed.passed is False
    assert {n["surface"] for n in changed.blocking["numbers"]} == {"2조 원", "3조 원"}


def test_tone_drift_relative_dates_are_advisory_absolute_dates_block():
    formal = {"title": "t", "content": "지난해 9월 25일 발표했다."}
    relative = check_tone_drift(formal, {"title": "t", "content": "작년 9월 25일 발표했어요."})
    assert relative.passed is True
    absolute = check_tone_drift(formal, {"title": "t", "content": "지난해 9월 26일 발표했어요."})
    assert absolute.passed is False
