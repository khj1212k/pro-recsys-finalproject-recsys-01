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
