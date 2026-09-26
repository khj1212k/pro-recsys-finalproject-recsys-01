import sys
import os
import importlib
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend", "airflow", "dags"),
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _make_context(exception="boom"):
    task_instance = MagicMock()
    task_instance.task_id = "some_task"
    dag = MagicMock()
    dag.dag_id = "some_dag"
    return {"task_instance": task_instance, "dag": dag, "exception": exception}


def test_notify_failure_logs_without_webhook(caplog):
    import _callbacks
    importlib.reload(_callbacks)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SLACK_WEBHOOK_URL", None)
        with caplog.at_level("ERROR"):
            _callbacks.notify_failure(_make_context())

    assert any("some_dag" in r.message and "some_task" in r.message for r in caplog.records)


def test_notify_failure_posts_to_slack_when_webhook_configured():
    import _callbacks
    importlib.reload(_callbacks)

    fake_requests = MagicMock()
    with patch.dict(os.environ, {"SLACK_WEBHOOK_URL": "http://example.com/webhook"}), \
         patch.dict(sys.modules, {"requests": fake_requests}):
        _callbacks.notify_failure(_make_context())

    fake_requests.post.assert_called_once()
    args, kwargs = fake_requests.post.call_args
    assert args[0] == "http://example.com/webhook"
    assert "some_dag" in kwargs["json"]["text"]


def test_dag_files_no_longer_hardcode_ephemeral_path():
    dag_dir = os.path.join(REPO_ROOT, "backend", "airflow", "dags")
    for fname in ("news_rss_collector_dag.py", "newsletter_ranking_dag.py"):
        with open(os.path.join(dag_dir, fname), encoding="utf-8") as f:
            content = f.read()
        assert "/data/ephemeral" not in content, f"{fname}에 하드코딩된 절대경로가 남아있습니다"
        assert "PROJECT_ROOT" in content, f"{fname}가 PROJECT_ROOT 기반 경로를 쓰지 않습니다"


def test_dag_files_register_on_failure_callback():
    """DAG 파일은 공통 DEFAULT_ARGS를 쓰고, 그 안에 실패 알림 콜백이 들어 있어야 한다."""
    import _callbacks
    import _jobs

    assert _jobs.DEFAULT_ARGS["on_failure_callback"] is _callbacks.notify_failure


def test_dag_job_command_runs_jobs_cli_from_repo_root():
    """Airflow 경로도 supercronic과 같은 `python -m jobs.run <job>`을 저장소 루트에서 실행한다."""
    import _jobs

    assert str(_jobs.PROJECT_ROOT) == REPO_ROOT
    assert _jobs.job_command("ingest") == f"cd {REPO_ROOT} && python -m jobs.run ingest"
