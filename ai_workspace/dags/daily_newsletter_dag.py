"""
Airflow DAG for Daily Newsletter Pipeline
Generates newsletters 3 times a day with monitoring and alerting
"""
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from airflow.utils.trigger_rule import TriggerRule
from datetime import datetime, timedelta
import os

# Project Path
PROJECT_PATH = "/data/ephemeral/home/pro-recsys-finalproject-recsys-01/ai_workspace"

# Alert configuration (can be overridden via Airflow Variables)
ALERT_EMAIL = os.getenv("AIRFLOW_ALERT_EMAIL", "admin@example.com")
SLACK_WEBHOOK = os.getenv("SLACK_WEBHOOK_URL", "")

default_args = {
    'owner': 'ai_news_team',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email': [ALERT_EMAIL],
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': 2,
    'retry_delay': timedelta(minutes=5),
    'execution_timeout': timedelta(hours=2),
    'sla': timedelta(hours=1),
}


def send_slack_alert(context):
    """Send Slack notification on task failure"""
    if not SLACK_WEBHOOK:
        return

    import requests

    task_instance = context.get('task_instance')
    dag_id = context.get('dag').dag_id
    task_id = task_instance.task_id
    execution_date = context.get('execution_date')
    exception = context.get('exception')

    message = {
        "text": f":x: *Airflow Task Failed*",
        "attachments": [
            {
                "color": "danger",
                "fields": [
                    {"title": "DAG", "value": dag_id, "short": True},
                    {"title": "Task", "value": task_id, "short": True},
                    {"title": "Execution Date", "value": str(execution_date), "short": True},
                    {"title": "Error", "value": str(exception)[:500], "short": False},
                ]
            }
        ]
    }

    try:
        requests.post(SLACK_WEBHOOK, json=message, timeout=10)
    except Exception as e:
        print(f"Failed to send Slack alert: {e}")


def send_success_notification(**context):
    """Send notification on successful pipeline completion"""
    if not SLACK_WEBHOOK:
        print("Pipeline completed successfully (Slack notification skipped - no webhook)")
        return

    import requests

    execution_date = context.get('execution_date')

    message = {
        "text": f":white_check_mark: *Newsletter Pipeline Completed*",
        "attachments": [
            {
                "color": "good",
                "fields": [
                    {"title": "Execution Date", "value": str(execution_date), "short": True},
                    {"title": "Status", "value": "Success", "short": True},
                ]
            }
        ]
    }

    try:
        requests.post(SLACK_WEBHOOK, json=message, timeout=10)
    except Exception as e:
        print(f"Failed to send Slack notification: {e}")


with DAG(
    'daily_newsletter_pipeline',
    default_args=default_args,
    description='Generate Newsletters 3 times a day (08:00, 12:00, 18:00) with adaptive batching',
    schedule='0 8,12,18 * * *',
    catchup=False,
    tags=['news', 'langgraph', 'production'],
    on_failure_callback=send_slack_alert,
    max_active_runs=1,
) as dag:

    # Task 1: Health Check
    health_check = BashOperator(
        task_id='health_check',
        bash_command=f'cd {PROJECT_PATH} && python -c "from db.connection import get_connection; conn = get_connection(); conn.close(); print(\'DB OK\')"',
        on_failure_callback=send_slack_alert,
    )

    # Task 2: Run RSS Collection
    collect_rss = BashOperator(
        task_id='collect_rss',
        bash_command=f'cd {PROJECT_PATH} && python main.py --collect',
        on_failure_callback=send_slack_alert,
    )

    # Task 3: Extract Content
    extract_content = BashOperator(
        task_id='extract_content',
        bash_command=f'cd {PROJECT_PATH} && python main.py --extract --workers 8',
        on_failure_callback=send_slack_alert,
    )

    # Task 4: Generate Embeddings
    generate_embeddings = BashOperator(
        task_id='generate_embeddings',
        bash_command=f'cd {PROJECT_PATH} && python main.py --embed --batch-size 30',
        on_failure_callback=send_slack_alert,
    )

    # Task 5: Generate Newsletters (Adaptive)
    generate_newsletters = BashOperator(
        task_id='generate_newsletters',
        bash_command=f'cd {PROJECT_PATH} && python main.py --newsletter --min-target 10',
        on_failure_callback=send_slack_alert,
    )

    # Task 6: Success Notification
    notify_success = PythonOperator(
        task_id='notify_success',
        python_callable=send_success_notification,
        trigger_rule=TriggerRule.ALL_SUCCESS,
    )

    # Task Dependencies
    health_check >> collect_rss >> extract_content >> generate_embeddings >> generate_newsletters >> notify_success
