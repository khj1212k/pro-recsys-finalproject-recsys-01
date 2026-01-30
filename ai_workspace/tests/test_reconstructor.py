"""
Tests for NewsReconstructor
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from core.reconstructor import NewsReconstructor


class TestNewsReconstructor:
    """Test cases for NewsReconstructor"""

    @pytest.fixture
    def mock_llm_client(self):
        """Mock LLM client"""
        with patch('core.llm_client.get_llm_client') as mock:
            mock_instance = MagicMock()
            mock.return_value = mock_instance
            yield mock_instance

    def test_reconstruct_empty_articles(self, mock_llm_client):
        """Test reconstruction with empty articles"""
        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            reconstructor = NewsReconstructor()
            result = reconstructor.reconstruct([])

            assert result is None

    def test_reconstruct_success(self, mock_llm_client, sample_articles):
        """Test successful reconstruction"""
        expected_result = {
            "title": "반도체 투자 확대",
            "sentence": "삼성, SK 등이 대규모 투자 발표",
            "content": "반도체 업계가 투자를 확대하고 있다.",
            "keywords": ["삼성전자", "반도체", "투자"],
            "categories": ["경제", "IT과학"]
        }

        mock_llm_client.chat_completion.return_value = json.dumps(expected_result)

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            reconstructor = NewsReconstructor()
            reconstructor.client = mock_llm_client

            result = reconstructor.reconstruct(sample_articles)

            assert result is not None
            assert result['title'] == expected_result['title']
            assert 'keywords' in result
            assert 'categories' in result

    def test_reconstruct_with_feedback(self, mock_llm_client, sample_articles):
        """Test reconstruction with feedback"""
        expected_result = {
            "title": "개선된 제목",
            "sentence": "개선된 요약",
            "content": "개선된 내용",
            "keywords": ["키워드"],
            "categories": ["경제"]
        }

        mock_llm_client.chat_completion.return_value = json.dumps(expected_result)

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            reconstructor = NewsReconstructor()
            reconstructor.client = mock_llm_client

            result = reconstructor.reconstruct(
                sample_articles,
                feedback="제목이 너무 깁니다. 15자 이내로 줄여주세요."
            )

            assert result is not None
            # Verify that the API was called (feedback is incorporated)
            mock_llm_client.chat_completion.assert_called_once()

    def test_reconstruct_limits_articles(self, mock_llm_client, sample_articles):
        """Test that articles are limited to MAX_ARTICLES"""
        # Create more than 10 articles
        many_articles = sample_articles * 5  # 15 articles

        expected_result = {
            "title": "제목",
            "sentence": "요약",
            "content": "내용",
            "keywords": ["키워드"],
            "categories": ["경제"]
        }

        mock_llm_client.chat_completion.return_value = json.dumps(expected_result)

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            reconstructor = NewsReconstructor()
            reconstructor.client = mock_llm_client

            result = reconstructor.reconstruct(many_articles)

            # Should still work with many articles
            assert result is not None

    def test_reconstruct_api_error(self, mock_llm_client, sample_articles):
        """Test handling of API error"""
        mock_llm_client.chat_completion.side_effect = Exception("API Error")

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            reconstructor = NewsReconstructor()
            reconstructor.client = mock_llm_client

            result = reconstructor.reconstruct(sample_articles)

            assert result is None

    def test_reconstruct_json_in_markdown(self, mock_llm_client, sample_articles):
        """Test handling of JSON wrapped in markdown code block"""
        expected_result = {
            "title": "테스트 제목",
            "sentence": "테스트 요약",
            "content": "테스트 내용",
            "keywords": ["키워드"],
            "categories": ["경제"]
        }

        # Return JSON wrapped in markdown code block (common with HyperCLOVA)
        mock_llm_client.chat_completion.return_value = f"```json\n{json.dumps(expected_result)}\n```"

        with patch.dict('os.environ', {
            'NCP_CLOVASTUDIO_API_KEY': 'nv-test-key'
        }):
            reconstructor = NewsReconstructor()
            reconstructor.client = mock_llm_client

            result = reconstructor.reconstruct(sample_articles)

            assert result is not None
            assert result['title'] == expected_result['title']
