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
