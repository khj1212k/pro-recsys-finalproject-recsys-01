import os
import threading
from contextlib import contextmanager
from typing import Optional, Generator

import psycopg2
from psycopg2 import pool
from psycopg2.extensions import connection as Connection
from dotenv import load_dotenv

load_dotenv()


class DatabasePool:
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
        # 커넥션 풀 초기화
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
            print(f"Connection pool initialization failed: {e}")
            self._pool = None

    def get_connection(self) -> Connection:
        if self._pool:
            try:
                return self._pool.getconn()
            except pool.PoolError:
                pass

        return self._create_direct_connection()

    def release_connection(self, conn: Connection) -> None:
        if self._pool and conn:
            try:
                self._pool.putconn(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass

    def close_all(self) -> None:
        if self._pool:
            self._pool.closeall()
            self._pool = None

    @staticmethod
    def _create_direct_connection() -> Connection:
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
    global _db_pool
    if _db_pool is None:
        _db_pool = DatabasePool()
    return _db_pool


def get_connection() -> Connection:
    return get_pool().get_connection()


def release_connection(conn: Connection) -> None:
    get_pool().release_connection(conn)


@contextmanager
def get_db_connection() -> Generator[Connection, None, None]:

    conn = get_connection()
    try:
        yield conn
    finally:
        release_connection(conn)


def close_pool() -> None:
    global _db_pool
    if _db_pool:
        _db_pool.close_all()
        _db_pool = None
