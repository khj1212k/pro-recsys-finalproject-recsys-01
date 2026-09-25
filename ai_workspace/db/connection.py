import logging
import os
import threading
from contextlib import contextmanager
from typing import Optional, Generator

import psycopg2
from psycopg2 import pool
from psycopg2.extensions import connection as Connection
from pgvector.psycopg2 import register_vector
from dotenv import load_dotenv

from config.settings import Settings

logger = logging.getLogger(__name__)

load_dotenv()


class VectorExtensionMissingError(RuntimeError):
    """pgvector의 `vector` extension이 설치되지 않은 DB에 연결했을 때 발생한다."""


def _register_pgvector_adapter(conn: Connection) -> Connection:
    """이 커넥션에 pgvector 타입 어댑터를 등록한다.

    등록하지 않으면 vector 컬럼을 raw SQL(psycopg2 cursor)로 읽을 때 값이 문자열로
    돌아온다 - ai_workspace/core/user_embedder.py가 `np.array(<str>)`를 만들다
    깨지는 원인. extension이 없는 DB에 연결된 경우, 이후 벡터 쿼리가 알 수 없는
    시점에 이상한 값(문자열)을 조용히 돌려주는 대신 커넥션을 내주는 시점에 바로
    실패시킨다.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        if cur.fetchone() is None:
            raise VectorExtensionMissingError(
                "pgvector extension('vector')이 설치되어 있지 않습니다. "
                "먼저 `CREATE EXTENSION vector;`를 실행한 뒤 다시 연결하세요."
            )
    register_vector(conn)
    return conn


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
                conn = self._pool.getconn()
            except pool.PoolError:
                conn = None
            if conn is not None:
                # PoolError만 direct connection으로 폴백한다 - 여기서 발생하는
                # VectorExtensionMissingError까지 삼켜서 폴백해버리면 안 된다.
                try:
                    return _register_pgvector_adapter(conn)
                except Exception:
                    try:
                        self._pool.putconn(conn, close=True)
                    except Exception:
                        logger.exception("pgvector 등록 실패 후 커넥션 반납에도 실패했습니다")
                    raise

        direct_conn = self._create_direct_connection()
        try:
            return _register_pgvector_adapter(direct_conn)
        except Exception:
            try:
                direct_conn.close()
            except Exception:
                logger.exception("pgvector 등록 실패 후 직접 연결 종료에도 실패했습니다")
            raise

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
