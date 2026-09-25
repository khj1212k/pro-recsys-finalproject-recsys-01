import sys
import os
import numpy as np

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from workflow.nodes import initialize_cluster_processing


def test_initialize_cluster_processing_includes_press_name_for_each_article():
    """NewsletterEvaluator(workflow/evaluators.py)의 프롬프트는
    article.get('press_name', ...)를 참조하는데, 이 노드가 만드는 article dict에
    press_name이 빠져 있어 평가 프롬프트가 항상 빈 언론사명을 받고 있었다."""
    state = {
        "current_cluster_index": 0,
        "all_cluster_ids": [1],
        "all_cluster_groups": {1: [101, 102]},
        "data": {
            "ids": np.array([101, 102, 103]),
            "titles": ["제목1", "제목2", "제목3"],
            "contents": ["본문1", "본문2", "본문3"],
            "press_names": ["동아일보", "경향신문", "한국경제"],
        },
    }

    result = initialize_cluster_processing(state)

    articles = result["current_articles"]
    assert len(articles) == 2
    assert articles[0]["press_name"] == "동아일보"
    assert articles[1]["press_name"] == "경향신문"
    # 기존에 있던 필드도 계속 채워져야 한다
    assert articles[0]["title"] == "제목1"
    assert articles[0]["content"] == "본문1"


def test_initialize_cluster_processing_press_name_aligns_with_list_backed_ids():
    """data['ids']가 (np.ndarray가 아니라) 일반 list인 경로도 동일하게 동작해야 한다."""
    state = {
        "current_cluster_index": 0,
        "all_cluster_ids": [1],
        "all_cluster_groups": {1: [201]},
        "data": {
            "ids": [200, 201, 202],
            "titles": ["a", "b", "c"],
            "contents": ["ca", "cb", "cc"],
            "press_names": ["세계일보", "국민일보", "매일경제"],
        },
    }

    result = initialize_cluster_processing(state)

    assert result["current_articles"][0]["press_name"] == "국민일보"
