"""
ai_workspace를 위한 데이터베이스 연결 관리
성능 향상을 위한 커넥션 풀(Connection Pool) 지원
"""
import os
import threading
from contextlib import contextmanager
from typing import Optional, Generator

import psycopg2
from psycopg2 import pool
from psycopg2.extensions import connection as Connection
from dotenv import load_dotenv

# Load .env file
load_dotenv()


class DatabasePool:
    """
    스레드 안전한(Thread-safe) DB 커넥션 풀 싱글톤 클래스.
    동시성 제어를 위해 ThreadedConnectionPool을 사용합니다.
    """
    _instance: Optional['DatabasePool'] = None
    _lock = threading.Lock()
    _pool: Optional[pool.ThreadedConnectionPool] = None

    def __new__(cls) -> 'DatabasePool':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._pool is None:
            self._initialize_pool()

    def _initialize_pool(self) -> None:
        """커넥션 풀 초기화"""
        config = {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": os.getenv("DB_PORT", "5432"),
            "user": os.getenv("DB_USER", "postgres"),
            "password": os.getenv("DB_PASSWORD", "password"),
            "dbname": os.getenv("DB_NAME", "final_db"),
            "options": "-c client_encoding=UTF8"
        }

        min_conn = int(os.getenv("DB_POOL_MIN", "2"))
        max_conn = int(os.getenv("DB_POOL_MAX", "10"))

        try:
            self._pool = pool.ThreadedConnectionPool(
                minconn=min_conn,
                maxconn=max_conn,
                **config
            )
        except Exception as e:
            # Fallback: log error but don't crash
            print(f"Connection pool initialization failed: {e}")
            self._pool = None

    def get_connection(self) -> Connection:
        """
        풀에서 커넥션을 가져옵니다.
        풀이 고갈되거나 사용 불가능할 경우 직접 연결(Fallback)을 시도합니다.
        """
        if self._pool:
            try:
                return self._pool.getconn()
            except pool.PoolError:
                # Pool exhausted, create direct connection
                pass

        # Fallback to direct connection
        return self._create_direct_connection()

    def release_connection(self, conn: Connection) -> None:
        """커넥션을 풀로 반환합니다"""
        if self._pool and conn:
            try:
                self._pool.putconn(conn)
            except Exception:
                # Connection might be broken, close it
                try:
                    conn.close()
                except Exception:
                    pass

    def close_all(self) -> None:
        """Close all connections in the pool"""
        if self._pool:
            self._pool.closeall()
            self._pool = None

    @staticmethod
    def _create_direct_connection() -> Connection:
        """직접 DB 연결 생성 (Fallback용)"""
        config = {
            "host": os.getenv("DB_HOST", "localhost"),
            "port": os.getenv("DB_PORT", "5432"),
            "user": os.getenv("DB_USER", "postgres"),
            "password": os.getenv("DB_PASSWORD", "password"),
            "dbname": os.getenv("DB_NAME", "final_db"),
            "options": "-c client_encoding=UTF8"
        }
        return psycopg2.connect(**config)


# Global pool instance
_db_pool: Optional[DatabasePool] = None


def get_pool() -> DatabasePool:
    """DB 커넥션 풀 싱글톤 인스턴스를 반환합니다"""
    global _db_pool
    if _db_pool is None:
        _db_pool = DatabasePool()
    return _db_pool


def get_connection() -> Connection:
    """
    DB 커넥션을 획득합니다.
    가능한 경우 풀을 사용하고, 그렇지 않으면 직접 연결을 생성합니다.

    Note: 호출자는 반드시 커넥션을 닫거나 반환해야 합니다.
    자동 관리를 위해서는 get_db_connection() 컨텍스트 매니저를 사용하세요.
    """
    return get_pool().get_connection()


def release_connection(conn: Connection) -> None:
    """Release connection back to pool"""
    get_pool().release_connection(conn)


@contextmanager
def get_db_connection() -> Generator[Connection, None, None]:
    """
    DB 연결을 위한 컨텍스트 매니저.
    사용 후 자동으로 커넥션을 풀로 반환합니다.

    Usage:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM table")
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        release_connection(conn)


def close_pool() -> None:
    """Close the connection pool (call on application shutdown)"""
    global _db_pool
    if _db_pool:
        _db_pool.close_all()
        _db_pool = None
