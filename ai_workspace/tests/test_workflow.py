"""
Tests for LangGraph Workflow
"""
import pytest
from unittest.mock import MagicMock, patch

from workflow.state import AgentState
from workflow.nodes import (
    initialize_cluster_processing,
    route_after_cluster_eval,
    route_after_newsletter_eval,
    route_after_cluster_fail
)


class TestWorkflowNodes:
    """Test cases for workflow node functions"""

    def test_initialize_cluster_processing(self, sample_articles):
        """Test cluster initialization"""
        state = {
            "current_cluster_id": 0,
            "all_cluster_groups": {0: [1, 2, 3]},
            "current_articles": sample_articles,
            "current_article_ids": [1, 2, 3],
            "data": {"ids": [1, 2, 3], "titles": [], "contents": [], "press_names": []}
        }

        result = initialize_cluster_processing(state)

        assert result["current_cluster_id"] == 0
        assert result["cluster_eval"] is None
        assert result["cluster_retry_count"] == 0
        assert result["newsletter_draft"] is None

    def test_route_after_cluster_eval_pass(self):
        """Test routing after cluster evaluation passes"""
        state = {
            "cluster_eval": {"decision": "PASS", "confidence": 0.9}
        }

        result = route_after_cluster_eval(state)

        assert result == "pass"

    def test_route_after_cluster_eval_fail(self):
        """Test routing after cluster evaluation fails"""
        state = {
            "cluster_eval": {"decision": "FAIL", "confidence": 0.5}
        }

        result = route_after_cluster_eval(state)

        assert result == "fail"

    def test_route_after_newsletter_eval_pass(self):
        """Test routing after newsletter evaluation passes"""
        state = {
            "newsletter_eval": {"decision": "PASS", "score": 8},
            "newsletter_retry_count": 1
        }

        result = route_after_newsletter_eval(state)

        assert result == "pass"

    def test_route_after_newsletter_eval_retry(self):
        """Test routing for newsletter retry"""
        state = {
            "newsletter_eval": {"decision": "FAIL", "score": 4},
            "newsletter_retry_count": 1
        }

        with patch('workflow.nodes.Settings') as mock_settings:
            mock_settings.MAX_RETRY_NEWSLETTER_EVAL = 3
            result = route_after_newsletter_eval(state)

            assert result == "retry"

    def test_route_after_newsletter_eval_max_retries(self):
        """Test routing when max retries reached"""
        state = {
            "newsletter_eval": {"decision": "FAIL", "score": 4},
            "newsletter_retry_count": 3
        }

        with patch('workflow.nodes.Settings') as mock_settings:
            mock_settings.MAX_RETRY_NEWSLETTER_EVAL = 3
            result = route_after_newsletter_eval(state)

            assert result == "max_retries"

    def test_route_after_cluster_fail_retry(self):
        """Test routing for cluster retry"""
        state = {
            "current_cluster_id": 0,
            "skipped_clusters": []
        }

        result = route_after_cluster_fail(state)

        assert result == "retry"

    def test_route_after_cluster_fail_end(self):
        """Test routing when cluster is skipped"""
        state = {
            "current_cluster_id": 0,
            "skipped_clusters": [0]
        }

        result = route_after_cluster_fail(state)

        assert result == "end"


class TestWorkflowGraph:
    """Test cases for workflow graph compilation"""

    def test_compile_workflow(self):
        """Test that workflow compiles successfully"""
        from workflow.graph import compile_workflow

        app = compile_workflow()

        assert app is not None

    def test_workflow_has_required_nodes(self):
        """Test that workflow has all required nodes"""
        from workflow.graph import create_newsletter_workflow

        workflow = create_newsletter_workflow()

        # Check that nodes exist
        assert "init_cluster" in workflow.nodes
        assert "eval_cluster" in workflow.nodes
        assert "generate_newsletter" in workflow.nodes
        assert "eval_newsletter" in workflow.nodes
        assert "save_newsletter" in workflow.nodes
