import os
import glob

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# FIX_LOG #24가 db/connection.py의 DB 접속 정보를 Settings로 중앙화하면서도
# 놓친 부분: .env.example에는 여전히 실제 비밀번호가 평문으로 커밋돼 있었다.
KNOWN_LEAKED_PASSWORD = "recsyspeople"


def test_no_env_example_file_contains_the_known_leaked_password():
    env_example_files = glob.glob(
        os.path.join(REPO_ROOT, "**", ".env.example"), recursive=True
    )
    assert env_example_files, ".env.example 파일을 하나도 찾지 못했습니다 (경로 변경 여부 확인 필요)"

    for path in env_example_files:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert KNOWN_LEAKED_PASSWORD not in content, (
            f"{path}에 평문 비밀번호('{KNOWN_LEAKED_PASSWORD}')가 남아있습니다"
        )


def test_ai_workspace_env_example_uses_placeholder_password():
    path = os.path.join(REPO_ROOT, "ai_workspace", ".env.example")
    with open(path, encoding="utf-8") as f:
        content = f.read()

    assert "DB_PASSWORD=" in content
    for line in content.splitlines():
        if line.startswith("DB_PASSWORD="):
            value = line.split("=", 1)[1].strip()
            assert value != KNOWN_LEAKED_PASSWORD
            assert value == "" or "your-" in value or "placeholder" in value.lower()
