"""Airflow DAG 실패 알림 콜백.

SLACK_WEBHOOK_URL 환경변수가 설정되어 있으면 Slack으로 알림을 보내고,
없으면 최소한 로그에 명확히 남겨 Airflow UI를 매번 열지 않아도 실패를 알 수 있게 한다.
"""
import logging
import os

logger = logging.getLogger("airflow.dag_failure")


def notify_failure(context):
    task_instance = context.get("task_instance")
    dag = context.get("dag")
    task_id = task_instance.task_id if task_instance else "unknown"
    dag_id = dag.dag_id if dag else "unknown"
    exception = context.get("exception")

    message = f"🚨 Airflow task 실패: dag={dag_id}, task={task_id}, error={exception}"
    logger.error(message)

    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return

    try:
        import requests
        requests.post(webhook_url, json={"text": message}, timeout=5)
    except Exception as e:
        logger.error(f"Slack 알림 전송 실패: {e}")
