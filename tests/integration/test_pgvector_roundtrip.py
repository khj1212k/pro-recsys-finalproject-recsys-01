"""pgvector 어댑터가 db.connection.get_connection()의 모든 커넥션에 등록되는지,
raw-SQL로 vector 컬럼을 쓰고/읽고 코사인 거리(<=>) 연산까지 실제 Postgres에서
동작하는지 검증한다. 실제 DB가 필요하므로 tests/integration/conftest.py의
`database_url` 픽스처가 없으면 자동으로 스킵된다.
"""
import numpy as np
import pytest


def test_vector_write_read_roundtrip_and_cosine_distance_query(database_url, pg_conn):
    from db.connection import get_connection, release_connection
    from core.user_embedder import _to_vector_array

    with pg_conn.cursor() as setup_cur:
        setup_cur.execute("DROP TABLE IF EXISTS pgvector_roundtrip_test")
        setup_cur.execute(
            "CREATE TABLE pgvector_roundtrip_test (id serial primary key, embedding vector(4))"
        )

    try:
        conn = get_connection()
        try:
            # 1. write: 순수 python float list를 파라미터로 그대로 넘긴다.
            #    register_vector가 등록돼 있어야 list -> vector 리터럴 변환이 된다.
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pgvector_roundtrip_test (embedding) VALUES (%s), (%s)",
                    ([1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]),
                )
            conn.commit()

            # 2. read: raw-SQL로 다시 읽은 값을 ai_workspace/core/user_embedder.py가
            #    실제로 쓰는 _to_vector_array()로 안전하게 numpy ndarray로 바꿀 수
            #    있어야 한다(문자열로 오거나 str()로 깨지면 안 됨).
            with conn.cursor() as cur:
                cur.execute("SELECT embedding FROM pgvector_roundtrip_test ORDER BY id")
                rows = cur.fetchall()

            assert len(rows) == 2
            first = _to_vector_array(rows[0][0])
            assert isinstance(first, np.ndarray)
            np.testing.assert_allclose(first, [1.0, 0.0, 0.0, 0.0])

            # 3. 코사인 거리(<=>) 쿼리 동작 확인 - 자기 자신과의 거리는 0에 가까워야 한다
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, embedding <=> %s AS distance "
                    "FROM pgvector_roundtrip_test ORDER BY distance LIMIT 1",
                    ([1.0, 0.0, 0.0, 0.0],),
                )
                nearest_id, distance = cur.fetchone()
            assert distance == pytest.approx(0.0, abs=1e-6)

            # 직교하는 두 번째 벡터와의 거리는 1(코사인 거리 = 1 - cos유사도)에 가까워야 한다
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT embedding <=> %s AS distance FROM pgvector_roundtrip_test WHERE id != %s",
                    ([1.0, 0.0, 0.0, 0.0], nearest_id),
                )
                (orth_distance,) = cur.fetchone()
            assert orth_distance == pytest.approx(1.0, abs=1e-6)
        finally:
            release_connection(conn)
    finally:
        with pg_conn.cursor() as cleanup_cur:
            cleanup_cur.execute("DROP TABLE IF EXISTS pgvector_roundtrip_test")


def test_get_connection_raises_loudly_when_vector_extension_missing_in_a_fresh_database(
    database_url, pg_conn
):
    """extension이 없는 DB에 연결하면 register_pgvector_adapter가 조용히 넘어가지
    않고 즉시 실패해야 한다(guard 요구사항). template1 위에 임시 DB를 만들어
    vector extension을 절대 만들지 않고 검증한다."""
    import uuid
    import psycopg2

    from db.connection import VectorExtensionMissingError

    db_name = f"pgvector_guard_test_{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{db_name}"')

    try:
        dsn_without_vector = database_url.rsplit("/", 1)[0] + f"/{db_name}"
        raw_conn = psycopg2.connect(dsn_without_vector)
        try:
            from db.connection import _register_pgvector_adapter

            with pytest.raises(VectorExtensionMissingError):
                _register_pgvector_adapter(raw_conn)
        finally:
            raw_conn.close()
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
