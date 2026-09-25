import os
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_workflow():
    path = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_ci_workflow_triggers_on_push_and_pull_request():
    data = _load_workflow()
    # PyYAML은 YAML 1.1 규칙상 bare `on:` 키를 boolean True로 파싱한다(GitHub Actions
    # 자체 파서와는 무관한 PyYAML 특성) - 두 키 이름 모두에서 찾는다.
    triggers = data.get("on", data.get(True))
    assert triggers is not None
    assert "push" in triggers
    assert "pull_request" in triggers


def test_ci_workflow_uses_ubuntu_and_python_3_11_via_setup_uv():
    data = _load_workflow()
    job = data["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-latest"

    steps_text = yaml.dump(job["steps"])
    assert "astral-sh/setup-uv" in steps_text
    assert "3.11" in steps_text


def test_ci_workflow_installs_test_deps_and_ai_workspace_and_runs_pytest():
    data = _load_workflow()
    steps_text = yaml.dump(data["jobs"]["test"]["steps"])

    for dep in (
        "pytest", "langgraph", "langchain-openai", "openai", "tiktoken",
        "numpy", "pandas", "scikit-learn", "lightgbm", "optuna", "pgvector",
        "psycopg2-binary", "python-dotenv", "sqlalchemy", "sqlmodel", "alembic",
        "feedparser", "requests", "python-dateutil", "trafilatura", "tqdm",
        "pyyaml", "lxml_html_clean", "pydantic", "kiwipiepy", "hdbscan",
    ):
        assert dep in steps_text, f"CI가 {dep} 설치 단계를 포함하지 않습니다"

    assert "--no-deps" in steps_text
    assert "-e ai_workspace" in steps_text
    assert 'pytest -q -m "not integration"' in steps_text


def test_ci_workflow_yaml_parses():
    """self-review #5: .github/workflows/ci.yml이 유효한 YAML인지 명시적으로
    검증한다 (파싱 실패 시 CI 자체가 시작되지 못하므로 별도 항목으로 확인)."""
    data = _load_workflow()
    assert isinstance(data, dict)
    assert "jobs" in data


def test_ci_workflow_installs_recommend_engine_package_and_rapidfuzz():
    """self-review #5: recommend_engine은 자체 pyproject.toml을 가진 독립
    설치 단위라 ai_workspace만 -e로 설치해서는 그 패키지가 잡히지 않는다.
    rapidfuzz는 예정된 평가(evaluation) 테스트에 필요하다."""
    data = _load_workflow()
    steps_text = yaml.dump(data["jobs"]["test"]["steps"])

    assert "rapidfuzz" in steps_text, "CI가 rapidfuzz 설치 단계를 포함하지 않습니다"
    assert "-e ai_workspace/recommend_engine" in steps_text, (
        "CI가 recommend_engine 패키지를 --no-deps -e 로 설치하지 않습니다"
    )
    # recommend_engine 설치도 ai_workspace와 마찬가지로 --no-deps여야 한다
    # (의존성은 이미 위 pip install 단계에서 설치됨)
    steps = data["jobs"]["test"]["steps"]
    recommend_engine_step = next(
        s for s in steps if "-e ai_workspace/recommend_engine" in s.get("run", "")
    )
    assert "--no-deps" in recommend_engine_step["run"]


def test_ci_workflow_runs_whole_tests_tree():
    """self-review #5: pytest 실행이 tests/ 트리 전체(recommend_engine 하위
    포함)를 대상으로 하는지 명시적으로 확인한다."""
    data = _load_workflow()
    steps = data["jobs"]["test"]["steps"]
    pytest_step = next(s for s in steps if "pytest -q" in s.get("run", ""))
    assert "tests" in pytest_step["run"] or "tests/" in pytest_step["run"], (
        "pytest 실행 커맨드가 tests/ 경로를 명시하지 않습니다"
    )
    # pytest.ini의 testpaths도 tests 전체를 가리켜야 한다 (recommend_engine 하위 포함)
    pytest_ini_path = os.path.join(REPO_ROOT, "pytest.ini")
    with open(pytest_ini_path, encoding="utf-8") as f:
        ini_content = f.read()
    assert "testpaths = tests" in ini_content


def test_integration_marker_is_registered_in_pytest_ini():
    path = os.path.join(REPO_ROOT, "pytest.ini")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert "integration" in content
    assert "markers" in content


def test_pr_template_has_evidence_adr_section():
    path = os.path.join(REPO_ROOT, ".github", "PULL_REQUEST_TEMPLATE.md")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert "Evidence" in content
    assert "ADR" in content
