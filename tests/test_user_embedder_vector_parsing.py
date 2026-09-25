import os
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from core.user_embedder import UserEmbedder, _to_vector_array


def test_to_vector_array_handles_ndarray():
    v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    result = _to_vector_array(v)
    np.testing.assert_allclose(result, v)


def test_to_vector_array_handles_pgvector_vector_wrapper():
    # register_pgvector_adapter가 등록된 연결(db.connection.get_connection())에서
    # raw-SQL로 vector 컬럼을 읽으면 numpy.ndarray가 아니라 pgvector.Vector
    # 객체가 온다. np.array(v)로 그대로 감싸면 0차원 object 배열이 되어 이후
    # np.stack/곱셈에서 깨지므로 to_numpy()로 풀어야 한다.
    from pgvector import Vector

    result = _to_vector_array(Vector([1.0, 2.0, 3.0]))
    np.testing.assert_allclose(result, [1.0, 2.0, 3.0])


def test_to_vector_array_handles_legacy_string_when_adapter_not_registered():
    result = _to_vector_array("[1.0,2.0,3.0]")
    np.testing.assert_allclose(result, [1.0, 2.0, 3.0])


def test_to_vector_array_returns_none_for_none_or_empty():
    assert _to_vector_array(None) is None
    assert _to_vector_array("") is None


def _make_cursor_conn():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cur


def test_batch_update_all_users_computes_embedding_from_pgvector_wrapper_rows():
    """register_vector가 등록된 실제 연결처럼, news_letter_embedding 컬럼이
    pgvector.Vector 객체로 오는 상황을 흉내내 end-to-end로 검증한다."""
    from pgvector import Vector

    conn, cur = _make_cursor_conn()

    def execute_side_effect(sql, params=None):
        execute_side_effect.calls.append((sql, params))
    execute_side_effect.calls = []
    cur.execute.side_effect = execute_side_effect

    def fetchall_side_effect():
        sql, _ = execute_side_effect.calls[-1]
        if "user_embedding IS NULL" in sql:
            return [(1,)]
        if "user_preferred_newsletter" in sql:
            return [(1, 100)]
        if "user_newsletter_ctr_log" in sql:
            return []
        if "news_letter_embedding" in sql:
            return [(100, Vector([1.0, 0.0, 0.0]))]
        return []

    cur.fetchall.side_effect = fetchall_side_effect

    with patch("core.user_embedder.get_connection", return_value=conn), \
         patch("core.user_embedder.release_connection"):
        stats = UserEmbedder().batch_update_all_users()

    assert stats == {"success": 1, "failed": 0, "skipped": 0}
    cur.executemany.assert_called_once()
    (update_sql, updates), _ = cur.executemany.call_args
    assert len(updates) == 1
    (embedding_list, uid) = updates[0]
    assert uid == 1
    np.testing.assert_allclose(embedding_list, [1.0, 0.0, 0.0], atol=1e-6)
    conn.commit.assert_called_once()
