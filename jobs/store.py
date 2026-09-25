"""job_runs 테이블 + Postgres advisory lock (backend/alembic/versions/f87f7378672e_...)."""
from typing import Any, Dict, Optional

from psycopg2.extras import Json

from jobs.runtime import advisory_lock_key


class PostgresJobRunStore:
    """잡 1회 실행 동안 열어두는 전용 연결 하나로 락과 실행 기록을 모두 처리한다.

    autocommit이라 'running' 행이 실행 도중에도 다른 세션(runbook의 상태 조회,
    daily_report)에 바로 보인다. 세션 락이므로 프로세스가 죽으면 연결이 끊기면서
    락도 자동으로 풀린다.
    """

    def __init__(self, conn):
        self.conn = conn
        self.conn.autocommit = True

    @classmethod
    def connect(cls, job: str) -> "PostgresJobRunStore":
        from db.connection import connect_unpooled

        return cls(connect_unpooled(application_name=f"jobs.run:{job}"))

    def _one(self, sql: str, params) -> Any:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def try_lock(self, job: str) -> bool:
        (acquired,) = self._one("SELECT pg_try_advisory_lock(%s)", (advisory_lock_key(job),))
        return bool(acquired)

    def unlock(self, job: str) -> None:
        self._one("SELECT pg_advisory_unlock(%s)", (advisory_lock_key(job),))

    def abandon_stale(self, job: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_runs
                SET status = 'abandoned',
                    finished_at = now(),
                    error = coalesce(error, '') || 'process ended without recording a result'
                WHERE job = %s AND status = 'running'
                """,
                (job,),
            )
            return cur.rowcount

    def start(self, job: str, git_sha: Optional[str]) -> int:
        (run_id,) = self._one(
            "INSERT INTO job_runs (job, status, git_sha) VALUES (%s, 'running', %s) RETURNING id",
            (job, git_sha),
        )
        return run_id

    def finish(self, run_id: int, status: str, stats: Dict[str, Any], error: Optional[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_runs
                SET status = %s, finished_at = now(), stats = %s, error = %s
                WHERE id = %s
                """,
                (status, Json(stats), error, run_id),
            )

    def record(self, job: str, status: str, stats: Dict[str, Any], git_sha: Optional[str],
               error: Optional[str] = None) -> int:
        (run_id,) = self._one(
            """
            INSERT INTO job_runs (job, status, finished_at, stats, error, git_sha)
            VALUES (%s, %s, now(), %s, %s, %s)
            RETURNING id
            """,
            (job, status, Json(stats), error, git_sha),
        )
        return run_id

    def close(self) -> None:
        self.conn.close()
