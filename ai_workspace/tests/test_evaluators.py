"""
Tests for LLM Evaluators
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from workflow.evaluators import ClusterEvaluator, NewsletterEvaluator


class TestClusterEvaluator:
    """Test cases for ClusterEvaluator"""

    @pytest.fixture
    def mock_llm_client(self):
        """Mock LLM client"""
        with patch('core.llm_client.get_llm_client') as mock:
            mock_instance = MagicMock()
            mock.return_value = mock_instance
            yield mock_instance

    def test_evaluate_empty_cluster(self, mock_llm_client):
        """Test evaluation with empty cluster"""
        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            evaluator = ClusterEvaluator()
            result = evaluator.evaluate([])

            assert result['decision'] == 'FAIL'
            assert result['confidence'] == 0.0
            assert 'Empty cluster' in result['feedback']

    def test_evaluate_pass_cluster(self, mock_llm_client, sample_articles):
        """Test evaluation that passes"""
        # Mock response
        mock_llm_client.chat_completion.return_value = json.dumps({
            "decision": "PASS",
            "confidence": 0.9,
            "summary": "반도체 관련 뉴스",
            "feedback": "",
            "outlier_indices": []
        })

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            evaluator = ClusterEvaluator()
            evaluator.client = mock_llm_client

            result = evaluator.evaluate(sample_articles)

            assert result['decision'] == 'PASS'
            assert result['confidence'] == 0.9

    def test_evaluate_fail_cluster(self, mock_llm_client, sample_articles):
        """Test evaluation that fails"""
        mock_llm_client.chat_completion.return_value = json.dumps({
            "decision": "FAIL",
            "confidence": 0.8,
            "summary": "",
            "feedback": "Articles discuss unrelated topics",
            "outlier_indices": [2]
        })

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            evaluator = ClusterEvaluator()
            evaluator.client = mock_llm_client

            result = evaluator.evaluate(sample_articles)

            assert result['decision'] == 'FAIL'
            assert len(result['outlier_indices']) == 1


class TestNewsletterEvaluator:
    """Test cases for NewsletterEvaluator"""

    @pytest.fixture
    def mock_llm_client(self):
        """Mock LLM client"""
        with patch('core.llm_client.get_llm_client') as mock:
            mock_instance = MagicMock()
            mock.return_value = mock_instance
            yield mock_instance

    def test_evaluate_empty_newsletter(self, mock_llm_client):
        """Test evaluation with empty newsletter"""
        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            evaluator = NewsletterEvaluator()
            result = evaluator.evaluate(None, [])

            assert result['decision'] == 'FAIL'
            assert result['score'] == 0
            assert 'Empty newsletter' in result['feedback']

    def test_evaluate_pass_newsletter(self, mock_llm_client, sample_newsletter_draft, sample_articles):
        """Test evaluation that passes"""
        mock_llm_client.chat_completion.return_value = json.dumps({
            "decision": "PASS",
            "score": 8,
            "feedback": "Good quality newsletter",
            "issues": []
        })

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            evaluator = NewsletterEvaluator()
            evaluator.client = mock_llm_client

            result = evaluator.evaluate(sample_newsletter_draft, sample_articles)

            assert result['decision'] == 'PASS'
            assert result['score'] == 8

    def test_evaluate_fail_newsletter(self, mock_llm_client, sample_newsletter_draft, sample_articles):
        """Test evaluation that fails"""
        mock_llm_client.chat_completion.return_value = json.dumps({
            "decision": "FAIL",
            "score": 3,
            "feedback": "Missing key facts",
            "issues": ["Incomplete coverage", "Title too long"]
        })

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            evaluator = NewsletterEvaluator()
            evaluator.client = mock_llm_client

            result = evaluator.evaluate(sample_newsletter_draft, sample_articles)

            assert result['decision'] == 'FAIL'
            assert result['score'] == 3
            assert len(result['issues']) == 2
