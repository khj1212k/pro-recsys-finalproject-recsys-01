import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import workflow.evaluators as evaluators_module
from workflow.graph import compile_workflow
from config.settings import Settings


class _FakeSubGroupClusterEvaluator:
    """항상 FAIL을 반환하고, 현재 기사 목록 전체를 커버하는 두 개의 서브그룹을
    제안하는 가짜 평가기(sub-group-split 경로 반복 검증용)."""

    call_count = 0

    def __init__(self, provider=None):
        pass

    def evaluate(self, articles):
        type(self).call_count += 1
        n = len(articles)
        # 큰 그룹(n-1개)과 작은 그룹(1개)으로 나눠, "가장 큰 서브그룹 선택" 경로가
        # 매번 한 개씩만 줄어들며 재시도 상한까지 반복되도록 함. 합집합은 항상
        # 현재 기사 인덱스 전체를 커버한다.
        return {
            "decision": "FAIL",
            "confidence": 0.9,
            "summary": "fake",
            "feedback": "always fail (sub_groups)",
            "outlier_indices": [],
            "sub_groups": [list(range(0, n - 1)), [n - 1]],
        }


class _FakeOutlierClusterEvaluator:
    """항상 FAIL을 반환하고, 매번 첫 기사를 아웃라이어로 지목하는 가짜 평가기
    (아웃라이어 제거 경로 반복 검증용)."""

    call_count = 0

    def __init__(self, provider=None):
        pass

    def evaluate(self, articles):
        type(self).call_count += 1
        return {
            "decision": "FAIL",
            "confidence": 0.9,
            "summary": "fake",
            "feedback": "always fail (outliers)",
            "outlier_indices": [0],
            "sub_groups": [],
        }


def _build_state(cluster_id, article_ids):
    return {
        "run_id": 1,
        "all_cluster_groups": {cluster_id: article_ids},
        "all_cluster_ids": [cluster_id],
        "current_cluster_index": 0,
        "data": {
            "ids": list(article_ids),
            "titles": [f"title-{i}" for i in article_ids],
            "contents": [f"content-{i}" for i in article_ids],
        },
        "completed_newsletters": [],
        "failed_clusters": [],
        "skipped_clusters": [],
    }


def test_sub_group_path_respects_max_retries_and_terminates(monkeypatch):
    """CORRECTION #8: sub_groups가 매번 존재해도, 전체 그래프 실행에서
    ClusterEvaluator는 MAX_RETRY_CLUSTER_EVAL + 1번(최초 시도 + 재시도) 이하로만
    호출되어야 하고, GraphRecursionError 없이 정상 종료돼야 한다."""
    _FakeSubGroupClusterEvaluator.call_count = 0
    monkeypatch.setattr(evaluators_module, "ClusterEvaluator", _FakeSubGroupClusterEvaluator)

    app = compile_workflow()
    initial_state = _build_state(cluster_id=1, article_ids=[0, 1, 2, 3, 4, 5])

    final_state = app.invoke(initial_state)

    assert _FakeSubGroupClusterEvaluator.call_count <= Settings.MAX_RETRY_CLUSTER_EVAL + 1
    assert _FakeSubGroupClusterEvaluator.call_count == Settings.MAX_RETRY_CLUSTER_EVAL + 1
    assert final_state.get("skipped_clusters") == [1]


def test_outlier_path_respects_max_retries_and_terminates(monkeypatch):
    """CORRECTION #8: 아웃라이어 제거 경로 역시 동일한 가드를 거쳐 evaluator 호출
    횟수가 MAX_RETRY_CLUSTER_EVAL + 1을 넘지 않고, GraphRecursionError 없이
    정상 종료돼야 한다."""
    _FakeOutlierClusterEvaluator.call_count = 0
    monkeypatch.setattr(evaluators_module, "ClusterEvaluator", _FakeOutlierClusterEvaluator)

    app = compile_workflow()
    # 아웃라이어 경로는 원래 버그(cluster_retry_count를 아예 반환하지 않음)에서도
    # 기사 수 소진으로 결국 종료는 되므로, 재시도 상한 가드가 실제로 작동하는지
    # 검증하려면 "기사 수 소진"보다 훨씬 먼저 상한에 도달해야 할 만큼 충분히 많은
    # 기사로 시작해야 한다 (10개 → 버그 버전은 8번, 수정 버전은 3번 호출됨).
    initial_state = _build_state(cluster_id=2, article_ids=list(range(20, 30)))

    final_state = app.invoke(initial_state)

    assert _FakeOutlierClusterEvaluator.call_count <= Settings.MAX_RETRY_CLUSTER_EVAL + 1
    assert _FakeOutlierClusterEvaluator.call_count == Settings.MAX_RETRY_CLUSTER_EVAL + 1
    assert final_state.get("skipped_clusters") == [2]
