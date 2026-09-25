import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADR_DIR = os.path.join(REPO_ROOT, "docs", "adr")


def _read(name):
    with open(os.path.join(ADR_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_adr_0004_exists_with_no_leftover_placeholder():
    content = _read("0004-continue-in-fork-and-port-july-fixes.md")
    assert "{PORT_RESULTS}" not in content, "ADR 0004에 {PORT_RESULTS} 플레이스홀더가 그대로 남아있습니다"


def test_adr_0004_keeps_draft_structure_and_korean_headings():
    content = _read("0004-continue-in-fork-and-port-july-fixes.md")
    for heading in ("## 상태", "## 컨텍스트", "## 검토한 대안", "## 결정", "## 증거", "## 결과와 한계"):
        assert heading in content, f"ADR 0004에 '{heading}' 섹션이 없습니다"


def test_adr_0004_evidence_cites_real_test_totals_and_correction_files():
    content = _read("0004-continue-in-fork-and-port-july-fixes.md")

    # 실제로 이 브랜치에서 재현 가능한 테스트 총계여야 한다 (pytest -q로 재확인 가능)
    assert "115" in content

    for correction_test_file in (
        "test_history_embedding_leakage.py",
        "test_lgbm_ranker_lambdarank.py",
        "test_lambdarank_group_split.py",
        "test_cluster_retry_count.py",
        "test_cluster_eval_retry_graph.py",
        "test_negative_sampling_seed.py",
    ):
        assert correction_test_file in content, f"ADR 0004 증거 섹션에 {correction_test_file} 인용이 없습니다"

    for correction_id in ("#4", "#5", "#8", "#16"):
        assert correction_id in content


def test_adr_0004_states_portfolio_md_dropped():
    content = _read("0004-continue-in-fork-and-port-july-fixes.md")
    assert "PORTFOLIO.md" in content
    assert "제외" in content or "drop" in content.lower()


def test_adr_0004_cited_test_files_and_counts_exist_on_disk():
    """증거 섹션이 인용한 테스트 파일이 실제로 존재하고, 인용한 개수와 실제
    def test_* 함수 개수가 일치하는지 교차 검증한다."""
    content = _read("0004-continue-in-fork-and-port-july-fixes.md")

    expected_counts = {
        os.path.join(REPO_ROOT, "tests", "recommend_engine", "test_history_embedding_leakage.py"): 6,
        os.path.join(REPO_ROOT, "tests", "recommend_engine", "test_lgbm_ranker_lambdarank.py"): 4,
        os.path.join(REPO_ROOT, "tests", "recommend_engine", "test_lambdarank_group_split.py"): 7,
        os.path.join(REPO_ROOT, "tests", "test_cluster_retry_count.py"): 4,
        os.path.join(REPO_ROOT, "tests", "test_cluster_eval_retry_graph.py"): 2,
        os.path.join(REPO_ROOT, "tests", "recommend_engine", "test_negative_sampling_seed.py"): 7,
    }
    for path, expected in expected_counts.items():
        assert os.path.exists(path), f"인용된 테스트 파일이 없습니다: {path}"
        with open(path, encoding="utf-8") as f:
            actual = len(re.findall(r"^def test_", f.read(), re.MULTILINE))
        assert actual == expected, f"{path}의 테스트 수가 ADR 인용({expected})과 다릅니다: 실제 {actual}"

    assert str(sum(expected_counts.values())) in content or "6건" in content


def test_adr_readme_index_lists_all_four_adrs_with_status_and_date():
    content = _read("README.md")
    for number in ("0001", "0002", "0003", "0004"):
        assert number in content, f"docs/adr/README.md 인덱스에 {number}가 없습니다"
    assert "상태" in content
    assert "날짜" in content


def test_adr_dir_has_exactly_four_numbered_adrs_plus_index():
    files = sorted(os.listdir(ADR_DIR))
    numbered = [f for f in files if re.match(r"^\d{4}-", f)]
    assert len(numbered) == 4, f"docs/adr/에 번호가 매겨진 ADR이 4개가 아닙니다: {numbered}"
    assert "README.md" in files
