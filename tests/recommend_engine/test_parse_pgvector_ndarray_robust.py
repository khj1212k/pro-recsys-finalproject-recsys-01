import numpy as np
import pandas as pd
from unittest.mock import patch


def _make_loader():
    from src.data.data_loader import DataLoader
    return DataLoader(config={"database": {}})


def test_parse_pgvector_handles_comma_separated_string_input():
    loader = _make_loader()
    result = loader._parse_pgvector("[0.1,0.2,0.3]")
    assert isinstance(result, np.ndarray)
    assert result.dtype == np.float32
    np.testing.assert_allclose(result, [0.1, 0.2, 0.3], atol=1e-6)


def test_parse_pgvector_handles_ndarray_input_directly():
    loader = _make_loader()
    vec = np.random.rand(1024).astype(np.float32)
    result = loader._parse_pgvector(vec)
    np.testing.assert_allclose(result, vec)


def test_parse_pgvector_does_not_corrupt_ndarray_that_numpy_would_summarize_as_string():
    # register_vector가 등록된 연결(pgvector.psycopg2)에서는 vector 컬럼 값이
    # 이미 numpy.ndarray로 온다. 1024차원 배열을 str()로 감싸면 numpy가 "..."로
    # 요약해버려 값이 깨지므로, ndarray는 str 변환 없이 그대로 처리해야 한다.
    loader = _make_loader()
    vec = np.arange(1024, dtype=np.float32)
    assert "..." in str(vec)  # 이 테스트의 전제 조건을 명시적으로 확인
    result = loader._parse_pgvector(vec)
    np.testing.assert_array_equal(result, vec)


def test_parse_pgvector_handles_pgvector_vector_wrapper_object():
    # pgvector.psycopg2.register_vector가 등록된 연결(예: ai_workspace/core/
    # user_embedder.py의 raw psycopg2 경로)에서 raw-SQL로 vector 컬럼을 읽으면
    # numpy.ndarray가 아니라 pgvector.Vector 객체가 온다. str()로 감싸면
    # "Vector([...])" 문자열이 되어 숫자 파싱이 깨지므로 to_numpy()로 풀어야 한다.
    from pgvector import Vector

    loader = _make_loader()
    vec = Vector([0.1, 0.2, 0.3])
    result = loader._parse_pgvector(vec)
    assert isinstance(result, np.ndarray)
    np.testing.assert_allclose(result, [0.1, 0.2, 0.3], atol=1e-6)


def test_parse_pgvector_handles_none_input_with_zero_vector():
    loader = _make_loader()
    result = loader._parse_pgvector(None)
    assert result.shape == (1024,)
    assert np.all(result == 0)


def test_parse_pgvector_handles_malformed_string_with_zero_vector():
    loader = _make_loader()
    result = loader._parse_pgvector("not-a-vector")
    assert result.shape == (1024,)
    assert np.all(result == 0)


def test_load_embedded_news_preserves_ndarray_embedding_without_forcing_str_cast():
    """DataLoader가 SQLAlchemy 대신 register_vector가 등록된 psycopg2 raw 연결
    경로로 바뀌어 news_letter_embedding 컬럼이 이미 ndarray로 오더라도,
    load_embedded_news()가 이를 str()로 망가뜨리지 않고 그대로 보존해야 한다."""
    loader = _make_loader()

    big_vec = np.arange(1024, dtype=np.float32)
    news_df = pd.DataFrame({
        "news_letter_id": [1],
        "news_letter_title": ["제목"],
        "news_letter_content": ["내용"],
        "news_letter_embedding": [big_vec],
        "news_letter_created_at": [pd.Timestamp("2026-09-25")],
    })
    cat_df = pd.DataFrame({"news_letter_id": [], "category_id": []})

    with patch.object(loader, "_load_from_db", side_effect=[news_df, cat_df]):
        news_dict = loader.load_embedded_news()

    np.testing.assert_array_equal(news_dict[1].embedding, big_vec)
