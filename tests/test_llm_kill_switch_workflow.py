# LangGraph 노드/서브그래프가 LLM 킬 스위치 아래에서 정상적으로("우아하게") 저하되는지
# 확인한다. 진짜(레지스트리가 만드는) OpenAICompatLLMClient를 그대로 쓰되, 실제
# API 키 대신 형식만 맞춘 더미 키를 넣고 LLM_KILL_SWITCH=1을 켜서, 네트워크 호출
# 시도 자체가 kill switch에서 차단되는지까지 end-to-end로 검증한다.
import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import pytest

import core.llm.registry as registry
import workflow.nodes as nodes_module
from workflow.nodes import generate_newsletter, evaluate_newsletter, convert_tone_node
from workflow.graph import compile_workflow


ARTICLES = [
    {"id": 10, "title": "삼성전자 HBM 증설 발표", "press_name": "동아일보", "content": "삼성전자가 HBM 생산을 확대한다. " * 20},
    {"id": 11, "title": "SK하이닉스도 투자 확대", "press_name": "한국경제", "content": "SK하이닉스도 투자를 늘린다. " * 20},
]


@pytest.fixture(autouse=True)
def _kill_switch_env_with_dummy_keys(monkeypatch):
    """레지스트리가 실제 OpenAICompatLLMClient를 만들 수 있도록 형식만 맞춘 더미
    키를 채워둔다 - kill switch가 네트워크 호출 자체를 막으므로 이 키들로 실제
    요청이 나가는 일은 없어야 한다(각 테스트에서 확인)."""
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("UPSTAGE_API_KEY", "test-upstage-key")
    registry.reset_registry()
    yield
    registry.reset_registry()


def test_generate_newsletter_node_falls_back_to_local_heuristic_under_kill_switch():
    state = {
        "current_articles": ARTICLES,
        "newsletter_feedback": None,
        "newsletter_retry_count": 0,
        "generation_history": {"attempts": []},
    }

    result = generate_newsletter(state)

    # NewsReconstructor의 로컬 휴리스틱 폴백(fallback_content/fallback_meta)은
    # LLM 실패 사유와 무관하게 항상 비어있지 않은 초안을 만든다 - 노드 자체가
    # 예외를 내며 죽지 않는다는 것이 핵심.
    draft = result["newsletter_draft"]
    assert draft is not None
    assert draft.get("content")
    assert draft.get("title")


def test_evaluate_newsletter_node_treats_kill_switch_as_failure():
    draft = {
        "title": "임시 제목",
        "sentence": "임시 요약",
        "content": "임시 본문",
        "keywords": [],
        "categories": [],
    }
    state = {"newsletter_draft": draft, "current_articles": ARTICLES, "newsletter_retry_count": 0}

    result = evaluate_newsletter(state)

    assert result["newsletter_eval"]["decision"] == "FAIL"


def test_convert_tone_node_falls_back_to_deterministic_softener_under_kill_switch():
    draft = {
        "title": "삼성전자 HBM 증설 발표했습니다",
        "sentence": "요약입니다",
        "content": "삼성전자가 HBM 생산을 확대하기로 했습니다.",
        "keywords": ["삼성전자", "HBM"],
    }
    state = {"newsletter_draft": draft}

    result = convert_tone_node(state)

    converted = result["converted_newsletter"]
    # ToneConverter._fallback_convert()가 붙이는 결정론적 마커
    assert converted["title"].startswith("📰")
    assert "삼성전자" in converted["content"]


def test_subgraph_ends_cleanly_without_saving_when_kill_switch_active(monkeypatch):
    def _fail_if_called(*_a, **_k):
        raise AssertionError("kill switch가 켜져 있는데 DB 저장 경로가 호출됐다")

    monkeypatch.setattr(nodes_module, "get_connection", _fail_if_called)
    monkeypatch.setattr(nodes_module, "release_connection", _fail_if_called)
    monkeypatch.setattr(nodes_module, "save_news_letter", _fail_if_called)

    initial_state = {
        "run_id": 1,
        "all_cluster_groups": {1: [10, 11]},
        "all_cluster_ids": [1],
        "current_cluster_index": 0,
        "data": {
            "ids": [10, 11],
            "titles": [a["title"] for a in ARTICLES],
            "contents": [a["content"] for a in ARTICLES],
            "press_names": [a["press_name"] for a in ARTICLES],
        },
        "completed_newsletters": [],
        "failed_clusters": [],
        "skipped_clusters": [],
    }

    app = compile_workflow()
    final_state = app.invoke(initial_state)  # 예외 없이 끝나야 함

    assert final_state.get("completed_newsletters") == []
    # 클러스터 평가(judge)가 kill switch로 즉시 FAIL 처리되고, outlier/sub_groups가
    # 없으므로 재시도 없이 곧바로 스킵된다 (workflow/nodes.py::handle_cluster_eval_failure)
    assert final_state.get("skipped_clusters") == [1]
    assert final_state.get("cluster_eval", {}).get("decision") == "FAIL"
