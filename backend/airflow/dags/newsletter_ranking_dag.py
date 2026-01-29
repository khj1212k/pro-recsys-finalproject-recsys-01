from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator
from datetime import datetime, timedelta
import pendulum
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent.parent.absolute()
env_path = PROJECT_ROOT / '.env'
load_dotenv(env_path)

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
    'newsletter_ranking_batch',
    default_args=default_args,
    description='Calculate popularity ranking for newsletters daily',
    schedule='45 17 * * *',
    start_date=datetime(2026, 1, 28, tzinfo=kst),
    catchup=False,
    tags=['ranking', 'newsletter'],
) as dag:

    PYTHON_EXEC = f"{PROJECT_ROOT}/.venv/bin/python"
    SCRIPT_PATH = f"{PROJECT_ROOT}/scheduler/calculate_ranking.py"

    calculate_ranking_task = BashOperator(
        task_id='calculate_ranking',
        bash_command=f"""
        set -e
        export PYTHONPATH={PROJECT_ROOT}
        cd {PROJECT_ROOT}
        set -a
        source .env
        set +a
        {PYTHON_EXEC} -u {SCRIPT_PATH}
        """,
    )

    calculate_ranking_task