"""알림: 로그에 남기고, SLACK_WEBHOOK_URL이 있으면 Slack으로도 보낸다.

backend/airflow/dags/_callbacks.py의 notify_failure도 send_alert를 호출한다 -
supercronic 경로(jobs.run)와 보관된 Airflow DAG이 같은 알림 규칙을 쓰게 하기 위함.
"""
import logging
import os

logger = logging.getLogger("jobs.notify")


def post_message(text: str) -> bool:
    """Slack 웹훅이 설정돼 있으면 보낸다. 알림 실패가 잡 결과를 바꾸지는 않는다."""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return False

    try:
        import requests
        requests.post(webhook_url, json={"text": text}, timeout=5)
        return True
    except Exception as e:
        logger.error(f"Slack 알림 전송 실패: {e}")
        return False


def send_alert(message: str) -> None:
    logger.error(message)
    post_message(message)
