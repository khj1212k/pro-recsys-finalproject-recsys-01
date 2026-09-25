"""make_report.py 테스트: team_repro_v1.json 형태의 최소 픽스처로부터 마크다운을
생성해, 표 형식이 깨지지 않는지(특히 분해 실험 표의 괄호 짝, section 1-2의
generator_split 경고 문구 포함 여부)를 검증한다. 실제 리포트 숫자를 재계산하지
않는다 - make_report.py 자체가 순수 포매팅 함수이므로 작은 가짜 입력으로 충분하다.
"""
import importlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEAM_REPRO_DIR = REPO_ROOT / "evaluation" / "recsys" / "team_repro"
sys.path.insert(0, str(TEAM_REPRO_DIR))


def _metric_summary(mean=0.5, std=0.1, n_seeds=3, num_users=10):
    return {
        "mrr": {"mean": mean, "std": std, "values": [mean]},
        "precision@5": {"mean": mean, "std": std, "values": [mean]},
        "ndcg@5": {"mean": mean, "std": std, "values": [mean]},
        "ndcg@10": {"mean": mean, "std": std, "values": [mean]},
        "coverage@5": {"mean": mean, "std": std, "values": [mean]},
        "auc": {"mean": mean, "std": std, "values": [mean]},
        "num_users": {"mean": num_users},
        "n_seeds": n_seeds,
    }


def _ci(mean=0.5, lo=0.4, hi=0.6, n=10):
    return {"mrr": {"mean": mean, "ci_lo": lo, "ci_hi": hi, "n_users": n, "n_boot": 1000},
            "precision@5": {"mean": mean, "ci_lo": lo, "ci_hi": hi, "n_users": n, "n_boot": 1000},
            "ndcg@5": {"mean": mean, "ci_lo": lo, "ci_hi": hi, "n_users": n, "n_boot": 1000},
            "coverage@5": {"mean": mean, "ci_lo": lo, "ci_hi": hi, "n_users": n, "n_boot": 1000}}


def _effect(effect=0.1, lo=-0.05, hi=0.25, n=10):
    return {"effect": effect, "ci_lo": lo, "ci_hi": hi, "n_users": n, "n_boot": 1000,
            "mean_a": 0.4, "mean_b": 0.5}


def _baseline_row(mean=0.3):
    return {"mrr": mean, "precision@5": mean, "ndcg@5": mean, "coverage@5": mean}


@pytest.fixture()
def fake_report_json(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports" / "recsys"
    report_dir.mkdir(parents=True)

    data = {
        "meta": {
            "generated_at": "2026-09-25T00:00:00",
            "git": {"current_branch": "eval/x", "current_head": "abc123", "team_final_sha": "def456", "fix_snapshot_sha": "ghi789"},
            "data_sha256": {"a.csv": "hash1"},
            "dataset_window": {"start": "2026-01-30T00:00:00", "end": "2026-01-31T00:00:00", "n_users": 100, "n_newsletters": 195, "n_ctr_logs": 19500, "n_clicks": 9281},
            "seeds": {"headline": [42, 43], "decomposition_secondary": [42]},
            "category_recovery": {
                "chosen_k": 5,
                "loo_accuracy_by_k": {"1": 0.1489, "3": 0.1915, "5": 0.2979, "7": 0.2766},
                "n_direct_labels": 47,
                "source_counts": {"knn": 148, "onboarding_log": 42, "json_title": 5},
            },
        },
        "headline": {
            "team-final": {"summary": _metric_summary()},
            "fix-snapshot": {"summary": _metric_summary()},
            "current": {"summary": _metric_summary()},
        },
        "headline_ci": {
            "team-final": _ci(), "fix-snapshot": _ci(), "current": _ci(),
        },
        "generator_split": {
            "current": {"summary": _metric_summary()},
            "team-final": {"summary": _metric_summary()},
        },
        "baselines": {
            "team_split": {"random": _baseline_row(), "popularity": _baseline_row(0.0)},
            "generator_split": {"random": _baseline_row(), "popularity": _baseline_row(0.0)},
        },
        "decomposition_summary": {
            "leakage": {"fixed_summary": {}, "leaky_summary": {}},
            "negatives": {"random_summary": {}, "impression_summary": {}},
            "label_assumption": {"clicks_only_summary": {}, "all_rows_summary": {}},
            "objective": {"lambdarank_summary": {}, "binary_summary": {}},
        },
        "decomposition_bootstrap": {
            "leakage": {"mrr": _effect(), "precision@5": _effect(), "ndcg@5": _effect(), "coverage@5": _effect()},
            "negatives": {"mrr": _effect(), "precision@5": _effect(), "ndcg@5": _effect(), "coverage@5": _effect()},
            "label_assumption": {"mrr": _effect(), "precision@5": _effect(), "ndcg@5": _effect(), "coverage@5": _effect()},
            "objective": {"mrr": _effect(), "precision@5": _effect(), "ndcg@5": _effect(), "coverage@5": _effect()},
            "candidate_pool": {
                "full_vs_small_mrr": _effect(), "full_vs_small_precision@5": _effect(),
                "full_vs_small_ndcg@5": _effect(), "full_vs_small_coverage@5": _effect(),
                "full_vs_padded_mrr": _effect(), "full_vs_padded_precision@5": _effect(),
                "full_vs_padded_ndcg@5": _effect(), "full_vs_padded_coverage@5": _effect(),
            },
        },
    }
    (report_dir / "team_repro_v1.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    import make_report

    monkeypatch.setattr(make_report, "WORKTREE_ROOT", tmp_path)
    monkeypatch.setattr(make_report, "REPORT_DIR", report_dir)
    return make_report, report_dir


def test_make_report_writes_markdown(fake_report_json):
    make_report, report_dir = fake_report_json
    make_report.main()
    out = report_dir / "team_repro_v1.md"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "team_repro v1" in text


def test_decomposition_table_rows_have_balanced_parens(fake_report_json):
    """섹션 3 표의 각 행에서 '(' 개수와 ')' 개수가 일치해야 한다(과거 split() 기반
    포매팅 버그가 짝이 안 맞는 괄호를 만들었다 - 회귀 방지)."""
    make_report, report_dir = fake_report_json
    make_report.main()
    text = (report_dir / "team_repro_v1.md").read_text(encoding="utf-8")

    table_lines = [
        line for line in text.splitlines()
        if line.startswith("| ") and any(name in line for name in ["leakage", "negatives", "label_assumption", "objective"])
        and "조건 A" not in line
    ]
    assert table_lines, "분해 실험 표 데이터 행을 찾지 못했습니다."
    for line in table_lines:
        assert line.count("(") == line.count(")"), f"괄호 짝이 안 맞음: {line!r}"


def test_category_recovery_loo_accuracy_surfaced(fake_report_json):
    """스펙 1번("k는 ... leave-one-out 교차검증으로 선택한다. report LOO accuracy and
    per-source counts")이 실제로 리포트 본문에 반영돼야 한다 - 숫자로 묻힌 JSON이
    아니라 사람이 읽는 markdown에 LOO 정확도와 소스별 건수가 나와야 한다."""
    make_report, report_dir = fake_report_json
    make_report.main()
    text = (report_dir / "team_repro_v1.md").read_text(encoding="utf-8")

    assert "29.8" in text or "0.2979" in text  # LOO 정확도(k=5) 값이 어딘가에 등장
    assert "148" in text  # knn 소스 건수
    assert "LOO" in text


def test_generator_split_disjoint_id_caveat_present(fake_report_json):
    """generator_split을 '랜덤 분할'이라고 잘못 표현하지 않고, 뉴스레터 id가 겹치지
    않는 분할이라는 경고 문구가 리포트에 포함돼야 한다 (실측: train은 news_letter_id
    4~154, valid는 155~198로 완전히 분리됨 - popularity 베이스라인이 정확히 0인 이유)."""
    make_report, report_dir = fake_report_json
    make_report.main()
    text = (report_dir / "team_repro_v1.md").read_text(encoding="utf-8")

    assert "생성기 자체 랜덤 분할" not in text
    assert "겹치지 않는다" in text or "겹치지 않는" in text
    assert "cold" in text.lower() or "콜드" in text


def test_fmt_effect_val_and_ci_only_are_consistent_with_fmt_effect():
    import make_report

    d = {"effect": -0.1234, "ci_lo": -0.5, "ci_hi": 0.3, "n_users": 17, "n_boot": 1000}
    combined = make_report.fmt_effect(d)
    val = make_report.fmt_effect_val(d)
    ci = make_report.fmt_ci_only(d)
    assert val in combined
    assert "n=17" in ci
    assert ci.count("(") == ci.count(")")
