"""run_repro.py의 v2.2 집계 함수 테스트: 조기 종료 동점 진단 요약과 같은 조건끼리의 버전 비교.

pipeline.py 서브프로세스는 띄우지 않는다 - 실행 결과 dict를 직접 만들어 넣는다. 이 테스트가
보호하는 것:
  - 조기 종료용 inner-valid 행 순서가 캐시 키에 들어간다(순서만 바꾼 실행이 옛 캐시를 쓰지 않는다).
  - '엔진 순서의 1라운드 NDCG가 이후 라운드보다 높은 시드'를 실행 기록에서 그대로 세고,
    진단이 없는 실행(AUC 조기 종료, 고정 라운드)은 arm별 표에서 뺀다.
  - 버전 비교의 각 쌍이 선언한 arm끼리만 계산되고, 같은 조건 비교 표시가 붙는다.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEAM_REPRO_DIR = REPO_ROOT / "evaluation" / "recsys" / "team_repro"
sys.path.insert(0, str(TEAM_REPRO_DIR))

import run_repro as RR  # noqa: E402

USERS = [1, 2, 3, 4, 5, 6]


def _per_user(value: float) -> dict:
    return {str(u): {"mrr": value + 0.01 * u, "precision@5": value / 2} for u in USERS}


def _diag(it1_engine, it1_tie, best_it, best_engine, best_tie, as_evaluated_first=0.17):
    def cell(data_order, tie_expected, reversed_order):
        return {
            "ndcg@5": {
                "data_order": data_order, "reversed_order": reversed_order, "tie_expected": tie_expected,
                "n_groups": 100, "tied_positive_group_share": 0.3,
            }
        }

    by_iter = {"1": cell(it1_engine, it1_tie, it1_tie - 0.05), str(best_it): cell(best_engine, best_tie, best_tie - 0.001)}
    return {
        "rank_group_key": "user_timestamp", "ndcg_eval_at": [5, 10],
        "frames": {
            "engine_row_order": {"positive_first_share": 1.0, "by_iteration": by_iter},
            "as_evaluated": {"positive_first_share": as_evaluated_first, "by_iteration": by_iter},
        },
    }


def _run(seed, primary, as_written, best_iteration=50, n_trees=None, objective="lambdarank", es_valid_order="shuffled", diag=None):
    return {
        "seed": seed, "best_iteration": best_iteration, "n_trees": n_trees if n_trees is not None else best_iteration,
        "objective_used": objective, "es_valid_order": es_valid_order,
        "rounds_policy": "inner_valid_early_stopping" if best_iteration is not None else "fixed_100_rounds_no_early_stopping",
        "best_score_inner_valid": {"valid_0": {"ndcg@5": 0.74}} if diag else {},
        "es_tie_diagnostic": diag,
        "primary": {"aggregate_metrics": {"mrr": primary, "precision@5": primary / 2}, "per_user_metrics": _per_user(primary)},
        "as_written": {"aggregate_metrics": {"mrr": as_written, "precision@5": as_written / 2}, "per_user_metrics": _per_user(as_written)},
    }


def _runs():
    seeds = [42, 43, 44]
    # 시드 43은 엔진 순서에서 1라운드 NDCG(0.775)가 이후 라운드(0.742)보다 높아 1라운드에서 멈춘다.
    shuffled_diag = {
        42: _diag(0.68, 0.61, 35, 0.747, 0.743),
        43: _diag(0.775, 0.727, 72, 0.742, 0.7416),
        44: _diag(0.70, 0.65, 40, 0.75, 0.748),
    }
    engine_diag = {
        42: _diag(0.68, 0.61, 35, 0.747, 0.743, as_evaluated_first=1.0),
        43: _diag(0.775, 0.727, 1, 0.775, 0.727, as_evaluated_first=1.0),
        44: _diag(0.70, 0.65, 40, 0.75, 0.748, as_evaluated_first=1.0),
    }
    engine_bi = {42: 35, 43: 1, 44: 40}
    shuffled_bi = {42: 35, 43: 72, 44: 40}
    return {
        "current": [_run(s, 0.62, 0.80, shuffled_bi[s], diag=shuffled_diag[s]) for s in seeds],
        "current_es_engine_order": [
            _run(s, 0.55 if engine_bi[s] == 1 else 0.62, 0.74, engine_bi[s], es_valid_order="engine", diag=engine_diag[s])
            for s in seeds
        ],
        "binary": [_run(s, 0.59, 0.81, 90, objective="binary") for s in seeds],
        "fixed100": [_run(s, 0.62, 0.84, None, n_trees=100) for s in seeds],
        "team-final": [_run(s, 0.56, 0.85, 150, objective="binary") for s in seeds],
        "team_final_fixed100": [_run(s, 0.57, 0.84, None, n_trees=100, objective="binary") for s in seeds],
    }


def test_low_iteration_threshold_matches_pipeline():
    spec = importlib.util.spec_from_file_location("team_repro_pipeline_for_const", TEAM_REPRO_DIR / "pipeline.py")
    pipe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipe)
    assert RR.LOW_BEST_ITERATION_MAX == pipe.LOW_BEST_ITERATION_MAX
    assert RR.ES_VALID_ORDER_DEFAULT in pipe.ES_VALID_ORDERS


def test_cache_tag_changes_with_es_valid_order():
    kw = dict(
        version="current", protocol="team_split", label_mode="clicks_only", leakage_mode="fixed",
        candidate_pool="full_195", negative_source="random", objective_override="none", answer_start="2026-01-01T00:00:00",
    )
    default = RR.cache_tag(42, "h", "c", "cfg", None, **kw)
    shuffled = RR.cache_tag(42, "h", "c", "cfg", None, es_valid_order="shuffled", **kw)
    engine = RR.cache_tag(42, "h", "c", "cfg", None, es_valid_order="engine", **kw)
    assert default == shuffled  # 기본값은 shuffled
    assert engine != shuffled
    assert "esengine" in engine and "esshuffled" in shuffled


def test_tie_diag_rows_reads_iteration_one_and_best_iteration():
    rows = RR.tie_diag_rows(_runs()["current"])
    r43 = next(r for r in rows if r["seed"] == 43)
    assert r43["best_iteration"] == 72
    assert r43["iter1_engine_order_ndcg5"] == pytest.approx(0.775)
    assert r43["iter1_tie_expected_ndcg5"] == pytest.approx(0.727)
    assert r43["best_engine_order_ndcg5"] == pytest.approx(0.742)
    assert r43["positive_first_share_engine_order"] == 1.0
    assert r43["positive_first_share_as_evaluated"] == pytest.approx(0.17)
    no_diag = RR.tie_diag_rows(_runs()["binary"])
    assert all(not r["has_diagnostic"] for r in no_diag)


def test_early_stopping_tie_artefact_counts_come_from_recorded_diagnostics():
    runs = _runs()
    art = RR.early_stopping_tie_artefact(runs, {})
    s = art["summary"]
    assert s["n_seeds"] == 3
    assert s["n_engine_order_stops_at_1"] == 1 and s["n_shuffled_stops_at_1"] == 0
    assert s["collapse_seeds_engine_order"] == [43]
    # 엔진 순서에서 1라운드 NDCG가 섞은 조기 종료가 고른 라운드의 값보다 높은 시드 = 1라운드에서 멈춘 시드
    assert s["iter1_beats_best_seeds"] == [43]
    assert s["n_tie_expected_improves_after_iter1"] == 3  # 동점 기대값으로는 모든 시드가 나아진다
    assert s["iter1_inflation_mean"] == pytest.approx((0.07 + 0.048 + 0.05) / 3)
    p43 = next(x for x in art["paired_current"] if x["seed"] == 43)
    assert p43["engine_order_best_iteration"] == 1 and p43["shuffled_best_iteration"] == 72
    assert p43["primary_mrr_engine_order"] == pytest.approx(0.55) and p43["primary_mrr_shuffled"] == pytest.approx(0.62)
    # NDCG 진단이 없는 arm(AUC 조기 종료, 고정 라운드)은 arm별 표에 넣지 않는다.
    assert set(art["by_arm"]) == {"current", "current_es_engine_order"}
    assert art["by_arm"]["current_es_engine_order"]["n_stops_at_1"] == 1
    assert art["by_arm"]["current"]["n_low_iteration"] == 0
    assert art["by_arm"]["current"]["n_inner_valid_groups"] == [100, 100, 100]
    assert art["by_arm"]["current"]["rank_group_key"] == "user_timestamp"


def test_version_comparison_pairs_use_declared_arms_and_flag_like_with_like(monkeypatch):
    calls = []
    real = RR.M.nested_bootstrap_paired_diff

    def fast(a, b, mk, n_boot=1000, seed=0, **kw):
        calls.append(mk)
        return real(a, b, mk, n_boot=50, seed=seed, **kw)

    monkeypatch.setattr(RR.M, "nested_bootstrap_paired_diff", fast)
    vc = RR.version_comparison_pairs(_runs())
    assert vc["order"] == ["as_configured", "same_objective_binary_es", "fixed100_rounds", "v2_1_engine_order_es"]
    like = {k: p["like_with_like"] for k, p in vc["pairs"].items()}
    assert like == {
        "as_configured": False, "same_objective_binary_es": True, "fixed100_rounds": True, "v2_1_engine_order_es": False,
    }
    same = vc["pairs"]["same_objective_binary_es"]
    assert (same["team_final_arm"], same["current_arm"]) == ("team-final", "binary")
    assert same["team_final_objective"] == same["current_objective"] == "binary"
    fixed = vc["pairs"]["fixed100_rounds"]
    assert fixed["team_final_n_trees"] == fixed["current_n_trees"] == [100, 100, 100]
    v21 = vc["pairs"]["v2_1_engine_order_es"]
    assert v21["current_n_trees"] == [35, 1, 40]
    # effect = team-final 쪽 - current 쪽. 1차: 0.56 - 0.59, 2차: 0.85 - 0.81
    assert same["primary"]["mrr"]["effect"] == pytest.approx(0.56 - 0.59)
    assert same["as_written"]["mrr"]["effect"] == pytest.approx(0.85 - 0.81)
    # DiD = (team-final 2차-1차) - (current 2차-1차)
    assert same["leak_effect_did"]["mrr"]["effect"] == pytest.approx((0.85 - 0.56) - (0.81 - 0.59))
    assert same["leak_effect_team_final"]["mrr"]["paired_seeds"] is True  # 같은 모델의 두 추론
    assert same["primary"]["mrr"]["paired_seeds"] is False  # 서로 다른 모델
