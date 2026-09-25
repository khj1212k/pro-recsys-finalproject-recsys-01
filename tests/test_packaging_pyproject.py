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
