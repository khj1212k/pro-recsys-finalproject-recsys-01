import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


def _make_cursor_conn(fetchone_result):
    """conn.cursor()를 `with ... as cur:` 형태로 쓰는 코드를 흉내낼 수 있도록
    __enter__/__exit__까지 채운 MagicMock 커넥션/커서 쌍을 만든다."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    cur.fetchone.return_value = fetchone_result
    return conn, cur


def test_register_pgvector_adapter_calls_register_vector_when_extension_present():
    from db.connection import _register_pgvector_adapter

    conn, cur = _make_cursor_conn(fetchone_result=(1,))

    with patch("db.connection.register_vector") as mock_register_vector:
        result = _register_pgvector_adapter(conn)

    assert cur.execute.call_count == 1
    executed_sql = cur.execute.call_args[0][0]
    assert "pg_extension" in executed_sql
    assert "vector" in executed_sql
    mock_register_vector.assert_called_once_with(conn)
    assert result is conn


def test_register_pgvector_adapter_fails_loudly_when_extension_missing():
    from db.connection import _register_pgvector_adapter, VectorExtensionMissingError

    conn, cur = _make_cursor_conn(fetchone_result=None)

    with patch("db.connection.register_vector") as mock_register_vector:
        with pytest.raises(VectorExtensionMissingError):
            _register_pgvector_adapter(conn)

    mock_register_vector.assert_not_called()


def test_get_connection_registers_pgvector_on_pooled_connection():
    from db.connection import DatabasePool

    DatabasePool._instance = None
    DatabasePool._pool = None

    fake_conn = MagicMock()
    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn

    with patch("db.connection.pool.ThreadedConnectionPool", return_value=fake_pool), \
         patch("db.connection._build_db_config", return_value={}):
        db_pool = DatabasePool()

    try:
        with patch("db.connection._register_pgvector_adapter", side_effect=lambda c: c) as mock_reg:
            conn = db_pool.get_connection()

        mock_reg.assert_called_once_with(fake_conn)
        assert conn is fake_conn
    finally:
        DatabasePool._instance = None
        DatabasePool._pool = None


def test_get_connection_registers_pgvector_on_direct_fallback_connection():
    from db.connection import DatabasePool

    DatabasePool._instance = None
    DatabasePool._pool = None

    with patch("db.connection.pool.ThreadedConnectionPool", side_effect=Exception("pool init boom")), \
         patch("db.connection._build_db_config", return_value={}):
        db_pool = DatabasePool()

    assert db_pool._pool is None

    try:
        fake_direct_conn = MagicMock()
        with patch.object(DatabasePool, "_create_direct_connection", return_value=fake_direct_conn), \
             patch("db.connection._register_pgvector_adapter", side_effect=lambda c: c) as mock_reg:
            conn = db_pool.get_connection()

        mock_reg.assert_called_once_with(fake_direct_conn)
        assert conn is fake_direct_conn
    finally:
        DatabasePool._instance = None
        DatabasePool._pool = None


def test_get_connection_propagates_vector_extension_missing_error_without_falling_back_to_direct():
    """pool에서 얻은 커넥션에 vector extension이 없으면, 그 에러가 PoolError로
    오인되어 direct connection 생성으로 조용히 폴백되면 안 된다 - 즉시 실패해야 한다."""
    from db.connection import DatabasePool, VectorExtensionMissingError

    DatabasePool._instance = None
    DatabasePool._pool = None

    fake_conn = MagicMock()
    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn

    with patch("db.connection.pool.ThreadedConnectionPool", return_value=fake_pool), \
         patch("db.connection._build_db_config", return_value={}):
        db_pool = DatabasePool()

    try:
        with patch("db.connection._register_pgvector_adapter", side_effect=VectorExtensionMissingError("no ext")), \
             patch.object(DatabasePool, "_create_direct_connection") as mock_direct:
            with pytest.raises(VectorExtensionMissingError):
                db_pool.get_connection()

        mock_direct.assert_not_called()
    finally:
        DatabasePool._instance = None
        DatabasePool._pool = None


def test_get_connection_still_falls_back_to_direct_connection_on_pool_error():
    """기존 동작(풀 고갈 시 direct connection으로 폴백)은 그대로 유지되어야 한다."""
    from db.connection import DatabasePool
    from psycopg2 import pool as psycopg2_pool

    DatabasePool._instance = None
    DatabasePool._pool = None

    fake_pool = MagicMock()
    fake_pool.getconn.side_effect = psycopg2_pool.PoolError("exhausted")

    with patch("db.connection.pool.ThreadedConnectionPool", return_value=fake_pool), \
         patch("db.connection._build_db_config", return_value={}):
        db_pool = DatabasePool()

    try:
        fake_direct_conn = MagicMock()
        with patch.object(DatabasePool, "_create_direct_connection", return_value=fake_direct_conn), \
             patch("db.connection._register_pgvector_adapter", side_effect=lambda c: c) as mock_reg:
            conn = db_pool.get_connection()

        assert conn is fake_direct_conn
        mock_reg.assert_called_once_with(fake_direct_conn)
    finally:
        DatabasePool._instance = None
        DatabasePool._pool = None
