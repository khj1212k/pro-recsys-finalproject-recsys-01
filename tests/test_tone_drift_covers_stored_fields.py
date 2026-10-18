# C4 (docs/adr/0010 Addendum 2026-10-18): save_newsletter_to_db는 문체 변환본의 title·sentence·content를
# 저장한다. 드리프트 게이트(workflow/gates.py::TONE_FIELDS)가 그중 sentence를 빠뜨리면 변환 LLM이
# 한줄소개의 수치를 바꿔도 어떤 게이트도 보지 못한 채 발행된다(고치기 전 TONE_FIELDS=("title","content"),
# 주석은 "sentence는 저장되지 않으므로"라고 틀리게 적혀 있었다).
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests import llm_graph_fakes as fx  # noqa: E402

import workflow.gates as gates_module  # noqa: E402
from workflow.gates import TONE_FIELDS, check_tone_drift  # noqa: E402
from workflow.graph import compile_workflow  # noqa: E402
from workflow.nodes import save_newsletter_to_db  # noqa: E402

DRAFT = {"title": "HBM 투자 확대", "sentence": fx.SENTENCE, "content": fx.CLEAN, "keywords": ["HBM"]}
TAMPERED_SENTENCE = "삼성전자가 20조 원을 투자해요 ✅"


def _converted(sentence=fx.CASUAL_SENTENCE, with_alias=True):
    out = {"title": "📰 HBM 투자 확대", "summary": sentence, "content": fx.CASUAL_BODY, "keywords": ["HBM"]}
    if with_alias:
        out["sentence"] = sentence
    return out


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, llm_ledger_init):
    fx.configure_env(monkeypatch, tmp_path, llm_ledger_init)
    yield
    fx.teardown_env()


# ---------------------------------------------------------------------------
# 드리프트 테스트: 저장 필드가 늘거나 바뀌면 이 테스트가 게이트의 비교 필드를 다시 보게 한다
# ---------------------------------------------------------------------------

def test_tone_fields_cover_every_text_field_saved_from_the_converted_newsletter(monkeypatch):
    saved = fx.no_db(monkeypatch)
    draft = {"title": "초안 제목", "sentence": "초안 한줄", "content": "초안 본문", "keywords": ["a"], "categories": ["사회"]}
    converted = {"title": "변환 제목", "summary": "변환 한줄", "sentence": "변환 한줄", "content": "변환 본문",
                 "keywords": ["a"]}
    state = {"newsletter_draft": draft, "converted_newsletter": converted, "current_cluster_id": 1,
             "current_article_ids": [1, 2], "current_articles": [{"id": 1}, {"id": 2}], "run_id": 1}

    save_newsletter_to_db(state)

    stored = saved[0]["newsletter"]
    from_converted = {f for f in ("title", "sentence", "content") if stored[f] != draft[f]}
    assert from_converted == {"title", "sentence", "content"}  # 셋 다 변환본에서 온다
    assert from_converted <= set(TONE_FIELDS), "저장되는 변환본 필드를 드리프트 게이트가 비교하지 않는다"


def test_tone_fields_include_sentence():
    assert "sentence" in TONE_FIELDS


# ---------------------------------------------------------------------------
# 게이트 단위: sentence의 수치 변조
# ---------------------------------------------------------------------------

def test_clean_casual_conversion_passes():
    assert check_tone_drift(DRAFT, _converted()).passed


@pytest.mark.parametrize("with_alias", [True, False])
def test_a_number_changed_only_in_the_sentence_is_blocked(with_alias):
    result = check_tone_drift(DRAFT, _converted(TAMPERED_SENTENCE, with_alias=with_alias))

    assert not result.passed
    surfaces = {(n["surface"], n["change"]) for n in result.blocking["numbers"]}
    assert ("20조 원", "added") in surfaces
    assert "20조 원" in result.feedback


def test_the_same_tampering_is_invisible_when_sentence_is_left_out_of_the_comparison(monkeypatch):
    """대조군: 고치기 전 TONE_FIELDS로는 이 변조가 통과했다(C4의 크기)."""
    monkeypatch.setattr(gates_module, "TONE_FIELDS", ("title", "content"))

    assert check_tone_drift(DRAFT, _converted(TAMPERED_SENTENCE)).passed


# ---------------------------------------------------------------------------
# 그래프 + 실제 role 클라이언트 + 페이크 전송 계층
# ---------------------------------------------------------------------------

def _run_graph(monkeypatch, tone_responses):
    saved = fx.no_db(monkeypatch)
    gen, judge = fx.role_transports([fx.content(), fx.meta()] + tone_responses, [fx.cluster_pass(), fx.judge_pass()])
    final = compile_workflow().invoke(fx.initial_state())
    return final, saved, gen


def test_sentence_number_tampering_twice_saves_the_formal_draft_sentence(monkeypatch):
    final, saved, gen = _run_graph(monkeypatch, [fx.tone(summary=TAMPERED_SENTENCE), fx.tone(summary=TAMPERED_SENTENCE)])

    assert final["completed_newsletters"] == [901]
    stored = saved[0]["newsletter"]
    assert stored["sentence"] == fx.SENTENCE  # 변조된 캐주얼 한줄소개 대신 형식체 초안의 한줄소개
    assert "20조" not in stored["sentence"] + stored["title"] + stored["content"]
    assert final["tone_fallback"] == "formal"
    assert final["tone_drift_report"]["blocking"]["numbers"][0]["surface"] == "20조 원"
    assert gen.chat.completions.call_count == 4  # 본문, 메타, 문체 변환 2회(최초 + 드리프트 재변환 1회)
    retry_prompt = gen.chat.completions.parse_calls[3]["messages"][0]["content"]
    assert "20조 원" in retry_prompt  # 무엇이 바뀌었는지 재변환 프롬프트에 들어간다


def test_sentence_number_tampering_fixed_on_the_retry_saves_the_casual_sentence(monkeypatch):
    final, saved, gen = _run_graph(monkeypatch, [fx.tone(summary=TAMPERED_SENTENCE), fx.tone()])

    assert final["completed_newsletters"] == [901]
    assert saved[0]["newsletter"]["sentence"] == fx.CASUAL_SENTENCE
    assert final.get("tone_fallback") in (None, "")
    assert gen.chat.completions.call_count == 4
