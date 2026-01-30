"""
Pytest configuration and fixtures for AI Workspace tests
"""
import os
import sys
from typing import Dict, List, Generator
from unittest.mock import MagicMock, patch

import pytest
import numpy as np

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def sample_articles() -> List[Dict]:
    """Sample article data for testing"""
    return [
        {
            "raw_news_id": 1,
            "title": "삼성전자, 반도체 투자 확대 발표",
            "press_name": "한국경제",
            "content": "삼성전자가 반도체 분야에 대규모 투자를 발표했다. " * 20
        },
        {
            "raw_news_id": 2,
            "title": "SK하이닉스, HBM 생산량 증대",
            "press_name": "매일경제",
            "content": "SK하이닉스가 HBM 메모리 생산량을 크게 늘린다고 밝혔다. " * 20
        },
        {
            "raw_news_id": 3,
            "title": "반도체 업계, AI 수요 급증에 호황",
            "press_name": "동아일보",
            "content": "AI 반도체 수요가 급증하며 반도체 업계가 호황을 맞고 있다. " * 20
        },
    ]


@pytest.fixture
def sample_embeddings() -> np.ndarray:
    """Sample embedding vectors for testing"""
    np.random.seed(42)
    return np.random.randn(10, 1024).astype(np.float32)


@pytest.fixture
def sample_cluster_eval_result() -> Dict:
    """Sample cluster evaluation result"""
    return {
        "decision": "PASS",
        "confidence": 0.9,
        "summary": "반도체 업계 투자 및 생산 확대 관련 뉴스",
        "feedback": "",
        "outlier_indices": []
    }


@pytest.fixture
def sample_newsletter_draft() -> Dict:
    """Sample newsletter draft"""
    return {
        "title": "반도체 업계 투자 확대",
        "sentence": "삼성, SK 등 반도체 기업들이 대규모 투자를 발표했다",
        "content": "반도체 업계가 AI 수요 급증에 대응하여 투자를 확대하고 있다. " * 30,
        "keywords": ["삼성전자", "SK하이닉스", "반도체", "AI", "투자"],
        "categories": ["경제", "IT과학"]
    }


@pytest.fixture
def mock_llm_client():
    """Mock LLM client for testing"""
    with patch('core.llm_client.get_llm_client') as mock:
        mock_instance = MagicMock()
        mock.return_value = mock_instance

        # Mock chat completion - default response
        mock_instance.chat_completion.return_value = '{"decision": "PASS", "score": 8}'

        yield mock_instance


@pytest.fixture
def mock_db_connection():
    """Mock database connection for testing"""
    with patch('db.connection.get_connection') as mock:
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock.return_value = mock_conn

        yield mock_conn, mock_cursor


@pytest.fixture
def env_setup() -> Generator:
    """Setup test environment variables"""
    original_env = os.environ.copy()

    os.environ["ENV"] = "dev"
    os.environ["DB_NAME"] = "test_db"
    os.environ["LLM_PROVIDER"] = "naver"
    os.environ["NCP_CLOVASTUDIO_API_KEY"] = "nv-test-key"  # Apps method (nv- prefix)

    yield

    os.environ.clear()
    os.environ.update(original_env)
