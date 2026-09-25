"""배치 잡 진입점 패키지 (`python -m jobs.run <job>`, `python -m jobs.migrate`).

스케줄러(supercronic, 선택적으로 Airflow)는 이 CLI만 호출한다. 잡 로직은
ai_workspace/ 파이프라인과 backend/ 배치 스크립트를 그대로 재사용하고, 여기서는
실행 기록(job_runs), 중복 실행 방지(Postgres advisory lock), 실패 알림만 더한다.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def setup_import_paths() -> None:
    """진입점(jobs.run, jobs.migrate)에서만 호출한다 - 패키지 임포트 자체에는 부작용이 없게
    해서, Airflow DAG 프로세서처럼 jobs.notify만 필요한 곳의 sys.path를 건드리지 않는다.

    ai_workspace는 `from config.settings import ...`처럼 자기 디렉터리를 루트로 쓰고,
    backend 배치 스크립트는 `from app.database import ...`를 쓴다. append로 두어
    site-packages(예: 진짜 alembic 패키지)를 backend/alembic 디렉터리가 가리지 않게 한다.
    """
    for sub in ("ai_workspace", "backend"):
        path = str(REPO_ROOT / sub)
        if path not in sys.path:
            sys.path.append(path)
