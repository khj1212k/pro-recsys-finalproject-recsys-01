
from airflow import DAG
from airflow.operators.bash import BashOperator
from datetime import datetime, timedelta
import pendulum

# Define Timezone (KST)
kst = pendulum.timezone("Asia/Seoul")

default_args = {
    'owner': 'admin',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

# Schedule: 17:45 KST Daily
# Airflow stores dates in UTC, but we can use time-zone aware start_dates.
# For cron schedule, it's best to be explicit.
# If we set timezone in DAG, cron follows that timezone.
with DAG(
    'newsletter_ranking_batch',
    default_args=default_args,
    description='Calculate popularity ranking for newsletters daily',
    schedule_interval='45 17 * * *', # 17:45 in the DAG's timezone (KST)
    start_date=datetime(2024, 1, 1, tzinfo=kst),
    catchup=False,
    tags=['ranking', 'newsletter'],
) as dag:

    # Path to the script and virtual environment python
    # Adjust these paths based on the actual server environment
    PROJECT_ROOT = "/data/ephemeral/home/sojeong/final-project/pro-recsys-finalproject-recsys-01/backend"
    PYTHON_EXEC = f"{PROJECT_ROOT}/.venv/bin/python"
    SCRIPT_PATH = f"{PROJECT_ROOT}/scripts/calculate_ranking.py"

    calculate_ranking_task = BashOperator(
        task_id='calculate_ranking',
        bash_command=f"cd {PROJECT_ROOT} && {PYTHON_EXEC} {SCRIPT_PATH}",
    )

    calculate_ranking_task
