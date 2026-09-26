# 보관된 Airflow DAG (실행하지 않음)

이 디렉터리는 **실행 경로가 아니다.** 운영 스케줄러는 `scheduler` 컨테이너의 supercronic
(`docker/crontab`)이고, Mac 개발 환경의 임베딩은 호스트 launchd 에이전트(`scripts/mac_embed_agent.sh`)가
돌린다. 결정 근거는 [ADR 0006](../../../docs/adr/0006-runtime-compose-and-scheduler.md).

- 팀 프로젝트 시절(2026-01~02)에는 Airflow DAG이 `ai_workspace/main.py` 단계를 직접 불렀다.
  이 fork에서 모든 배치를 `python -m jobs.run <job>` CLI(실행 기록·advisory lock·실패 알림)로 옮기면서
  DAG도 같은 CLI를 부르는 얇은 형태로 바꿔 두었다(`_jobs.py`). 스케줄·의존 순서의 기록으로 남긴다.
- 2026-09-26에 `docker compose --profile airflow` 경로와 Airflow 이미지를 제거했다. 두 스케줄러가
  동시에 떠서 같은 잡을 이중으로 스케줄링할 위험을 없애고, 12GB 소형 VM에서 상시 떠 있는
  Airflow 프로세스 메모리를 아끼기 위해서다(유휴 메모리 실측은 ADR 0006).
- DAG 파일의 구문·경로는 `tests/test_airflow_dag_fixes.py`가 계속 검사한다.
  직접 띄워 보려면 `pip install -r backend/requirements-airflow.txt` 후 `AIRFLOW__CORE__DAGS_FOLDER`를
  이 디렉터리로 두고 `JOBS_PYTHON`에 잡 의존성이 설치된 파이썬 경로를 지정한다.
