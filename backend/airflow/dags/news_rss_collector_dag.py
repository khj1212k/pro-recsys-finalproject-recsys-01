from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator
import pendulum
from datetime import datetime, timedelta
import os

# KST Timezone settings
kst = pendulum.timezone("Asia/Seoul")

default_args = {
    'owner': 'admin',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

with DAG(
    'news_collector',
    default_args=default_args,
    description='AI Workspace News Pipeline (Stage 1 to 3)',
    schedule='0 2,5,8,11,14,20,23 * * *',
    start_date=datetime(2026, 2, 4, tzinfo=kst),
    catchup=False,
    tags=['news_collector'],
) as dag:

    news_collector = BashOperator(
        task_id='news_collector',
        bash_command='cd /data/ephemeral/home/pro-recsys-finalproject-recsys-01/ai_workspace && python main.py --from-stage 1 --to-stage 3',
        do_xcom_push=False,
    )

    news_collector
