"""docs/demo/의 상대 링크가 모두 실제 파일을 가리키는지, 데모를 가리키는 다른 문서의 링크도 맞는지."""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "docs" / "demo"
# 데모로 가는 링크를 단 문서들(ADR 0009·ADR 색인·실행 계획은 `docs/demo/`를 코드 표기로만 적는다).
POINTERS = [
    "README.md",
    "docs/case-studies/03-first-llm-measurement.md",
    "docs/case-studies/README.md",
    "docs/evidence-summary.md",
    "docs/adr/0023-data-sources-copyright-retention.md",
]
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def relative_links(path: Path):
    text = path.read_text(encoding="utf-8")
    for target in _LINK.findall(text):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
            continue  # http(s), mailto, 같은 문서 안 앵커
        yield target


def resolve(path: Path, target: str) -> Path:
    return (path.parent / target.split("#", 1)[0].split("?", 1)[0]).resolve()


def demo_pages():
    return sorted(DEMO.rglob("*.md"))


@pytest.mark.skipif(not DEMO.exists(), reason="docs/demo가 아직 없다")
@pytest.mark.parametrize("page", demo_pages(), ids=lambda p: str(p.relative_to(DEMO)))
def test_demo_relative_links_resolve(page):
    broken = [t for t in relative_links(page) if not resolve(page, t).exists()]
    assert broken == []


@pytest.mark.skipif(not DEMO.exists(), reason="docs/demo가 아직 없다")
@pytest.mark.parametrize("doc", POINTERS)
def test_links_pointing_at_the_demo_resolve(doc):
    path = REPO / doc
    links = [t for t in relative_links(path) if "demo/" in t]
    assert links, f"{doc}에 데모 링크가 없다"
    assert [t for t in links if not resolve(path, t).exists()] == []


@pytest.mark.skipif(not DEMO.exists(), reason="docs/demo가 아직 없다")
def test_every_demo_page_is_linked_from_the_index():
    index = (DEMO / "README.md").read_text(encoding="utf-8")
    pages = [p.name for p in (DEMO / "newsletters").glob("*.md")]
    assert pages and all(f"newsletters/{name}" in index for name in pages)
