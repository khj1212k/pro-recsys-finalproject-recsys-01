import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_fix_log_lives_under_docs_and_root_copy_absent():
    docs_path = os.path.join(REPO_ROOT, "docs", "fix-log-2026-07.md")
    root_path = os.path.join(REPO_ROOT, "FIX_LOG.md")

    assert os.path.exists(docs_path), "docs/fix-log-2026-07.md가 존재하지 않습니다"
    assert not os.path.exists(root_path), "레포 루트에 FIX_LOG.md가 남아있으면 안 됩니다 (docs/로 이동됨)"


def test_fix_log_no_longer_cites_nonexistent_portfolio_review_as_source():
    """원문 1번째 문단은 '포트폴리오 리뷰(`PORTFOLIO_REVIEW.md`) §3에서 식별된...'
    형태로, 저장소에 존재하지 않는 문서를 출처로 인용하고 있었다. 포팅 노트에서
    이 사실을 설명하며 파일명을 언급하는 것과 별개로, 더 이상 그 문서를 실제
    출처인 것처럼 인용하는 문장은 남아있으면 안 된다."""
    docs_path = os.path.join(REPO_ROOT, "docs", "fix-log-2026-07.md")
    with open(docs_path, encoding="utf-8") as f:
        content = f.read()

    assert "포트폴리오 리뷰(`PORTFOLIO_REVIEW.md`) §3에서 식별된" not in content

    portfolio_review_path = os.path.join(REPO_ROOT, "PORTFOLIO_REVIEW.md")
    assert not os.path.exists(portfolio_review_path)


def test_fix_log_has_2026_09_porting_note_with_corrections():
    docs_path = os.path.join(REPO_ROOT, "docs", "fix-log-2026-07.md")
    with open(docs_path, encoding="utf-8") as f:
        content = f.read()

    assert "2026-09" in content
    assert "#8" in content
    assert "#20" in content
    assert "#24" in content


def test_adr_files_ported_under_docs_adr():
    adr_dir = os.path.join(REPO_ROOT, "docs", "adr")
    expected = {
        "0001-langgraph-for-newsletter-generation.md",
        "0002-hdbscan-for-news-clustering.md",
        "0003-lightgbm-mmr-for-recommendation.md",
    }
    actual = set(os.listdir(adr_dir))
    assert expected.issubset(actual)


def test_portfolio_md_not_ported():
    assert not os.path.exists(os.path.join(REPO_ROOT, "PORTFOLIO.md"))
