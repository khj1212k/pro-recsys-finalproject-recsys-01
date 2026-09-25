import os
import threading
from contextlib import contextmanager
from typing import Optional, Generator

import psycopg2
from psycopg2 import pool
from psycopg2.extensions import connection as Connection
from dotenv import load_dotenv

from config.settings import Settings

load_dotenv()


def _build_db_config() -> dict:
    """DB 접속 정보를 Settings(config/settings.py)에서 읽어온다.

    이전에는 이 파일이 os.getenv를 직접 호출해 기본값("password")을 자체적으로
    갖고 있었고, 이는 .env.example/recommend_engine의 config.yaml이 쓰는 기본값과
    서로 달랐다. Settings를 단일 진실 공급원으로 통일한다.
    """
    return {
        "host": Settings.DB_HOST,
        "port": Settings.DB_PORT,
        "user": Settings.DB_USER,
        "password": Settings.DB_PASSWORD,
        "dbname": Settings.DB_NAME,
        "options": "-c client_encoding=UTF8",
    }


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
        config = _build_db_config()

        min_conn = int(os.getenv("DB_POOL_MIN", str(Settings.DB_POOL_MIN)))
        max_conn = int(os.getenv("DB_POOL_MAX", str(Settings.DB_POOL_MAX)))

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
        return psycopg2.connect(**_build_db_config())


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
