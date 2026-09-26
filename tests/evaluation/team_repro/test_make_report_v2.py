"""make_report_v2.py 테스트: team_repro_v2.json(v2.1 형식)의 최소 픽스처로부터 마크다운을
생성해 깨지지 않는지, 필수 절이 나오는지, 그리고 v2 재검토에서 지적된 문장 오류가 다시
생기지 않는지(0.897 표기, 팀 귀속 문장, 정의 오류 행 라벨, CI 부호로 고른 결론 단어)를
검증한다. 숫자 자체는 재계산하지 않는다."""
import copy
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEAM_REPRO_DIR = REPO_ROOT / "evaluation" / "recsys" / "team_repro"
sys.path.insert(0, str(TEAM_REPRO_DIR))

METRICS = ["mrr", "precision@5", "ndcg@5", "coverage@5"]


def _summary(mean=0.5, std=0.05, n_seeds=5, best_iteration=None, degenerate=0, tie=False):
    d = {mk: {"mean": mean, "std": std, "values": [mean] * n_seeds} for mk in METRICS}
    d.update({
        "n_seeds": n_seeds, "seeds": list(range(42, 42 + n_seeds)), "num_users": 31,
        "best_iteration": best_iteration or [35] * n_seeds, "n_trees": best_iteration or [35] * n_seeds,
        "n_degenerate_seeds": degenerate, "n_distinct_scores_primary": [2983] * n_seeds,
        "top_tie_size_eval_users_median": [3.0] * n_seeds,
    })
    if tie:
        d["tie_draw_range"] = {mk: [mean - 0.1, mean + 0.1] for mk in METRICS}
    return d


def _ci(mean=0.5, lo=0.4, hi=0.6):
    return {"mean": mean, "ci_lo": lo, "ci_hi": hi, "n_users": 31, "n_seeds": 5, "n_boot": 1000}


def _eff(effect=0.0, lo=-0.1, hi=0.1, paired=False):
    return {
        "effect": effect, "ci_lo": lo, "ci_hi": hi, "n_users": 31, "n_seeds_a": 5, "n_seeds_b": 5,
        "n_boot": 1000, "mean_a": 0.5, "mean_b": 0.5 + effect, "paired_seeds": paired,
    }


def _pair(effect=0.0, lo=-0.1, hi=0.1, paired=False):
    return {"mrr": _eff(effect, lo, hi, paired), "precision@5": _eff(effect, lo, hi, paired)}


def _baseline(mean=0.4, with_groups=True):
    d = {"aggregate": {mk: mean for mk in METRICS}, "n_draws": 1}
    if with_groups:
        d.update({
            "cold": {"mrr": mean, "precision@5": mean, "num_users": 15},
            "warm": {"mrr": mean, "precision@5": mean, "num_users": 16},
            "seen_filtered": {"aggregate": {mk: mean for mk in METRICS}},
            "unshown_filtered": {"aggregate": {mk: mean for mk in METRICS}},
            "seen_share_top5": 0.3,
        })
    return d


BASELINE_NAMES = ["random", "fixed_csv_order", "popularity", "recency", "category_match", "cosine_history", "onboarding_newsletter_cosine"]


def _base_data() -> dict:
    headline_block = lambda p, aw, bi=None, deg=0: {  # noqa: E731
        "primary_summary": _summary(p, best_iteration=bi, degenerate=deg),
        "primary_tie_random_summary": _summary(p - 0.04, tie=True),
        "as_written_summary": _summary(aw, best_iteration=bi, degenerate=deg),
        "as_written_tie_random_summary": _summary(aw - 0.01, tie=True),
        "primary_cold": {"mrr": p, "precision@5": p, "num_users": 15, "n_seeds": 5},
        "primary_warm": {"mrr": p, "precision@5": p, "num_users": 16, "n_seeds": 5},
        "primary_seen_filtered_summary": _summary(p + 0.05),
        "primary_unshown_filtered_summary": _summary(p + 0.1),
    }
    decomp_block = lambda m: {  # noqa: E731
        "primary": _summary(m), "primary_tie_random": _summary(m - 0.02, tie=True),
        "primary_seen_filtered": _summary(m), "primary_unshown_filtered": _summary(m),
        "primary_cold": {"mrr": m, "precision@5": m, "num_users": 15}, "primary_warm": {"mrr": m, "precision@5": m, "num_users": 16},
    }
    mvb_entry = {"plain": _pair(-0.1, -0.2, -0.01), "unshown_filtered": _pair(), "plain_tie_random": _pair()}
    return {
        "meta": {
            "generated_at": "2026-09-26T00:00:00", "report_version": "v2.1",
            "git": {
                "harness_sha": "abc123", "harness_dirty": False, "current_branch": "eval/x",
                "input_content_hashes": {"harness_pipeline_inputs": "h" * 64, "current_engine": "e" * 64},
                "team_final_sha": "def456", "fix_snapshot_sha": "ghi789",
            },
            "config_hashes": {"current|none": "hash1"}, "config_overrides": {"current|binary": ["objective:lambdarank->binary"]},
            "data_sha256": {"a.csv": "hash1"},
            "seeds": {"current_team_final_and_all_current_arms": [42, 43, 44, 45, 46], "fix_snapshot": [42, 43, 44]},
            "tie_draws": 30, "random_baseline_draws": 100, "n_boot": 1000,
            "category_recovery": {
                "chosen_k": 5, "loo_accuracy_by_k": {"5": 0.2979}, "n_direct_labels": 47,
                "source_counts": {"knn": 148, "onboarding_log": 42, "json_title": 5},
                "loo_ci_chosen_k_wilson95": [0.19, 0.44], "loo_ci_note": "best-of-9라 낙관적.",
            },
            "answer_start": "2026-01-31T00:07:44",
            "cache_key_fields": ["version", "seed"],
        },
        "data_structure": {
            "n_users_total": 100, "n_newsletters": 195, "n_ctr_logs": 19500, "n_clicks": 9281, "click_rate": 0.4759,
            "dataset_window_start": "2026-01-30T19:26:19", "dataset_window_end": "2026-01-31T01:42:15",
            "dataset_window_seconds": 22556.0, "exposures_per_persona_min": 195, "exposures_per_persona_max": 195,
            "session_span_seconds_mean": 3642.0, "answer_start": "2026-01-31T00:07:44",
            "n_cold_users": 15, "n_warm_users": 85, "n_evaluated_users_in_answer_window": 31,
            "n_evaluated_cold_users": 15, "n_evaluated_warm_users": 16, "n_answer_clicks": 1857,
            "random_p5_base_rate_mean_answer_density": 0.3072, "n_answers_shown_before_answer_start": 0,
            "answer_density_among_unshown_mean": 0.405,
            "generator_split": {
                "n_valid_items": 44, "n_train_items": 151, "valid_id_range": [155, 198], "train_id_range": [4, 154],
                "valid_items_are_exactly_newest": True, "train_items_max_created_at": "2026-01-26",
                "valid_items_min_created_at": "2026-01-27",
            },
        },
        "headline": {
            "current": headline_block(0.57, 0.76, bi=[35, 1, 1, 7, 1], deg=3),
            "team-final": headline_block(0.54, 0.84),
            "fix-snapshot": headline_block(0.59, 0.79),
        },
        "headline_ci": {
            v: {
                "primary": {mk: _ci() for mk in METRICS}, "as_written": {mk: _ci() for mk in METRICS},
                "leak_effect": _pair(0.18, 0.08, 0.30, paired=True), "leak_effect_tie_random": _pair(0.2, 0.1, 0.3, paired=True),
            }
            for v in ["current", "team-final", "fix-snapshot"]
        },
        "version_comparison": {
            "team_final_minus_current_primary": _pair(-0.037, -0.18, 0.094),
            "team_final_minus_current_as_written": _pair(0.084, 0.019, 0.166),
            "leak_effect_did_team_final_minus_current": _pair(0.122, -0.003, 0.26),
        },
        "team_final_written": {
            "clicks_only": _summary(0.99),
            "all_rows_definition_error": _summary(1.0, std=0.0),
            "random_reference": {
                "clicks_only": {"aggregate": {"mrr": 0.689, "precision@5": 0.506}, "n_draws": 100, "mean_answers_per_user": 92.8, "n_users": 100},
                "all_rows_definition_error": {"aggregate": {"mrr": 1.0, "precision@5": 1.0}, "n_draws": 100, "mean_answers_per_user": 195.0, "n_users": 100},
            },
            "mean_answers_per_user": {"clicks_only": 92.8, "all_rows_definition_error": 195.0},
            "model_minus_random_clicks_only": {"mrr": {"effect": 0.3, "model_mean": 0.99, "random_mean": 0.689}},
        },
        "generator_split": {"current": {"summary": _summary(0.35)}, "team-final": {"summary": _summary(0.40)}},
        "baselines": {
            "team_split": {"meta": {"cold_fallback": "popularity"}, "baselines": {b: _baseline() for b in BASELINE_NAMES}},
            "generator_split": {"meta": {}, "baselines": {b: _baseline(0.3, with_groups=False) for b in BASELINE_NAMES}},
            "small_recent_15": {
                "meta": {"n_candidates": 15, "n_evaluated_users": 27, "mean_answer_density": 0.336},
                "baselines": {b: _baseline(0.5) for b in BASELINE_NAMES},
            },
        },
        "model_vs_baseline": {
            m: {b: copy.deepcopy(mvb_entry) for b in BASELINE_NAMES}
            for m in ["current_lambdarank_es", "current_lambdarank_fixed100", "current_binary", "team_final"]
        },
        "small_pool_model_vs_baseline": {b: {"plain": _pair(), "plain_tie_random": _pair()} for b in BASELINE_NAMES},
        "decomposition_summary": {
            **{k: decomp_block(0.55) for k in ["leaky", "binary", "leaky_binary", "fixed100", "leaky_fixed100", "impression", "small_recent_15"]},
            "random_negatives_n_train": [34874] * 5,
        },
        "decomposition_bootstrap": {
            k: _pair() for k in [
                "leakage_lambdarank_early_stopping", "leakage_lambdarank_early_stopping_tie_random", "leakage_binary",
                "leakage_lambdarank_fixed100", "objective_lambdarank_es_vs_binary", "objective_lambdarank_fixed100_vs_binary",
                "rounds_es_vs_fixed100", "rounds_es_vs_fixed100_tie_random", "negatives_random_vs_impression",
            ]
        },
        "reproduction_check_vs_v2": {
            "v2_raw_commit": "1585ce6",
            "arms": {"current": {"shared_seeds": [42, 43, 44], "max_abs_diff_mrr_p5": 0.0, "identical_to_4dp": True}},
        },
    }


def _render(tmp_path, monkeypatch, data) -> str:
    report_dir = tmp_path / "reports" / "recsys"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "team_repro_v2.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    import make_report_v2

    monkeypatch.setattr(make_report_v2, "WORKTREE_ROOT", tmp_path)
    monkeypatch.setattr(make_report_v2, "REPORT_DIR", report_dir)
    make_report_v2.main()
    return (report_dir / "team_repro_v2.md").read_text(encoding="utf-8")


@pytest.fixture()
def text(tmp_path, monkeypatch):
    return _render(tmp_path, monkeypatch, _base_data())


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_all_required_sections_present(text):
    assert "team_repro v2.1" in text
    for frag in ["요약", "데이터 구조", "프로토콜", "베이스라인 비교", "분해 실험", "증명할 수 없는", "다음 단계", "이력서"]:
        assert frag in text, f"필수 절 누락: {frag!r}"


def test_no_unformatted_values_in_summary(text):
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "N/A" not in summary, summary


def test_every_0897_mention_says_team_reported(text):
    """v2 재검토: 0.897은 팀 보고값(한 시점 스냅샷, 누출로 부풀려졌을 가능성)이라는 것을 매번 밝힌다."""
    lines = [ln for ln in text.splitlines() if "0.897" in ln]
    assert lines
    for ln in lines:
        assert "팀 보고값" in ln, ln


def test_team_final_written_uses_clicks_and_random_reference(text):
    sec = _section(text, "### 2-4.", "## 3.")
    assert "0.6890" in sec and "0.5060" in sec  # 같은 정의의 무작위 기준값
    assert "정의 오류" in sec
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "근접" not in summary


def test_resume_list_has_no_team_attribution_or_overclaim(text):
    safe = _section(text, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    for bad in ["잘못 귀속", "팀의 우위", "정량적으로 규명", "팀이 최종 보고했던 성능 우위"]:
        assert bad not in safe, bad
    assert "팀원이 담당한" in safe


def test_avoid_list_keeps_key_items(text):
    avoid = _section(text, "### 쓰면 안 되는 문장", "## 부록")
    assert "0.849" in avoid and "0.772" in avoid
    assert "후보 풀 크기" in avoid
    assert "FIX #4" in avoid


def test_no_padded_or_label_assumption_table_rows(text):
    assert "| label_assumption |" not in text
    assert "| full_195 → padded_400" not in text


def test_leak_verdict_word_follows_ci(tmp_path, monkeypatch):
    """결론 단어는 CI 부호에서만 나온다: 같은 템플릿에서 CI가 0을 포함하면 '검출되지 않는다'."""
    pos = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "누출이 지표를 부풀린다" in _section(pos, "## 0. 요약", "## 1.")
    data = _base_data()
    data["headline_ci"]["current"]["leak_effect"] = _pair(0.02, -0.05, 0.09, paired=True)
    inc = _render(tmp_path / "b", monkeypatch, data)
    assert "누출이 지표를 검출되지 않는다" not in inc  # 문장 조립이 어색하게 깨지지 않았는지
    assert "검출되지 않는다(CI가 0 포함)" in _section(inc, "## 0. 요약", "## 1.")


def test_baseline_claim_withdrawn_only_without_model_win(tmp_path, monkeypatch):
    base = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "결론은 철회한다" in base
    data = _base_data()
    data["model_vs_baseline"]["current_lambdarank_es"]["popularity"]["plain"] = _pair(0.1, 0.02, 0.2)
    win = _render(tmp_path / "b", monkeypatch, data)
    assert "결론은 철회한다" not in win
    assert "모델 우위) 비교: popularity" in win


def test_degenerate_seeds_are_labelled(text):
    assert "퇴화 3/5" in text
    assert "단일 트리 붕괴는 고치지 못했다" in text


def test_fix4_null_sentence_only_when_all_inconclusive(tmp_path, monkeypatch):
    base = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "검정력 부족" in _section(base, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    data = _base_data()
    data["decomposition_bootstrap"]["leakage_binary"] = _pair(-0.1, -0.2, -0.02)
    mixed = _render(tmp_path / "b", monkeypatch, data)
    assert "검정력 부족" not in _section(mixed, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")


def test_fix4_detected_setting_named_with_its_ci(tmp_path, monkeypatch):
    data = _base_data()
    data["decomposition_bootstrap"]["leakage_binary"] = _pair(-0.13, -0.22, -0.04)
    text = _render(tmp_path, monkeypatch, data)
    safe = _section(text, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    assert "binary -0.1300 [-0.2200, -0.0400]" in safe
    assert "lambdarank 조기 종료·lambdarank 100라운드 모델에서는 검출되지 않았다" in safe
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "가설" in summary
