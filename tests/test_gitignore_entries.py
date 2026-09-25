import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gitignore_lines():
    path = os.path.join(REPO_ROOT, ".gitignore")
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f.read().splitlines()]


def test_gitignore_covers_self_review_4_patterns():
    """self-review #4: /data/, reports/**/raw/, mlruns/, checkpoints/, .claude/,
    .venv-*/ 가 (로컬 전용인 .git/info/exclude가 아니라) 저장소에 커밋되는
    .gitignore 자체에 있어야 다른 협업자/CI에서도 동일하게 무시된다."""
    lines = _gitignore_lines()

    required = {
        "/data/",
        "reports/**/raw/",
        "mlruns/",
        "checkpoints/",
        ".claude/",
        ".venv-*/",
    }
    missing = required - set(lines)
    assert not missing, f".gitignore에 빠진 패턴: {missing}"
