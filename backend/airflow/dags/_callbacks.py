"""Airflow DAG 실패 알림 콜백.

알림 규칙(로그 + SLACK_WEBHOOK_URL이 있으면 Slack)은 jobs/notify.py 하나에 두고
여기서는 Airflow context를 메시지로 바꾸기만 한다 - 기본 스케줄러(supercronic이
호출하는 `python -m jobs.run`)와 선택적 Airflow 프로필이 같은 알림 경로를 쓰게 하기 위함.
"""
import sys
from pathlib import Path

# backend/airflow/dags/_callbacks.py -> parents[3]이 저장소 루트(jobs/ 패키지 위치)
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from jobs.notify import send_alert  # noqa: E402


def notify_failure(context):
    task_instance = context.get("task_instance")
    dag = context.get("dag")
    task_id = task_instance.task_id if task_instance else "unknown"
    dag_id = dag.dag_id if dag else "unknown"
    exception = context.get("exception")

    send_alert(f"🚨 Airflow task 실패: dag={dag_id}, task={task_id}, error={exception}")
