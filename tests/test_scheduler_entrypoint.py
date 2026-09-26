"""docker/scheduler-entrypoint.sh가 컨테이너 종료 신호를 supercronic이 띄운 잡의 프로세스 그룹까지 전달하는지.

진짜 supercronic 대신, 같은 방식으로 동작하는 가짜를 PATH에 둔다: 잡을 새 프로세스 그룹으로 띄우고
SIGTERM을 받으면 잡에 신호를 보내지 않고 끝나기만 기다린다. /proc을 읽는 스크립트라 Linux(CI)에서만 돈다.
"""
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

ENTRYPOINT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docker", "scheduler-entrypoint.sh"
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="엔트리포인트가 /proc으로 프로세스 그룹을 찾는다 (Linux 전용)"
)


def _write_executable(path, body):
    with open(path, "w") as f:
        f.write(f"#!{sys.executable}\n" + textwrap.dedent(body))
    os.chmod(path, 0o755)


def _wait_for(path, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.05)
    raise AssertionError(f"{path}가 {timeout}s 안에 생기지 않음")


def test_sigterm_reaches_job_process_group_and_scheduler_exits_cleanly(tmp_path):
    ready, outcome = tmp_path / "ready", tmp_path / "outcome"
    job = tmp_path / "job.py"
    _write_executable(job, f"""
        import signal, sys, time
        def on_term(signum, frame):
            open({str(outcome)!r}, "w").write("terminated")
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
        open({str(ready)!r}, "w").write("ok")
        time.sleep(30)
        open({str(outcome)!r}, "w").write("finished without signal")
    """)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "supercronic", f"""
        import signal, subprocess, sys
        # 잡이 ready를 쓰기 전에 핸들러가 있어야 한다: 반대 순서면 테스트의 SIGTERM이 핸들러보다
        # 먼저 도착해 가짜 supercronic이 기본 동작으로 죽고(143), 엔트리포인트는 그 상태를 그대로 전달한다.
        signal.signal(signal.SIGTERM, lambda *a: None)
        child = subprocess.Popen([{str(job)!r}], process_group=0)
        child.wait()
        sys.exit(0)
    """)

    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    proc = subprocess.Popen(["sh", ENTRYPOINT, "/dev/null"], env=env)
    try:
        _wait_for(ready)
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        assert time.monotonic() - started < 5
        assert outcome.read_text() == "terminated"
    finally:
        if proc.poll() is None:
            proc.kill()
