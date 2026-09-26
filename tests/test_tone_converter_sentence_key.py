# 파이프라인의 한 줄 요약 키는 `sentence`다(generator 초안, NewsletterEvaluator 프롬프트,
# DB 컬럼 news_letter_sentence). ToneConverter는 `summary`만 읽고 써서
# (1) 문체 변환 프롬프트의 "요약:" 칸이 항상 비었고
# (2) LLM이 만든 캐주얼 요약은 save 노드에서 {**draft, **converted} 병합 시 `sentence`를
#     덮지 못해 버려졌다 - 저장되는 요약은 격식체, 본문은 캐주얼체로 문체가 섞였다.
# (2026-09-26 첫 E2E 워밍업 실행 전에 수정; docs/design 리뷰 P3/P9 참고)
import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import FakeLLMClient

import workflow.evaluators as evaluators_module
import workflow.nodes as nodes_module
import core.reconstruction.generator as generator_module
import core.tone_converter as tone_module
from core.tone_converter import ToneConverter
from core.llm.schemas import ClusterEval, NewsletterContent, NewsletterMeta, NewsletterEval, ToneResult
from workflow.graph import compile_workflow


DRAFT = {
    "title": "반도체 HBM 증설",
    "sentence": "삼성전자와 SK하이닉스가 HBM 생산을 확대한다.",
    "content": "삼성전자와 SK하이닉스가 HBM 생산 확대에 나섰다.",
    "keywords": ["삼성전자", "SK하이닉스", "HBM"],
    "categories": ["IT/과학"],
}

CASUAL = ToneResult(
    title="📰 반도체 HBM 증설",
    summary="삼성전자와 SK하이닉스가 HBM을 늘려요 ✅",
    content="📰 삼성전자와 SK하이닉스가 HBM 생산을 늘리고 있어요.",
    keywords=["삼성전자", "SK하이닉스", "HBM"],
)


def test_tone_prompt_carries_draft_sentence_when_draft_has_no_summary_key():
    fake = FakeLLMClient(results=[{"parsed": CASUAL}])

    ToneConverter(llm_client=fake).convert(dict(DRAFT))

    prompt = fake.calls[0]["messages"][0]["content"]
    assert f"요약: {DRAFT['sentence']}" in prompt


def test_converted_summary_is_returned_under_pipeline_sentence_key():
    fake = FakeLLMClient(results=[{"parsed": CASUAL}])

    converted = ToneConverter(llm_client=fake).convert(dict(DRAFT))

    assert converted["sentence"] == CASUAL.summary


def test_fallback_softener_keeps_sentence_when_llm_output_never_validates(monkeypatch):
    from config.settings import Settings

    monkeypatch.setattr(Settings, "MAX_RETRY_TONE_VALIDATION", 0)
    fake = FakeLLMClient(results=[{"parsed": ToneResult(title="", summary="", content="", keywords=[])}])

    converted = ToneConverter(llm_client=fake).convert(dict(DRAFT))

    # 폴백도 원본 sentence를 기반으로 해야 한다(제목으로 대체되면 안 됨)
    assert "HBM 생산을 확대" in converted["sentence"]


class _FakeDBConn:
    def cursor(self):
        raise AssertionError("임베딩이 없으므로 cursor()가 호출되면 안 된다")

    def commit(self):
        pass


def test_saved_newsletter_sentence_is_the_casual_summary(monkeypatch):
    judge = FakeLLMClient(results=[
        {"parsed": ClusterEval(decision="PASS", confidence=0.9, summary="", feedback="",
                               outlier_indices=[], sub_groups=[])},
        {"parsed": NewsletterEval(decision="PASS", score=8, feedback="", issues=[])},
    ])
    generator = FakeLLMClient(results=[
        {"parsed": NewsletterContent(content=DRAFT["content"])},
        {"parsed": NewsletterMeta(title=DRAFT["title"], sentence=DRAFT["sentence"],
                                  keywords=DRAFT["keywords"], categories=DRAFT["categories"])},
    ])
    tone = FakeLLMClient(results=[{"parsed": CASUAL}])
    monkeypatch.setattr(evaluators_module, "get_client", lambda role: judge)
    monkeypatch.setattr(generator_module, "get_client", lambda role: generator)
    monkeypatch.setattr(tone_module, "get_client", lambda role: tone)
    # 임베딩 노드는 torch 없이 실패를 삼키지만, torch가 있는 환경에서도 모델을 올리지 않게 막는다
    monkeypatch.setattr(nodes_module, "get_shared_embedder", lambda: (_ for _ in ()).throw(RuntimeError("no model")))

    saved = {}

    def fake_save(conn, article_ids, newsletter, run_id=None, generation_history=None):
        saved["newsletter"] = newsletter
        return 1

    monkeypatch.setattr(nodes_module, "get_connection", lambda: _FakeDBConn())
    monkeypatch.setattr(nodes_module, "release_connection", lambda conn: None)
    monkeypatch.setattr(nodes_module, "save_news_letter", fake_save)

    state = {
        "run_id": 1,
        "all_cluster_groups": {1: [10, 11, 12]},
        "all_cluster_ids": [1],
        "current_cluster_index": 0,
        "data": {
            "ids": [10, 11, 12],
            "titles": ["a", "b", "c"],
            "contents": ["본문 " * 30] * 3,
            "press_names": ["p1", "p2", "p3"],
        },
        "completed_newsletters": [],
        "failed_clusters": [],
        "skipped_clusters": [],
    }
    compile_workflow().invoke(state)

    assert saved["newsletter"]["sentence"] == CASUAL.summary
    assert saved["newsletter"]["content"] == CASUAL.content
