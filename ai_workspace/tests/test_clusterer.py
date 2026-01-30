"""
Tests for NewsClusterer
"""
import pytest
import numpy as np

from core.clusterer import NewsClusterer, get_cluster_groups


class TestNewsClusterer:
    """Test cases for NewsClusterer class"""

    def test_init_default_params(self):
        """Test initialization with default parameters"""
        clusterer = NewsClusterer()
        assert clusterer.min_cluster_size == 3
        assert clusterer.min_samples == 2

    def test_init_custom_params(self):
        """Test initialization with custom parameters"""
        clusterer = NewsClusterer(min_cluster_size=5, min_samples=3)
        assert clusterer.min_cluster_size == 5
        assert clusterer.min_samples == 3

    def test_fit_predict_with_sample_data(self, sample_embeddings):
        """Test clustering with sample embeddings"""
        clusterer = NewsClusterer(min_cluster_size=2, min_samples=1)
        labels = clusterer.fit_predict(sample_embeddings)

        assert labels is not None
        assert len(labels) == len(sample_embeddings)
        assert clusterer.labels_ is not None

    def test_get_cluster_stats(self, sample_embeddings):
        """Test cluster statistics calculation"""
        clusterer = NewsClusterer(min_cluster_size=2, min_samples=1)
        clusterer.fit_predict(sample_embeddings)

        stats = clusterer.get_cluster_stats()

        assert 'n_clusters' in stats
        assert 'n_noise' in stats
        assert 'noise_ratio' in stats
        assert stats['n_noise'] >= 0

    def test_evaluate(self, sample_embeddings):
        """Test clustering evaluation"""
        clusterer = NewsClusterer(min_cluster_size=2, min_samples=1)
        clusterer.fit_predict(sample_embeddings)

        metrics = clusterer.evaluate(sample_embeddings)

        assert 'n_clusters' in metrics
        assert 'silhouette_score' in metrics


class TestGetClusterGroups:
    """Test cases for get_cluster_groups function"""

    def test_basic_grouping(self):
        """Test basic cluster grouping"""
        article_ids = np.array([1, 2, 3, 4, 5])
        labels = np.array([0, 0, 1, 1, -1])  # -1 is noise

        groups = get_cluster_groups(article_ids, labels)

        assert 0 in groups
        assert 1 in groups
        assert -1 not in groups  # Noise should be excluded
        assert groups[0] == [1, 2]
        assert groups[1] == [3, 4]

    def test_all_noise(self):
        """Test when all articles are noise"""
        article_ids = np.array([1, 2, 3])
        labels = np.array([-1, -1, -1])

        groups = get_cluster_groups(article_ids, labels)

        assert len(groups) == 0

    def test_single_cluster(self):
        """Test with single cluster"""
        article_ids = np.array([1, 2, 3, 4])
        labels = np.array([0, 0, 0, 0])

        groups = get_cluster_groups(article_ids, labels)

        assert len(groups) == 1
        assert groups[0] == [1, 2, 3, 4]
