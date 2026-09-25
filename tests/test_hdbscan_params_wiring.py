import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

import numpy as np


def _fake_data():
    return {
        "ids": np.array([1, 2, 3]),
        "titles": ["a", "b", "c"],
        "embeddings": np.array([[0.1, 0.2], [0.1, 0.2], [0.9, 0.9]]),
        "press_names": ["p", "p", "p"],
        "contents": ["c1", "c2", "c3"],
    }


def test_cluster_news_non_default_params_reach_hdbscan_hdbscan():
    """NewsClusterer.cluster_news(min_cluster_size, min_samples)로 넘긴 비-기본값이
    실제로 hdbscan.HDBSCAN(...) 생성자까지 전달돼야 한다 (이전에는 인자가 무시되고
    __init__ 시점 기본값 3/2가 항상 쓰였음)."""
    from core.clustering.hdbscan_clusterer import NewsClusterer

    fake_hdbscan_instance = MagicMock()
    fake_hdbscan_instance.fit_predict.return_value = np.array([0, 0, -1])

    with patch("core.clustering.hdbscan_clusterer.hdbscan.HDBSCAN", return_value=fake_hdbscan_instance) as mock_hdbscan_cls, \
         patch.object(NewsClusterer, "_load_data_from_db", return_value=_fake_data()), \
         patch("core.clustering.hdbscan_clusterer.decide_split_v2") as mock_split:

        mock_split.return_value = MagicMock(should_split=False, debug={})

        clusterer = NewsClusterer()  # 기본값(3, 2)으로 생성
        clusterer.cluster_news(min_cluster_size=7, min_samples=5)

    assert mock_hdbscan_cls.called
    _, call_kwargs = mock_hdbscan_cls.call_args
    assert call_kwargs["min_cluster_size"] == 7
    assert call_kwargs["min_samples"] == 5


def test_cluster_news_defaults_to_instance_values_when_not_overridden():
    """cluster_news()를 인자 없이 호출하면 __init__에서 설정된 인스턴스 값이 유지된다."""
    from core.clustering.hdbscan_clusterer import NewsClusterer

    fake_hdbscan_instance = MagicMock()
    fake_hdbscan_instance.fit_predict.return_value = np.array([0, 0, -1])

    with patch("core.clustering.hdbscan_clusterer.hdbscan.HDBSCAN", return_value=fake_hdbscan_instance) as mock_hdbscan_cls, \
         patch.object(NewsClusterer, "_load_data_from_db", return_value=_fake_data()), \
         patch("core.clustering.hdbscan_clusterer.decide_split_v2") as mock_split:

        mock_split.return_value = MagicMock(should_split=False, debug={})

        clusterer = NewsClusterer(min_cluster_size=11, min_samples=9)
        clusterer.cluster_news()

    _, call_kwargs = mock_hdbscan_cls.call_args
    assert call_kwargs["min_cluster_size"] == 11
    assert call_kwargs["min_samples"] == 9


def test_stage5_resolves_effective_params_cli_over_settings_over_default():
    """Stage5_NewsletterGeneration.execute()는 CLI(명시적 인자) > Settings > 기본값
    순서로 유효 파라미터를 계산해서 NewsClusterer.cluster_news에 넘겨야 한다."""
    from pipeline.stages import Stage5_NewsletterGeneration

    fake_clusterer = MagicMock()
    fake_clusterer.cluster_news.return_value = {1: ["a"]}
    fake_clusterer.get_clustered_articles.return_value = {"dummy": "data"}
    fake_clusterer.labels_ = np.array([0, -1])

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
         patch("db.batch_manager.create_new_batch", return_value=1):

        # CLI에서 값을 명시적으로 넘기면 Settings보다 우선해야 한다
        stage = Stage5_NewsletterGeneration(settings=settings)
        stage.execute(min_cluster_size=99, min_samples=42)

    _, call_kwargs = fake_clusterer.cluster_news.call_args
    assert call_kwargs["min_cluster_size"] == 99
    assert call_kwargs["min_samples"] == 42


def test_stage5_falls_back_to_settings_when_cli_not_given():
    """CLI에서 값을 넘기지 않으면(None) Settings 값이 사용돼야 한다."""
    from pipeline.stages import Stage5_NewsletterGeneration

    fake_clusterer = MagicMock()
    fake_clusterer.cluster_news.return_value = {1: ["a"]}
    fake_clusterer.get_clustered_articles.return_value = {"dummy": "data"}
    fake_clusterer.labels_ = np.array([0, -1])

    fake_app = MagicMock()
    fake_app.invoke.return_value = {"completed_newsletters": [1]}

    settings = MagicMock()
    settings.HDBSCAN_MIN_CLUSTER_SIZE = 15
    settings.HDBSCAN_MIN_SAMPLES = 6
    settings.MIN_NEWSLETTER_TARGET = 0
    settings.CLUSTER_LOOKBACK_HOURS = 48

    with patch("core.clusterer.NewsClusterer", return_value=fake_clusterer), \
         patch("workflow.graph.compile_workflow", return_value=fake_app), \
         patch("core.llm_metrics.get_metrics_collector", return_value=MagicMock()), \
         patch("db.batch_manager.create_new_batch", return_value=1):

        stage = Stage5_NewsletterGeneration(settings=settings)
        stage.execute()  # min_cluster_size/min_samples/lookback_hours 모두 미지정

    _, call_kwargs = fake_clusterer.cluster_news.call_args
    assert call_kwargs["min_cluster_size"] == 15
    assert call_kwargs["min_samples"] == 6
    assert call_kwargs["lookback_hours"] == 48
