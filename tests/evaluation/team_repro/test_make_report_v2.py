"""make_report_v2.py 테스트: team_repro_v2.json 형태의 최소 픽스처로부터
마크다운을 생성해 깨지지 않는지, v2가 반드시 담아야 하는 절(요구사항 9) - 요약/
데이터 구조/프로토콜/결과표/분해실험/증명 불가/다음 단계/이력서 문장 - 이 실제로
나오는지 검증한다. 숫자 자체는 재계산하지 않는다."""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEAM_REPRO_DIR = REPO_ROOT / "evaluation" / "recsys" / "team_repro"
sys.path.insert(0, str(TEAM_REPRO_DIR))


def _metric_summary(mean=0.5, std=0.1, n_seeds=3, best_iteration=None):
    d = {
        "mrr": {"mean": mean, "std": std, "values": [mean]},
        "precision@5": {"mean": mean, "std": std, "values": [mean]},
        "ndcg@5": {"mean": mean, "std": std, "values": [mean]},
        "coverage@5": {"mean": mean, "std": std, "values": [mean]},
        "n_seeds": n_seeds,
    }
    if best_iteration is not None:
        d["best_iteration"] = best_iteration
    return d


def _ci_block(mean=0.5, lo=0.4, hi=0.6, n=10):
    return {"mean": mean, "ci_lo": lo, "ci_hi": hi, "n_users": n, "n_boot": 1000}


def _effect(effect=0.1, lo=-0.05, hi=0.25, n=10):
    return {"effect": effect, "ci_lo": lo, "ci_hi": hi, "n_users": n, "n_boot": 1000, "mean_a": 0.4, "mean_b": 0.5}


def _baseline_row(mean=0.3, cold=0.2, warm=0.4, n_cold=5, n_warm=10):
    return {
        "aggregate": {"mrr": mean, "precision@5": mean, "ndcg@5": mean, "coverage@5": mean},
        "cold": {"mrr": cold, "precision@5": cold, "num_users": n_cold},
        "warm": {"mrr": warm, "precision@5": warm, "num_users": n_warm},
        "seen_filtered": {"aggregate": {"mrr": mean * 0.9, "precision@5": mean * 0.9}},
        "seen_share_top5": 0.3,
    }


@pytest.fixture()
def fake_report_json(tmp_path, monkeypatch):
    report_dir = tmp_path / "reports" / "recsys"
    report_dir.mkdir(parents=True)

    data = {
        "meta": {
            "generated_at": "2026-09-25T00:00:00",
            "report_version": "v2",
            "superseded_report": "team_repro_v1",
            "git": {"harness_sha": "abc123", "current_branch": "eval/x", "team_final_sha": "def456", "fix_snapshot_sha": "ghi789"},
            "config_hashes": {"current|none": "hash1", "team-final|none": "hash2", "fix-snapshot|none": "hash3"},
            "config_overrides": {},
            "data_sha256": {"a.csv": "hash1"},
            "dataset_window": {"start": "2026-01-30T00:00:00", "end": "2026-01-31T00:00:00", "n_users": 100, "n_newsletters": 195, "n_ctr_logs": 19500, "n_clicks": 9281},
            "seeds": {
                "headline_current_team_final": [42, 43, 44],
                "headline_fix_snapshot": [42, 43],
                "decomposition_secondary": [42, 43],
            },
            "category_recovery": {
                "chosen_k": 5,
                "loo_accuracy_by_k": {"1": 0.1489, "5": 0.2979},
                "n_direct_labels": 47,
                "source_counts": {"knn": 148, "onboarding_log": 42, "json_title": 5},
                "loo_ci_chosen_k_wilson95": [0.19, 0.44],
                "loo_ci_note": "best-of-9라 낙관적",
            },
            "answer_start": "2026-01-31T00:07:44",
        },
        "data_structure": {
            "n_users_total": 100, "n_newsletters": 195, "n_ctr_logs": 19500, "n_clicks": 9281,
            "click_rate": 0.4759, "dataset_window_start": "2026-01-30T19:26:00", "dataset_window_end": "2026-01-31T01:42:00",
            "dataset_window_seconds": 22560.0, "exposures_per_persona_min": 195, "exposures_per_persona_max": 195,
            "session_span_seconds_mean": 3600.0, "answer_start": "2026-01-31T00:07:44",
            "n_cold_users": 15, "n_warm_users": 16, "n_evaluated_users_in_answer_window": 31, "n_answer_clicks": 120,
            "random_p5_base_rate_mean_answer_density": 0.32,
        },
        "headline": {
            "team-final": {"primary_summary": _metric_summary(0.5, best_iteration=[36, 1, 1]), "as_written_summary": _metric_summary(0.85)},
            "fix-snapshot": {"primary_summary": _metric_summary(0.55), "as_written_summary": _metric_summary(0.79)},
            "current": {"primary_summary": _metric_summary(0.59, best_iteration=[36, 1, 1, 7, 1]), "as_written_summary": _metric_summary(0.77)},
        },
        "headline_ci": {
            "team-final": {"primary": {"mrr": _ci_block(), "precision@5": _ci_block()}, "as_written": {"mrr": _ci_block(), "precision@5": _ci_block()}, "leak_effect": {"mrr": _effect(), "precision@5": _effect()}},
            "fix-snapshot": {"primary": {"mrr": _ci_block(), "precision@5": _ci_block()}, "as_written": {"mrr": _ci_block(), "precision@5": _ci_block()}, "leak_effect": {"mrr": _effect(), "precision@5": _effect()}},
            "current": {"primary": {"mrr": _ci_block(), "precision@5": _ci_block()}, "as_written": {"mrr": _ci_block(), "precision@5": _ci_block()}, "leak_effect": {"mrr": _effect(), "precision@5": _effect()}},
        },
        "team_final_written_summary": {"mrr": {"mean": 0.99, "std": 0.01, "values": [0.99]}, "precision@5": {"mean": 0.96, "std": 0.01, "values": [0.96]}, "n_seeds": 3, "note": "NOW()-6일 재현"},
        "generator_split": {
            "current": {"summary": _metric_summary(0.35)},
            "team-final": {"summary": _metric_summary(0.40)},
        },
        "baselines": {
            "team_split": {"random": _baseline_row(0.32), "popularity": _baseline_row(0.48), "cosine_history": _baseline_row(0.6)},
            "generator_split": {"random": {"aggregate": {"mrr": 0.1, "precision@5": 0.1, "ndcg@5": 0.1, "coverage@5": 0.1}}},
        },
        "decomposition_summary": {
            "leakage": {"fixed_summary": {}, "leaky_summary": {}},
            "negatives": {"random_summary": {}, "impression_summary": {}},
            "objective": {"lambdarank_summary": {}, "binary_summary": {}},
            "candidate_pool": {"full_195_summary": {}, "small_recent_15_summary": {}, "padded_400_summary": {}},
        },
        "decomposition_bootstrap": {
            "leakage": {"mrr": _effect(0.002), "precision@5": _effect(0.0)},
            "negatives": {"mrr": _effect(-0.02), "precision@5": _effect(-0.01)},
            "objective": {"mrr": _effect(-0.01), "precision@5": _effect(0.0)},
            "candidate_pool": {
                "full_vs_small_mrr": _effect(-0.09), "full_vs_small_precision@5": _effect(-0.1),
                "full_vs_padded_mrr": _effect(-0.01), "full_vs_padded_precision@5": _effect(0.0),
            },
        },
    }
    (report_dir / "team_repro_v2.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    import make_report_v2

    monkeypatch.setattr(make_report_v2, "WORKTREE_ROOT", tmp_path)
    monkeypatch.setattr(make_report_v2, "REPORT_DIR", report_dir)
    return make_report_v2, report_dir


def test_make_report_v2_writes_markdown(fake_report_json):
    make_report_v2, report_dir = fake_report_json
    make_report_v2.main()
    out = report_dir / "team_repro_v2.md"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "team_repro v2" in text


def test_all_required_sections_present(fake_report_json):
    make_report_v2, report_dir = fake_report_json
    make_report_v2.main()
    text = (report_dir / "team_repro_v2.md").read_text(encoding="utf-8")
    for heading_fragment in ["요약", "데이터 구조", "프로토콜", "베이스라인 비교", "분해 실험", "증명할 수 없는", "다음 단계", "이력서"]:
        assert heading_fragment in text, f"필수 절 누락: {heading_fragment!r}"


def test_label_assumption_not_a_decomposition_table_row(fake_report_json):
    """v2는 label_assumption(all_rows) 분해실험 자체를 제거했다 - 표의 데이터 행으로
    남아있으면 회귀(설명 문구에서 실험명을 언급하는 것 자체는 허용)."""
    make_report_v2, report_dir = fake_report_json
    make_report_v2.main()
    text = (report_dir / "team_repro_v2.md").read_text(encoding="utf-8")
    assert "| label_assumption |" not in text


def test_claims_to_avoid_section_lists_leaky_headline_gap(fake_report_json):
    make_report_v2, report_dir = fake_report_json
    make_report_v2.main()
    text = (report_dir / "team_repro_v2.md").read_text(encoding="utf-8")
    assert "0.849" in text and "0.772" in text
