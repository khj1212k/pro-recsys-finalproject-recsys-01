"""make_report_v2.py 테스트: team_repro_v2.json(v2.2 형식)의 최소 픽스처로부터 마크다운을
생성해 깨지지 않는지, 필수 절이 나오는지, 그리고 재검토에서 지적된 문장 오류가 다시
생기지 않는지를 검증한다: 0.897 표기, 팀 귀속 문장, 정답 정의 행 라벨, CI 부호로 고른 결론
단어, 조기 종료가 일찍 멈춘 원인의 서술(v2.2), 같은 조건 비교에서만 고르는 버전 비교 결론(v2.2).
숫자 자체는 재계산하지 않는다."""
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
        "n_degenerate_seeds": degenerate, "n_low_iteration_seeds": degenerate,
        "es_valid_order": "shuffled", "n_distinct_scores_primary": [2983] * n_seeds,
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
            "unshown_filtered": {
                "aggregate": {mk: mean for mk in METRICS},
                "list_len_stats": {"mean_len": 11.2, "share_len_lt_5": 0.1},
            },
            "seen_share_top5": 0.3,
        })
    return d


BASELINE_NAMES = ["random", "fixed_csv_order", "popularity", "recency", "category_match", "cosine_history", "onboarding_newsletter_cosine"]


def _vc_pair(tf_arm, cur_arm, like, primary=None, as_written=None, did=None, cur_trees=None):
    return {
        "team_final_arm": tf_arm, "current_arm": cur_arm, "condition": f"{tf_arm} vs {cur_arm}",
        "remaining_differences": "코드", "like_with_like": like,
        "team_final_n_trees": [115, 181, 109, 158, 255], "current_n_trees": cur_trees or [35, 72, 49, 90, 28],
        "primary": primary or _pair(-0.03, -0.14, 0.08), "as_written": as_written or _pair(0.03, -0.02, 0.09),
        "leak_effect_team_final": _pair(0.285, 0.158, 0.418, paired=True),
        "leak_effect_current": _pair(0.2, 0.1, 0.3, paired=True),
        "leak_effect_did": did or _pair(0.06, -0.03, 0.16),
    }


def _version_comparison(like_did=None, v21_did=None, like_aw=None):
    """기본값: 같은 조건 비교의 DiD는 검출 안 됨, v2.1 재현 비교의 DiD만 양수."""
    return {
        "order": ["as_configured", "same_objective_binary_es", "fixed100_rounds", "v2_1_engine_order_es"],
        "pairs": {
            "as_configured": _vc_pair("team-final", "current", False),
            "same_objective_binary_es": _vc_pair("team-final", "binary", True, did=like_did, as_written=like_aw),
            "fixed100_rounds": _vc_pair("team_final_fixed100", "fixed100", True, did=like_did, as_written=like_aw),
            "v2_1_engine_order_es": _vc_pair(
                "team-final", "current_es_engine_order", False, as_written=_pair(0.096, 0.032, 0.170),
                did=v21_did or _pair(0.109, 0.012, 0.211), cur_trees=[35, 1, 1, 90, 1],
            ),
        },
    }


def _leak_entry(aw, p, trees, effect=None):
    return {
        "primary_mrr_mean": p, "as_written_mrr_mean": aw, "n_trees": trees, "best_iteration": trees,
        "objective": "lambdarank", "rounds_policy": "inner_valid_early_stopping", "es_valid_order": "shuffled",
        "leak_effect": effect or _pair(aw - p, 0.08, 0.30, paired=True),
    }


def _artefact(engine_bi=(35, 1, 1, 90, 1), shuffled_bi=(35, 72, 49, 90, 28)):
    paired = []
    for seed, e_bi, s_bi in zip(range(42, 47), engine_bi, shuffled_bi):
        collapsed = e_bi <= 1
        it1_engine = 0.775 if collapsed else 0.68
        paired.append({
            "seed": seed, "engine_order_best_iteration": e_bi, "shuffled_best_iteration": s_bi,
            "engine_order_recorded_ndcg5": it1_engine if collapsed else 0.745, "shuffled_recorded_ndcg5": 0.742,
            "iter1_engine_order_ndcg5": it1_engine, "iter1_tie_expected_ndcg5": it1_engine - 0.05,
            "iter1_reversed_order_ndcg5": it1_engine - 0.1, "iter1_tied_positive_group_share": 0.29,
            "shuffled_best_engine_order_ndcg5": 0.745, "shuffled_best_tie_expected_ndcg5": 0.742,
            "iter1_inflation": 0.05, "engine_order_iter1_beats_tie_fair_best": collapsed,
            "tie_expected_improves_after_iter1": True,
            "positive_first_share_engine_order": 1.0, "positive_first_share_shuffled": 0.167,
            "primary_mrr_engine_order": 0.55 if collapsed else 0.65, "primary_mrr_shuffled": 0.62,
        })
    n_e1 = sum(1 for b in engine_bi if b <= 1)
    n_s1 = sum(1 for b in shuffled_bi if b <= 1)
    return {
        "note": "note", "paired_current": paired,
        "summary": {
            "n_seeds": 5, "n_engine_order_stops_at_1": n_e1, "n_shuffled_stops_at_1": n_s1,
            "n_engine_order_low_iteration": n_e1, "n_shuffled_low_iteration": sum(1 for b in shuffled_bi if b <= 5),
            "iter1_inflation_mean": 0.05, "iter1_inflation_min": 0.048, "iter1_inflation_max": 0.071,
            "n_engine_order_iter1_beats_tie_fair_best": n_e1, "n_tie_expected_improves_after_iter1": 5,
            "collapse_seeds_engine_order": [x["seed"] for x in paired if x["engine_order_best_iteration"] <= 1],
            "iter1_beats_best_seeds": [x["seed"] for x in paired if x["engine_order_iter1_beats_tie_fair_best"]],
        },
        "by_arm": {
            "current": {
                "es_valid_order": "shuffled", "rank_group_key": "user_timestamp", "n_inner_valid_groups": [1432] * 5,
                "best_iteration": list(shuffled_bi), "n_stops_at_1": n_s1,
                "n_low_iteration": sum(1 for b in shuffled_bi if b <= 5),
                "positive_first_share_engine_order": [1.0] * 5, "positive_first_share_as_evaluated": [0.167] * 5,
                "iter1_engine_order_ndcg5": [0.775] * 5, "iter1_tie_expected_ndcg5": [0.725] * 5,
                "best_tie_expected_ndcg5": [0.742] * 5, "recorded_ndcg5_at_best": [0.742] * 5,
            },
            "current_es_engine_order": {
                "es_valid_order": "engine", "best_iteration": list(engine_bi), "n_stops_at_1": n_e1, "n_low_iteration": n_e1,
                "positive_first_share_engine_order": [1.0] * 5, "positive_first_share_as_evaluated": [1.0] * 5,
                "iter1_engine_order_ndcg5": [0.775] * 5, "iter1_tie_expected_ndcg5": [0.725] * 5,
                "best_tie_expected_ndcg5": [0.725] * 5, "recorded_ndcg5_at_best": [0.775] * 5,
            },
        },
    }


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
        "primary_unshown_list_len": {"mean_len": 9.4, "min_len": 2, "share_len_lt_5": 0.2, "n_users": 31},
    }
    decomp_block = lambda m: {  # noqa: E731
        "primary": _summary(m), "primary_tie_random": _summary(m - 0.02, tie=True),
        "primary_seen_filtered": _summary(m), "primary_unshown_filtered": _summary(m),
        "primary_cold": {"mrr": m, "precision@5": m, "num_users": 15}, "primary_warm": {"mrr": m, "precision@5": m, "num_users": 16},
    }
    mvb_entry = {"plain": _pair(-0.1, -0.2, -0.01), "unshown_filtered": _pair(), "plain_tie_random": _pair()}
    return {
        "meta": {
            "generated_at": "2026-10-06T00:00:00", "report_version": "v2.2",
            "es_valid_order_default": "shuffled", "low_best_iteration_max": 5, "fixed_rounds_sensitivity": 100,
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
            "current": headline_block(0.62, 0.80, bi=[35, 72, 49, 90, 28]),
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
        "version_comparison": _version_comparison(),
        "model_leak_effects": {
            "team-final": _leak_entry(0.85, 0.56, [115, 181, 109, 158, 255]),
            "team_final_fixed100": _leak_entry(0.84, 0.57, [100] * 5),
            "fix-snapshot": _leak_entry(0.79, 0.59, [40, 49, 29]),
            "current": _leak_entry(0.80, 0.62, [35, 72, 49, 90, 28]),
            "binary": _leak_entry(0.81, 0.59, [121, 85, 92, 42, 89]),
            "fixed100": _leak_entry(0.84, 0.62, [100] * 5),
            "current_es_engine_order": _leak_entry(0.75, 0.58, [35, 1, 1, 90, 1]),
        },
        "early_stopping_tie_artefact": _artefact(),
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
            **{k: decomp_block(0.55) for k in [
                "leaky", "binary", "leaky_binary", "fixed100", "leaky_fixed100", "impression", "small_recent_15",
                "team_final_fixed100",
            ]},
            "current_es_engine_order": {
                **decomp_block(0.58),
                "primary": _summary(0.58, best_iteration=[35, 1, 1, 90, 1], degenerate=3),
            },
            "random_negatives_n_train": [34874] * 5,
        },
        "decomposition_bootstrap": {
            k: _pair() for k in [
                "leakage_lambdarank_early_stopping", "leakage_lambdarank_early_stopping_tie_random", "leakage_binary",
                "leakage_lambdarank_fixed100", "objective_lambdarank_es_vs_binary", "objective_lambdarank_fixed100_vs_binary",
                "rounds_es_vs_fixed100", "rounds_es_vs_fixed100_tie_random", "negatives_random_vs_impression",
                "es_valid_order_engine_vs_shuffled", "es_valid_order_engine_vs_shuffled_tie_random",
            ]
        },
        "reproduction_check_vs_v21": {
            "v21_raw_commit": "67a5db2",
            "arms": {
                "team-final (AUC 조기 종료)": {
                    "shared_seeds": [42, 43, 44, 45, 46], "max_abs_diff_mrr_p5": 0.0, "identical_to_4dp": True,
                    "expected_identical": True, "violates_expectation": False,
                    "best_iteration_v21": [115, 181, 109, 158, 255], "best_iteration_v22": [115, 181, 109, 158, 255],
                },
                "current (NDCG 조기 종료, 섞음)": {
                    "shared_seeds": [42, 43, 44, 45, 46], "max_abs_diff_mrr_p5": 0.08, "identical_to_4dp": False,
                    "expected_identical": False, "violates_expectation": False,
                    "best_iteration_v21": [35, 1, 1, 90, 1], "best_iteration_v22": [35, 72, 49, 90, 28],
                },
            },
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
    assert "team_repro v2.2" in text
    for frag in ["요약", "데이터 구조", "프로토콜", "베이스라인 비교", "분해·민감도 실험", "증명할 수 없는", "다음 단계", "이력서"]:
        assert frag in text, f"필수 절 누락: {frag!r}"


def test_no_unformatted_values_in_summary(text):
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "N/A" not in summary, summary
    assert "?" not in summary.replace("?)", ""), summary


def test_every_0897_mention_says_team_reported(text):
    """0.897은 팀 보고값이라는 것을 매번 밝힌다."""
    lines = [ln for ln in text.splitlines() if "0.897" in ln]
    assert lines
    for ln in lines:
        assert "팀 보고값" in ln, ln


def test_team_value_is_not_described_as_measured_once(text):
    """v2.1 재검토 minor: '한 시점에 한 번 측정'은 출처 없는 서술이라 쓰지 않는다(금지 목록의 인용만 예외)."""
    avoid = _section(text, "### 쓰면 안 되는 문장", "## 부록")
    rest = text.replace(avoid, "")
    header = rest.split("## 0. 요약", 1)[0]
    body = rest.replace(header, "")
    assert "한 시점" not in body
    assert "한 시점에 한 번만 측정했다" in avoid
    assert "측정 절차·시점" in body


def test_team_final_written_uses_clicks_and_random_reference(text):
    sec = _section(text, "### 2-4.", "## 3.")
    assert "0.6890" in sec and "0.5060" in sec  # 같은 정의의 무작위 기준값
    assert "가장 그럴듯한 번역" in sec and "문서로 남아 있지 않아" in sec  # 확정 사실로 쓰지 않는다
    assert "올바른 번역" not in text
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "근접" not in summary


def test_resume_list_has_no_team_attribution_or_overclaim(text):
    safe = _section(text, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    for bad in ["잘못 귀속", "팀의 우위", "정량적으로 규명", "팀이 최종 보고했던 성능 우위", "버그를 고쳤다"]:
        assert bad not in safe, bad
    assert "팀원이 담당한" in safe


def test_avoid_list_keeps_key_items(text):
    avoid = _section(text, "### 쓰면 안 되는 문장", "## 부록")
    assert "0.849" in avoid and "0.772" in avoid
    assert "후보 풀 크기" in avoid
    assert "FIX #4" in avoid
    assert "엔진의 조기 종료 버그를 고쳤다" in avoid
    assert "learning_rate" in avoid  # 틀린 원인 설명을 금지 목록에 둔다


def test_no_padded_or_label_assumption_table_rows(text):
    assert "| label_assumption |" not in text
    assert "| full_195 → padded_400" not in text


def test_leak_verdict_word_follows_ci(tmp_path, monkeypatch):
    """결론 단어는 CI 부호에서만 나온다: 설정 하나라도 CI가 0을 포함하면 '모두 부풀린다'고 쓰지 않는다."""
    pos = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "모두에서 누출이 지표를 부풀린다" in _section(pos, "## 0. 요약", "## 1.")
    data = _base_data()
    data["model_leak_effects"]["binary"]["leak_effect"] = _pair(0.02, -0.05, 0.09, paired=True)
    inc = _render(tmp_path / "b", monkeypatch, data)
    summary = _section(inc, "## 0. 요약", "## 1.")
    assert "모두에서 누출이 지표를 부풀린다" not in summary
    assert "설정별 판정" in summary and "검출 안 됨" in summary
    safe = _section(inc, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    assert "설정별 판정이 갈려" in safe  # 범위 문장을 내지 않는다


def test_resume_leak_sentence_quotes_current_as_range_over_settings(text):
    safe = _section(text, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    assert "0.85→0.56" in safe  # team-final이 1차 인용
    assert "0.80~0.84→0.59~0.62" in safe  # current는 모델 설정 3가지의 범위
    assert "lambdarank 조기 종료·binary 조기 종료·lambdarank 100라운드" in safe


def test_baseline_claim_withdrawn_only_without_model_win(tmp_path, monkeypatch):
    base = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "결론은 철회한다" in base
    data = _base_data()
    data["model_vs_baseline"]["current_lambdarank_es"]["popularity"]["plain"] = _pair(0.1, 0.02, 0.2)
    win = _render(tmp_path / "b", monkeypatch, data)
    assert "결론은 철회한다" not in win
    assert "모델 우위) 비교: popularity" in win


# --- v2.2: 조기 종료 아티팩트 ---------------------------------------------------------


def test_early_stop_cause_is_stated_from_measured_tie_diagnostic(text):
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "동점을 데이터 행 순서로 깬다" in summary
    assert "5개 시드 중 3개가 1라운드에서 멈춘다" in summary
    assert "이 시드들이 정확히 1라운드에서 멈춘 시드다" in summary
    assert "1라운드 종료 0/5" in summary  # 섞은 뒤
    sec = _section(text, "### 4-1.", "### 4-2.")
    assert "| 43 | 1 | 72 |" in sec


def test_wrong_collapse_causes_appear_only_as_retraction(text):
    """v2.1이 원인으로 적은 learning_rate/작은 inner-valid는 철회·금지 문맥에서만 나온다."""
    for ln in text.splitlines():
        if "learning_rate" in ln:
            assert ("철회" in ln) or ("틀린 추정" in ln) or ("원인은 조기 종료 지표" in ln), ln
    assert "단일 트리 붕괴는 고치지 못했다" not in text
    assert "퇴화 3/5" not in text  # '퇴화' 라벨 대신 1라운드 종료 시드 수를 적는다
    assert "1라운드 종료 3/5" in text  # 엔진 순서 진단 arm


def test_engine_is_reported_as_not_fixed(text):
    nxt = _section(text, "## 6.", "## 7.")
    assert "미수정" in nxt and "엔진 소스를 고치지 않았다" in nxt


def test_seeds_still_stopping_early_after_shuffle_are_flagged(tmp_path, monkeypatch):
    data = _base_data()
    data["early_stopping_tie_artefact"] = _artefact(shuffled_bi=(35, 72, 1, 90, 28))
    text = _render(tmp_path, monkeypatch, data)
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "섞은 뒤에도 1개 시드가 1라운드에서 멈췄다" in summary
    sec = _section(text, "### 4-1.", "### 4-2.")
    assert "동점 순서로 설명되지 않는다" in sec
    assert "원인은 확인하지 않았다" in sec and "가설이다" in sec  # 추정을 원인으로 쓰지 않는다
    assert "user_timestamp / [1432, 1432, 1432, 1432, 1432]" in sec


def test_resume_artefact_sentence_only_when_shuffle_reduces_first_round_stops(tmp_path, monkeypatch):
    base = _render(tmp_path / "a", monkeypatch, _base_data())
    key = "조기 종료 지표의 아티팩트임을 밝혔다"
    assert key in _section(base, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    data = _base_data()
    data["early_stopping_tie_artefact"] = _artefact(shuffled_bi=(35, 1, 1, 90, 1))  # 섞어도 그대로
    same = _render(tmp_path / "b", monkeypatch, data)
    assert key not in _section(same, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")


# --- v2.2: 버전 비교는 같은 조건 비교의 CI에서만 결론을 고른다 ---------------------------


def test_version_did_retracted_when_only_v21_comparison_is_positive(text):
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "같은 조건 비교 어디에서도 검출되지 않는다" in summary
    assert "문장은 철회한다" in summary and "[35, 1, 1, 90, 1]" in summary
    avoid = _section(text, "### 쓰면 안 되는 문장", "## 부록")
    assert "team-final 코드가 current보다 누출에 더 민감하다" in avoid
    sec = _section(text, "### 2-3.", "### 2-4.")
    assert "v2.1 비교 재현(엔진 순서 조기 종료)" in sec and "| 아니오 |" in sec and "| 예 |" in sec


def test_version_did_positive_only_when_all_like_with_like_pairs_agree(tmp_path, monkeypatch):
    data = _base_data()
    data["version_comparison"] = _version_comparison(like_did=_pair(0.1, 0.02, 0.2))
    text = _render(tmp_path / "a", monkeypatch, data)
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "같은 조건 비교 모두에서 양수다" in summary
    assert "인과 문장으로는 쓰지 않는다" in summary
    assert "문장은 철회한다" not in summary

    mixed = _base_data()
    mixed["version_comparison"] = _version_comparison()
    mixed["version_comparison"]["pairs"]["fixed100_rounds"]["leak_effect_did"] = _pair(0.1, 0.02, 0.2)
    text2 = _render(tmp_path / "b", monkeypatch, mixed)
    assert "같은 조건 비교 안에서 갈린다" in _section(text2, "## 0. 요약", "## 1.")


def test_as_written_gap_wording_follows_like_with_like_ci(tmp_path, monkeypatch):
    base = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "2차(팀 방식 추론 시점) 격차도 같은 조건 비교에서는 검출되지 않는다" in base
    assert "v2.1이 보고한 양수 2차 격차는" in base
    data = _base_data()
    data["version_comparison"] = _version_comparison(like_aw=_pair(0.06, 0.01, 0.12))
    pos = _render(tmp_path / "b", monkeypatch, data)
    assert "격차는 같은 조건 비교 모두에서 양수다" in pos


# --- FIX #4 / 사소한 정정 -------------------------------------------------------------


def test_fix4_null_sentence_only_when_all_inconclusive(tmp_path, monkeypatch):
    base = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "검정력 부족" in _section(base, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    data = _base_data()
    data["decomposition_bootstrap"]["leakage_binary"] = _pair(-0.1, -0.2, -0.02)
    mixed = _render(tmp_path / "b", monkeypatch, data)
    assert "검정력 부족" not in _section(mixed, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")


def test_fix4_detected_setting_named_with_its_ci_and_selection(tmp_path, monkeypatch):
    data = _base_data()
    data["decomposition_bootstrap"]["leakage_binary"] = _pair(-0.13, -0.22, -0.04)
    data["decomposition_bootstrap"]["leakage_lambdarank_early_stopping"] = _pair(-0.02, -0.1, 0.07)
    data["decomposition_bootstrap"]["leakage_lambdarank_fixed100"] = _pair(-0.08, -0.16, 0.01)
    text = _render(tmp_path, monkeypatch, data)
    safe = _section(text, "### 쓸 수 있는 문장", "### 쓰면 안 되는 문장")
    assert "binary -0.1300 [-0.2200, -0.0400]" in safe
    assert "lambdarank 조기 종료·lambdarank 100라운드 모델에서는 검출되지 않았다" in safe
    assert "3개 설정 중 1개에서만 검출, 다중비교 보정 없음, 점추정 부호는 3개 설정 모두 음수" in safe
    summary = _section(text, "## 0. 요약", "## 1.")
    assert "가설" in summary and "3개 설정 중 1개에서만 검출" in summary


def test_fixed_rounds_and_unshown_filter_are_described_precisely(text):
    assert "inner-train(학습 구간의 앞 80%)만으로" in _section(text, "### 4-0.", "### 4-1.")
    sec = _section(text, "### 3-1.", "### 3-2.")
    assert "top-20 리스트에서 사후 제거" in sec and "5로 나눈다" in sec


def test_reproduction_check_flags_arm_that_should_match_but_does_not(tmp_path, monkeypatch):
    ok = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "같아야 하는 arm은 모두 소수 4자리까지 일치한다" in ok
    data = _base_data()
    arm = data["reproduction_check_vs_v21"]["arms"]["team-final (AUC 조기 종료)"]
    arm.update({"identical_to_4dp": False, "max_abs_diff_mrr_p5": 0.01, "violates_expectation": True})
    bad = _render(tmp_path / "b", monkeypatch, data)
    assert "기대와 다른 arm: team-final (AUC 조기 종료)" in bad


def test_impression_arm_low_iteration_seeds_are_stated_next_to_its_effect(tmp_path, monkeypatch):
    base = _render(tmp_path / "a", monkeypatch, _base_data())
    assert "impression arm은 섞은 inner-valid에서도" not in base
    data = _base_data()
    data["decomposition_summary"]["impression"]["primary"] = _summary(0.55, best_iteration=[6, 2, 73, 3, 4], degenerate=0)
    data["decomposition_summary"]["impression"]["primary"]["n_low_iteration_seeds"] = 3
    text = _render(tmp_path / "b", monkeypatch, data)
    sec = _section(text, "### 4-4.", "### 4-5.")
    assert "5라운드 이하에서 멈춘 시드가 3/5개다" in sec and "[6, 2, 73, 3, 4]" in sec
