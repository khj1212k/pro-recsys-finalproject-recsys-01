import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if sys.version_info >= (3, 11):
    import tomllib as toml_lib

    def _load_toml(path):
        with open(path, "rb") as f:
            return toml_lib.load(f)
else:  # pragma: no cover - 이 워크트리는 3.11 고정이지만 방어적으로 남겨둠
    import tomli as toml_lib

    def _load_toml(path):
        with open(path, "rb") as f:
            return toml_lib.load(f)


def _load_pyproject():
    return _load_toml(os.path.join(REPO_ROOT, "ai_workspace", "pyproject.toml"))


HEAVY_EMBED_PACKAGES = {"torch", "flagembedding", "transformers", "sentence-transformers"}


def _pkg_name(requirement: str) -> str:
    for sep in ("==", ">=", "<=", ">", "<", "~=", "[", " "):
        if sep in requirement:
            requirement = requirement.split(sep, 1)[0]
    return requirement.strip().lower()


def test_heavy_embedding_deps_moved_to_embed_extra():
    """FIX_LOG #23 (포팅 시 변경): torch/FlagEmbedding/transformers/sentence-transformers는
    기본 의존성이 아니라 선택적 extra `embed`로 분리되어야 한다 - 이 워크트리
    환경에서는 torch/FlagEmbedding 설치가 금지되어 있고 테스트는 stub을 쓴다."""
    data = _load_pyproject()

    base_deps = {_pkg_name(d) for d in data["project"]["dependencies"]}
    assert base_deps.isdisjoint(HEAVY_EMBED_PACKAGES), (
        f"기본 dependencies에 무거운 임베딩 의존성이 섞여있습니다: {base_deps & HEAVY_EMBED_PACKAGES}"
    )

    embed_extra = {_pkg_name(d) for d in data["project"]["optional-dependencies"]["embed"]}
    assert HEAVY_EMBED_PACKAGES.issubset(embed_extra)


def test_kiwipiepy_and_hdbscan_are_base_dependencies():
    """split_v2.py가 kiwipiepy를, hdbscan_clusterer.py가 hdbscan을 실제로
    사용하지만 원래 pyproject.toml 초안에는 kiwipiepy가 빠져 있었다."""
    data = _load_pyproject()
    base_deps = {_pkg_name(d) for d in data["project"]["dependencies"]}

    assert "kiwipiepy" in base_deps
    assert "hdbscan" in base_deps


def test_test_extra_still_present():
    data = _load_pyproject()
    test_extra = {_pkg_name(d) for d in data["project"]["optional-dependencies"]["test"]}
    assert "pytest" in test_extra


def _load_requirements_txt():
    path = os.path.join(REPO_ROOT, "ai_workspace", "requirements.txt")
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    lines = [line.strip() for line in raw.splitlines()]
    pkgs = {_pkg_name(line) for line in lines if line and not line.startswith("#")}
    return raw, pkgs


def test_requirements_txt_has_kiwipiepy_and_hdbscan_consistent_with_pyproject():
    """self-review #3: split_v2.py/hdbscan_clusterer.py가 실제로 쓰는
    kiwipiepy/hdbscan이 pyproject.toml 기본 의존성에는 있지만
    requirements.txt에는 빠져있으면 안 된다 (pip install -r로 설치할 때도
    동일한 패키지 집합이 보장돼야 함)."""
    _, req_pkgs = _load_requirements_txt()
    pyproject_pkgs = {_pkg_name(d) for d in _load_pyproject()["project"]["dependencies"]}

    assert "kiwipiepy" in req_pkgs
    assert "hdbscan" in req_pkgs
    # requirements.txt는 pyproject.toml 기본 의존성의 상위집합이어야 한다
    # (torch/FlagEmbedding 등 임베딩 전용 extra + airflow는 추가로 더 들고 있음)
    assert pyproject_pkgs.issubset(req_pkgs), pyproject_pkgs - req_pkgs


def test_requirements_txt_keeps_apache_airflow():
    """requirements.txt 분리는 이후 PR 범위이므로 여기서는 airflow를 빼지 않는다."""
    _, req_pkgs = _load_requirements_txt()
    assert "apache-airflow" in req_pkgs
    assert "apache-airflow-providers-standard" in req_pkgs


def test_requirements_txt_notes_mlflow_optuna_as_optional_without_listing_them():
    """mlflow/optuna는 main_lgbm.py/tune_hyperparams.py에서 선택적으로만
    import되므로(설치 안 돼도 graceful fallback) requirements.txt 기본 목록에는
    올리지 않되, 왜 빠졌는지 짧은 주석으로 남긴다."""
    raw, req_pkgs = _load_requirements_txt()

    assert "mlflow" not in req_pkgs
    assert "optuna" not in req_pkgs

    comment_lines = [line for line in raw.splitlines() if line.strip().startswith("#")]
    comment_text = " ".join(comment_lines).lower()
    assert "mlflow" in comment_text
    assert "optuna" in comment_text
