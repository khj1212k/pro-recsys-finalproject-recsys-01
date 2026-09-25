import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from workflow.nodes import handle_cluster_eval_failure


def test_sub_group_path_increments_retry_count():
    state = {
        "current_cluster_id": 1,
        "current_articles": [{"id": i} for i in range(6)],
        "cluster_eval": {"sub_groups": [[0, 1, 2], [3, 4, 5]]},
        "cluster_retry_count": 0,
    }
    result = handle_cluster_eval_failure(state)
    assert result.get("cluster_retry_count") == 1


def test_outlier_removal_path_increments_retry_count():
    state = {
        "current_cluster_id": 2,
        "current_articles": [{"id": i} for i in range(5)],
        "cluster_eval": {"outlier_indices": [0]},
        "cluster_retry_count": 1,
    }
    result = handle_cluster_eval_failure(state)
    assert result.get("cluster_retry_count") == 2


def test_max_retries_reached_skips_cluster_without_incrementing():
    state = {
        "current_cluster_id": 3,
        "current_articles": [{"id": i} for i in range(5)],
        "cluster_eval": {"outlier_indices": [0]},
        "cluster_retry_count": 2,  # 이미 MAX_RETRIES(2)에 도달
        "skipped_clusters": [],
    }
    result = handle_cluster_eval_failure(state)
    assert result.get("skipped_clusters") == [3]


def test_max_retries_reached_on_sub_group_path_skips_without_split():
    """FIX_LOG #8 CORRECTION: MAX_RETRIES 가드가 서브그룹 분기보다 먼저 평가되어야
    한다. 수정 전에는 이 가드가 서브그룹 분기 아래에 있어, sub_groups가 있는 실패는
    retry_count와 무관하게 항상 분할을 시도해 가드가 무력화됐다."""
    state = {
        "current_cluster_id": 4,
        "current_articles": [{"id": i} for i in range(6)],
        "cluster_eval": {"sub_groups": [[0, 1, 2], [3, 4, 5]]},
        "cluster_retry_count": 2,  # 이미 MAX_RETRIES(2)에 도달
        "skipped_clusters": [],
    }
    result = handle_cluster_eval_failure(state)
    assert result.get("skipped_clusters") == [4]
    assert "current_articles" not in result
