from datetime import datetime

import pendulum
from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator

from _jobs import DEFAULT_ARGS, PROJECT_ROOT, job_command  # noqa: F401  (PROJECT_ROOT: 경로 기준점)

kst = pendulum.timezone("Asia/Seoul")

# 스케줄은 docker/crontab의 ingest 줄과 같다(2시간마다 5분). on_failure_callback은
# DEFAULT_ARGS에 들어 있다.
with DAG(
    "news_collector",
    default_args=DEFAULT_ARGS,
    description="RSS -> 본문 추출 -> BGE-M3 임베딩 (python -m jobs.run ingest)",
    schedule="5 */2 * * *",
    start_date=datetime(2026, 9, 1, tzinfo=kst),
    catchup=False,
    max_active_runs=1,
    tags=["news_collector"],
) as dag:
    BashOperator(task_id="ingest", bash_command=job_command("ingest"), do_xcom_push=False)
