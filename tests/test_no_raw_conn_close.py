import subprocess
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# db/connection.py의 release_connection() 내부 폴백 close()만 허용
ALLOWED_CLOSE_FILE = os.path.join("ai_workspace", "db", "connection.py")


def test_no_raw_conn_close_outside_connection_module():
    result = subprocess.run(
        ["grep", "-rn", "conn.close()", "ai_workspace/", "--include=*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    offending_lines = [
        line for line in result.stdout.splitlines()
        if not line.startswith(ALLOWED_CLOSE_FILE + ":")
    ]
    assert offending_lines == [], (
        "release_connection() 대신 conn.close()를 쓰는 곳이 남아있습니다:\n"
        + "\n".join(offending_lines)
    )
