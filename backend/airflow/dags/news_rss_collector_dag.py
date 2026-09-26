from datetime import datetime

import pendulum
from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator

from _jobs import DEFAULT_ARGS, PROJECT_ROOT, job_command  # noqa: F401  (PROJECT_ROOT: 경로 기준점)

kst = pendulum.timezone("Asia/Seoul")

# 보관용(실행하지 않음, ADR 0006). docker/crontab의 ingest 줄과 같게 매시 5분에 RSS -> 본문 추출만 돈다 -
# 임베딩은 crontab에서 별도 embed 잡(매시 20분)이고 이 보관 DAG에는 없다. on_failure_callback은 DEFAULT_ARGS에 있다.
with DAG(
    "news_collector",
    default_args=DEFAULT_ARGS,
    description="RSS -> 본문 추출 (python -m jobs.run ingest --stages rss,extract)",
    schedule="5 * * * *",
    start_date=datetime(2026, 9, 1, tzinfo=kst),
    catchup=False,
    max_active_runs=1,
    tags=["news_collector"],
) as dag:
    BashOperator(task_id="ingest", bash_command=job_command("ingest --stages rss,extract"), do_xcom_push=False)
