import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import numpy as np


def test_clustering_stats_appended_to_cluster_log_passed_to_create_new_batch():
    """Stage5_NewsletterGeneration은 create_new_batch()에 넘기는 cluster_log에
    'clustering_stats' 항목(기사 수, 클러스터 수, noise 비율, 유효 파라미터)을
    추가해야 한다."""
    from pipeline.stages import Stage5_NewsletterGeneration

    fake_clusterer = MagicMock()
    fake_clusterer.cluster_news.return_value = {101: [1, 2, 3], 102: [4, 5]}
    fake_clusterer.get_clustered_articles.return_value = {"dummy": "data"}
    # 1차 HDBSCAN 라벨: 5개 중 2개가 noise(-1)
    fake_clusterer.labels_ = np.array([0, 0, 0, -1, -1])

    fake_app = MagicMock()
    fake_app.invoke.return_value = {"completed_newsletters": [1]}

    settings = MagicMock()
    settings.HDBSCAN_MIN_CLUSTER_SIZE = 3
    settings.HDBSCAN_MIN_SAMPLES = 2
    settings.MIN_NEWSLETTER_TARGET = 0
    settings.CLUSTER_LOOKBACK_HOURS = 24

    with patch("core.clusterer.NewsClusterer", return_value=fake_clusterer), \
         patch("workflow.graph.compile_workflow", return_value=fake_app), \
         patch("core.llm_metrics.get_metrics_collector", return_value=MagicMock()), \
         patch("db.batch_manager.create_new_batch", return_value=7) as mock_create_batch:

        stage = Stage5_NewsletterGeneration(settings=settings)
        stage.execute(min_cluster_size=3, min_samples=2)

    assert mock_create_batch.called
    (cluster_log,), _ = mock_create_batch.call_args

    assert "clustering_stats" in cluster_log
    stats = cluster_log["clustering_stats"]
    assert stats["n_articles"] == 5
    assert stats["n_clusters"] == 2
    assert stats["noise_ratio"] == 2 / 5
    assert stats["effective_params"]["min_cluster_size"] == 3
    assert stats["effective_params"]["min_samples"] == 2

    # 원래 클러스터 그룹 정보도 그대로 남아있어야 한다 (완전히 대체하지 않고 append)
    assert cluster_log[101] == [1, 2, 3]
    assert cluster_log[102] == [4, 5]


def test_clustering_stats_do_not_mutate_original_clusters_dict():
    """cluster_log에 통계를 추가할 때 원본 clusters 딕셔너리(all_cluster_groups로도
    재사용됨)를 변형하면 안 된다."""
    from pipeline.stages import Stage5_NewsletterGeneration

    original_clusters = {201: [10, 11]}
    fake_clusterer = MagicMock()
    fake_clusterer.cluster_news.return_value = original_clusters
    fake_clusterer.get_clustered_articles.return_value = {"dummy": "data"}
    fake_clusterer.labels_ = np.array([0, 0])

    fake_app = MagicMock()
    fake_app.invoke.return_value = {"completed_newsletters": [1]}

    settings = MagicMock()
    settings.HDBSCAN_MIN_CLUSTER_SIZE = 3
    settings.HDBSCAN_MIN_SAMPLES = 2
    settings.MIN_NEWSLETTER_TARGET = 0
    settings.CLUSTER_LOOKBACK_HOURS = 24

    with patch("core.clusterer.NewsClusterer", return_value=fake_clusterer), \
         patch("workflow.graph.compile_workflow", return_value=fake_app), \
         patch("core.llm_metrics.get_metrics_collector", return_value=MagicMock()), \
         patch("db.batch_manager.create_new_batch", return_value=9):

        stage = Stage5_NewsletterGeneration(settings=settings)
        stage.execute()

    assert "clustering_stats" not in original_clusters
    assert original_clusters == {201: [10, 11]}


def test_clustering_stats_computation_never_raises_when_labels_unavailable():
    """clusterer.labels_가 없거나 비정상이어도 통계 계산이 파이프라인을 죽이면 안 된다."""
    from pipeline.stages import _compute_clustering_stats

    clusterer_without_labels = MagicMock(spec=[])  # labels_ 속성 자체가 없음
    stats = _compute_clustering_stats(clusterer_without_labels, {1: [1]}, {"min_cluster_size": 3})

    assert stats["n_clusters"] == 1
    assert stats["n_articles"] is None
    assert stats["noise_ratio"] is None
