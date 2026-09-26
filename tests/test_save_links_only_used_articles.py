# 클러스터 평가가 아웃라이어를 빼거나(outlier_indices) 서브그룹 하나만 남기면(sub_groups)
# current_articles는 줄어들지만 current_article_ids는 원래 클러스터 전체로 남아 있었다.
# save 노드가 current_article_ids로 저장해서
# - 생성에 쓰이지 않은(판정이 "다른 사건"이라고 뺀) 기사까지 news_raw.news_letter_id가 채워지고
#   hdbscan_clusterer의 `news_letter_id IS NULL` 필터 때문에 다음 실행에서 영구 제외됐고
# - raw_news_count가 실제 출처 수보다 크게 기록됐다.
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
from core.llm.schemas import ClusterEval, NewsletterContent, NewsletterMeta, NewsletterEval, ToneResult
from workflow.graph import compile_workflow


class _FakeDBConn:
    def cursor(self):
        raise AssertionError("임베딩이 없으므로 cursor()가 호출되면 안 된다")

    def commit(self):
        pass


def _run(monkeypatch, first_cluster_eval):
    judge = FakeLLMClient(results=[
        {"parsed": first_cluster_eval},
        {"parsed": ClusterEval(decision="PASS", confidence=0.9, summary="", feedback="",
                               outlier_indices=[], sub_groups=[])},
        {"parsed": NewsletterEval(decision="PASS", score=8, feedback="", issues=[])},
    ])
    generator = FakeLLMClient(results=[
        {"parsed": NewsletterContent(content="본문 " * 20)},
        {"parsed": NewsletterMeta(title="제목", sentence="요약", keywords=["a"], categories=["사회"])},
    ])
    tone = FakeLLMClient(results=[
        {"parsed": ToneResult(title="📰 제목", summary="요약이에요", content="본문이에요", keywords=["a"])},
    ])
    monkeypatch.setattr(evaluators_module, "get_client", lambda role: judge)
    monkeypatch.setattr(generator_module, "get_client", lambda role: generator)
    monkeypatch.setattr(tone_module, "get_client", lambda role: tone)
    monkeypatch.setattr(nodes_module, "get_shared_embedder", lambda: (_ for _ in ()).throw(RuntimeError("no model")))

    saved = {}

    def fake_save(conn, article_ids, newsletter, run_id=None, generation_history=None):
        saved["article_ids"] = list(article_ids)
        return 7

    monkeypatch.setattr(nodes_module, "get_connection", lambda: _FakeDBConn())
    monkeypatch.setattr(nodes_module, "release_connection", lambda conn: None)
    monkeypatch.setattr(nodes_module, "save_news_letter", fake_save)

    state = {
        "run_id": 1,
        "all_cluster_groups": {1: [10, 11, 12, 13]},
        "all_cluster_ids": [1],
        "current_cluster_index": 0,
        "data": {
            "ids": [10, 11, 12, 13],
            "titles": ["a", "b", "c", "d"],
            "contents": ["본문 " * 30] * 4,
            "press_names": ["p1", "p2", "p3", "p4"],
        },
        "completed_newsletters": [],
        "failed_clusters": [],
        "skipped_clusters": [],
    }
    final = compile_workflow().invoke(state)
    return final, saved


def test_outlier_removed_by_cluster_eval_is_not_linked_to_the_newsletter(monkeypatch):
    final, saved = _run(monkeypatch, ClusterEval(
        decision="FAIL", confidence=0.4, summary="", feedback="기사 2는 다른 사건",
        outlier_indices=[2], sub_groups=[],
    ))

    assert final["completed_newsletters"] == [7]
    assert saved["article_ids"] == [10, 11, 13]


def test_only_the_kept_sub_group_is_linked_when_cluster_eval_splits(monkeypatch):
    final, saved = _run(monkeypatch, ClusterEval(
        decision="FAIL", confidence=0.4, summary="", feedback="두 사건",
        outlier_indices=[], sub_groups=[[0, 1, 3], [2]],
    ))

    assert final["completed_newsletters"] == [7]
    assert saved["article_ids"] == [10, 11, 13]
