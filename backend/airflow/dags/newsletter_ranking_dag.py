from datetime import datetime

import pendulum
from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator

from _jobs import DEFAULT_ARGS, PROJECT_ROOT, job_command  # noqa: F401  (PROJECT_ROOT: 경로 기준점)

kst = pendulum.timezone("Asia/Seoul")

# 인기도 랭킹: docker/crontab과 같은 매시 35분. on_failure_callback은 DEFAULT_ARGS에 들어 있다.
with DAG(
    "newsletter_ranking_batch",
    default_args=DEFAULT_ARGS,
    description="인기도-최신성 랭킹 (python -m jobs.run popularity)",
    schedule="35 * * * *",
    start_date=datetime(2026, 9, 1, tzinfo=kst),
    catchup=False,
    max_active_runs=1,
    tags=["ranking", "newsletter"],
) as ranking_dag:
    BashOperator(task_id="popularity", bash_command=job_command("popularity"), do_xcom_push=False)

# 생성 -> 사용자 임베딩 -> 학습/추론 -> 폴백. LLM 평가 프로토콜(ADR 0009/0010)과
# 랭커 v2(ADR 0013) 결정 전까지는 켜지 않는다 - 생성 시 일시정지 상태로 등록된다.
with DAG(
    "newsletter_daily",
    default_args=DEFAULT_ARGS,
    description="generate >> user_embed >> train >> batch_fallback",
    schedule="0 18 * * *",
    start_date=datetime(2026, 9, 1, tzinfo=kst),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["newsletter"],
) as daily_dag:
    generate = BashOperator(task_id="generate", bash_command=job_command("generate"), do_xcom_push=False)
    user_embed = BashOperator(task_id="user_embed", bash_command=job_command("user_embed"), do_xcom_push=False)
    train = BashOperator(task_id="train", bash_command=job_command("train"), do_xcom_push=False)
    fallback = BashOperator(task_id="batch_fallback", bash_command=job_command("batch_fallback"), do_xcom_push=False)

    generate >> user_embed >> train >> fallback
