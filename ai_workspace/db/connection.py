"""
Database connection management for ai_workspace
Supports Connection Pool for better performance
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
    Thread-safe database connection pool singleton.
    Uses ThreadedConnectionPool for concurrent access.
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
        """Initialize the connection pool"""
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
        Get a connection from the pool.
        Falls back to direct connection if pool is unavailable.
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
        """Release a connection back to the pool"""
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
        """Create a direct database connection (fallback)"""
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
    """Get the database pool singleton"""
    global _db_pool
    if _db_pool is None:
        _db_pool = DatabasePool()
    return _db_pool


def get_connection() -> Connection:
    """
    Get database connection.
    Uses pool if available, otherwise creates direct connection.

    Note: Caller is responsible for closing the connection.
    For automatic management, use get_db_connection() context manager.
    """
    return get_pool().get_connection()


def release_connection(conn: Connection) -> None:
    """Release connection back to pool"""
    get_pool().release_connection(conn)


@contextmanager
def get_db_connection() -> Generator[Connection, None, None]:
    """
    Context manager for database connections.
    Automatically releases connection back to pool.

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
