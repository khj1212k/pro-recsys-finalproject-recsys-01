import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _readme_text():
    path = os.path.join(REPO_ROOT, "README.md")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _test_run_section():
    """self-review #6: README의 "### 4. 테스트 실행" 섹션 본문만 추출한다
    (이 브랜치에서 모든 커맨드를 실제로 실행해 검증한 대상)."""
    text = _readme_text()
    match = re.search(r"### 4\. 테스트 실행\n(.*?)\n---", text, re.DOTALL)
    assert match, "README에 '### 4. 테스트 실행' 섹션이 없습니다"
    return match.group(1)


def test_readme_test_run_commands_match_verified_working_commands():
    """이 브랜치의 venv에서 실제로 실행해 확인한 커맨드들이 README에
    그대로 남아있는지 확인한다 (pip install -e 3종 + pytest 2종)."""
    section = _test_run_section()

    for command in (
        'pip install -e "ai_workspace[test]"',
        'pip install -e "ai_workspace[test,embed]"',
        'pip install -e "ai_workspace/recommend_engine[test]"',
        "pytest",
        'pytest -m "not integration"',
    ):
        assert command in section, f"README 테스트 실행 섹션에 '{command}' 커맨드가 없습니다"


def test_readme_test_run_section_references_existing_extras():
    """README가 언급하는 pyproject.toml optional-dependencies가 실제로
    존재하는지 확인한다 (extra 이름이 바뀌면 pip install -e "...[test]"가 깨짐)."""
    import sys

    if sys.version_info >= (3, 11):
        import tomllib as toml_lib

        def _load(path):
            with open(path, "rb") as f:
                return toml_lib.load(f)
    else:  # pragma: no cover
        import tomli as toml_lib

        def _load(path):
            with open(path, "rb") as f:
                return toml_lib.load(f)

    ai_workspace_extras = _load(os.path.join(REPO_ROOT, "ai_workspace", "pyproject.toml"))[
        "project"
    ]["optional-dependencies"]
    assert "test" in ai_workspace_extras
    assert "embed" in ai_workspace_extras

    recommend_engine_extras = _load(
        os.path.join(REPO_ROOT, "ai_workspace", "recommend_engine", "pyproject.toml")
    )["project"]["optional-dependencies"]
    assert "test" in recommend_engine_extras


def test_readme_performance_table_does_not_reintroduce_unreproducible_claims():
    """self-review #6: 코드로 재현 불가능한 수치(응답시간/다양성 %)가 성능
    지표 표에 다시 등장하면 안 된다. '재현 가능'을 주장하는 스크립트 경로는
    실제로 저장소에 존재해야 한다."""
    text = _readme_text()

    for banned in ("<100ms", "56.4%"):
        occurrences = text.count(banned)
        # 제거했다는 설명 문장 자체에는 남아있어도 되지만, 그 설명 문장 밖에서
        # 실측값인 것처럼 다시 쓰이면 안 된다 -> "이전 버전에는" 문맥에서만 등장해야 함
        assert occurrences <= 1, f"'{banned}' 수치가 여러 곳에서 재도입된 것으로 보입니다"

    script_paths = re.findall(r"`(ai_workspace/[^`]+\.py)`", text)
    for rel_path in script_paths:
        abs_path = os.path.join(REPO_ROOT, rel_path)
        assert os.path.exists(abs_path), f"README가 재현 가능하다고 인용한 스크립트가 없습니다: {rel_path}"
