from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator
from datetime import datetime, timedelta
import os

# Project Path
PROJECT_PATH = "/data/ephemeral/home/pro-recsys-finalproject-recsys-01/ai_workspace"

default_args = {
    'owner': 'ai_news_team',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email': ['admin@example.com'],
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

with DAG(
    'daily_newsletter_pipeline',
    default_args=default_args,
    description='Generate Newsletters 3 times a day (08:00, 12:00, 18:00) with adaptive batching',
    schedule='0 8,12,18 * * *', # 8am, 12pm, 6pm
    catchup=False,
    tags=['news', 'langgraph', 'production'],
) as dag:

    # Run the full pipeline
    # --no-reset: Keep existing DB data (incremental)
    # --min-target 10: Ensure at least 10 newsletters are created by reducing cluster size if needed
    # Using python directly. Ensure Airflow worker has dependencies installed.
    run_pipeline = BashOperator(
        task_id='run_pipeline_adaptive',
        bash_command=f'cd {PROJECT_PATH} && python main.py --no-reset --min-target 10 --workers 8'
    )
