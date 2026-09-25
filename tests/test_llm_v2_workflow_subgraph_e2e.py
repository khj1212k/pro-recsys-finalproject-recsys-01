# ADR 0005: LangGraph 단일 클러스터 서브그래프(workflow/graph.py)가 core/llm/ 기반
# ClusterEvaluator/NewsReconstructor/NewsletterEvaluator/ToneConverter로 정상적으로
# 이어지는지, 페이크 LLM 응답만으로 처음부터 끝까지(init -> eval -> generate ->
# eval -> embed -> tone -> save) 실행되는지 검증한다.
#
# 실제로 필요하지 않은 두 가지는 페이크로 대체한다:
# - 임베딩(NewsEmbedder)은 torch가 없는 환경에서 import 자체가 실패하는데,
#   embed_newsletter_node가 그 실패를 잡아 newsletter_embedding=None으로 넘어가므로
#   별도 패치 없이도 그래프가 끊기지 않는다(운영 CI에서는 torch가 있어 실제로 동작).
# - DB 저장(save_newsletter_to_db)은 Postgres가 필요하므로 get_connection/
#   release_connection/save_news_letter를 페이크로 대체한다(로컬 macOS에는 DB 없음).
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
from workflow.graph import compile_workflow
from core.llm.schemas import (
    ClusterEval, CriterionScores, NewsletterContent, NewsletterMeta, NewsletterEvalV2, ToneResult,
)


def _build_initial_state():
    return {
        "run_id": 1,
        "all_cluster_groups": {1: [10, 11, 12]},
        "all_cluster_ids": [1],
        "current_cluster_index": 0,
        "data": {
            "ids": [10, 11, 12],
            "titles": ["삼성전자 HBM 증설", "SK하이닉스 투자 확대", "메모리 업황 개선"],
            "contents": ["본문 내용 " * 30, "본문 내용 " * 30, "본문 내용 " * 30],
            "press_names": ["동아일보", "경향신문", "한국경제"],
        },
        "completed_newsletters": [],
        "failed_clusters": [],
        "skipped_clusters": [],
    }


class _FakeDBConn:
    def cursor(self):
        raise AssertionError("이 테스트의 뉴스레터에는 임베딩이 없어 cursor()가 호출되면 안 된다")

    def commit(self):
        pass


def test_subgraph_runs_end_to_end_with_fake_llm_clients_for_every_role(monkeypatch):
    # role="judge" - ClusterEvaluator와 NewsletterEvaluator가 순서대로 이 큐를 소비한다
    judge_client = FakeLLMClient(results=[
        {"parsed": ClusterEval(
            decision="PASS", confidence=0.92, summary="반도체 업황 개선",
            feedback="", outlier_indices=[], sub_groups=[],
        )},
        {"parsed": NewsletterEvalV2(
            scores=CriterionScores(faithfulness=5, coverage=4, coherence=4, style=4),
            unsupported_claims=[], feedback="",
        )},
    ])

    # role="generator" - NewsReconstructor의 Call#1(본문), Call#2(메타) 순서
    generator_client = FakeLLMClient(results=[
        {"parsed": NewsletterContent(content="삼성전자와 SK하이닉스가 HBM 생산 확대에 나섰다. " * 5)},
        {"parsed": NewsletterMeta(
            title="반도체 HBM 증설",
            sentence="메모리 반도체, 새 국면 맞이했어요",
            keywords=["삼성전자", "SK하이닉스", "HBM", "메모리", "반도체"],
            categories=["IT/과학"],
        )},
    ])

    # role="tone"
    tone_client = FakeLLMClient(results=[
        {"parsed": ToneResult(
            title="📰 반도체 HBM 증설",
            summary="메모리 반도체 업황, 이제 좋아진대요 ✅",
            content="📰 삼성전자와 SK하이닉스가 HBM 생산을 늘리고 있어요.",
            keywords=["삼성전자", "SK하이닉스", "HBM"],
        )},
    ])

    monkeypatch.setattr(evaluators_module, "get_client", lambda role: judge_client)
    monkeypatch.setattr(generator_module, "get_client", lambda role: generator_client)
    monkeypatch.setattr(tone_module, "get_client", lambda role: tone_client)

    saved = {}

    def fake_save_news_letter(conn, article_ids, newsletter, run_id=None, generation_history=None):
        saved["article_ids"] = article_ids
        saved["newsletter"] = newsletter
        saved["run_id"] = run_id
        return 999

    monkeypatch.setattr(nodes_module, "get_connection", lambda: _FakeDBConn())
    monkeypatch.setattr(nodes_module, "release_connection", lambda conn: None)
    monkeypatch.setattr(nodes_module, "save_news_letter", fake_save_news_letter)

    app = compile_workflow()
    final_state = app.invoke(_build_initial_state())

    # 클러스터/뉴스레터 평가 모두 큐를 정확히 하나씩만 소비하며 통과했어야 한다
    assert judge_client.call_count == 2
    assert generator_client.call_count == 2
    assert tone_client.call_count == 1

    assert final_state.get("failed_clusters") == []
    assert final_state.get("skipped_clusters") == []
    assert final_state.get("completed_newsletters") == [999]

    assert final_state["cluster_eval"]["decision"] == "PASS"
    assert final_state["newsletter_eval"]["decision"] == "PASS"
    assert final_state["newsletter_draft"]["title"] == "반도체 HBM 증설"
    assert final_state["converted_newsletter"]["title"] == "📰 반도체 HBM 증설"

    assert saved["article_ids"] == [10, 11, 12]
    assert saved["run_id"] == 1
    # 저장 시엔 변환된(부드러운) 문체가 우선 사용돼야 한다
    assert saved["newsletter"]["title"] == "📰 반도체 HBM 증설"
