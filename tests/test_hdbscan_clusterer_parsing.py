import sys
import os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from core.clustering.hdbscan_clusterer import parse_embedding
import numpy as np
import pytest


def test_parse_embedding_from_string():
    result = parse_embedding("[0.1, 0.2, 0.3]")
    np.testing.assert_allclose(result, np.array([0.1, 0.2, 0.3]))


def test_parse_embedding_from_list():
    result = parse_embedding([0.5, -0.5])
    np.testing.assert_allclose(result, np.array([0.5, -0.5]))


def test_parse_embedding_rejects_code_execution_string():
    # eval()이었다면 이 문자열이 실제로 os.system을 호출했을 것이다.
    # ast.literal_eval은 리터럴이 아닌 표현식에 ValueError를 던져야 한다.
    malicious = "__import__('os').system('echo pwned')"
    with pytest.raises((ValueError, SyntaxError)):
        parse_embedding(malicious)


def test_parse_embedding_from_pgvector_vector():
    # db.connection 풀 연결은 register_vector가 걸려 있어 vector 컬럼이 pgvector.Vector로 온다.
    # np.array(Vector)는 0차원 object 배열이 돼 HDBSCAN에서 TypeError로 죽었다.
    from pgvector import Vector

    result = parse_embedding(Vector([0.25, -0.5, 1.0]))
    assert result.dtype == np.float32 and result.shape == (3,)
    np.testing.assert_allclose(result, [0.25, -0.5, 1.0])


def test_parse_embedding_from_ndarray():
    result = parse_embedding(np.array([0.1, 0.2], dtype=np.float64))
    assert result.dtype == np.float32 and result.shape == (2,)
    np.testing.assert_allclose(result, [0.1, 0.2], rtol=1e-6)


def test_parse_embedding_rejects_non_vector_value():
    with pytest.raises((TypeError, ValueError)):
        parse_embedding(object())
