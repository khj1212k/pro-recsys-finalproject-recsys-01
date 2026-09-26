"""DAG이 공통으로 쓰는 `python -m jobs.run <job>` 명령 생성기.

보관용이다 - 실행 경로가 아니다(README.md, docs/adr/0006). 운영 스케줄러는 supercronic이고,
이 DAG들은 팀 시절 Airflow 스케줄을 같은 `python -m jobs.run` CLI 위에 옮겨 둔 기록이다.
"""
import os
import shlex
from datetime import timedelta
from pathlib import Path

from _callbacks import notify_failure

# backend/airflow/dags/_jobs.py -> parents[3]이 저장소 루트
PROJECT_ROOT = Path(__file__).resolve().parents[3]
# Airflow 자체 환경과 잡 의존성(torch 등)을 섞지 않기 위해 잡은 별도 venv의 파이썬으로 실행한다
# (JOBS_PYTHON=<잡 venv의 python>).
JOBS_PYTHON = os.getenv("JOBS_PYTHON", "python")

DEFAULT_ARGS = {
    "owner": "admin",
    "depends_on_past": False,
    "email_on_failure": False,
    "email_on_retry": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
    "on_failure_callback": notify_failure,
}


def job_command(job: str, *args: str) -> str:
    parts = [JOBS_PYTHON, "-m", "jobs.run", job, *args]
    return f"cd {shlex.quote(str(PROJECT_ROOT))} && " + " ".join(shlex.quote(p) for p in parts)
