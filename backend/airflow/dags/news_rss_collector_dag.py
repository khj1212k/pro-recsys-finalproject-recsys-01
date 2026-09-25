from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator
import pendulum
from datetime import datetime, timedelta
from pathlib import Path

from _callbacks import notify_failure

# KST Timezone settings
kst = pendulum.timezone("Asia/Seoul")

# 프로젝트 루트 (부스트캠프 서버 전용 절대경로 하드코딩 대신 파일 위치 기준 상대경로)
PROJECT_ROOT = Path(__file__).parent.parent.parent.absolute()
AI_PATH = PROJECT_ROOT / "ai_workspace"
PYTHON_EXEC = PROJECT_ROOT / ".venv" / "bin" / "python"

default_args = {
    'owner': 'admin',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
    'on_failure_callback': notify_failure,
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
        bash_command=f'cd {AI_PATH} && {PYTHON_EXEC} main.py --from-stage 1 --to-stage 3',
        do_xcom_push=False,
    )

    news_collector
