"""train/serve parity 게이트(ADR 0033, 설계 문서 §3.3 · E11)를 메모리 저장소에서 돌린다.

서비스(실제 파이프라인·스코어러 묶음·서빙 어댑터·등록된 LightGBM shadow)로 요청 200건을 재생해 로그를 쌓고,
같은 로그를 하네스 방식으로 다시 계산해 네 항목을 본다. 그리고 게이트가 실제로 어긋남을 잡는지 본다 -
항목마다 그 항목만 깨뜨린 입력을 넣어 그 항목이 실패하는지.

같은 게이트를 PostgreSQL과 실제 라우터로 도는 것은 tests/integration/test_feature_parity.py(CI)이고,
reports/recsys/parity_v1.json의 수치는 그쪽에서 나온다.
"""
import copy
import json
from dataclasses import replace
from datetime import timedelta

import lightgbm as lgb
import numpy as np
import pytest

from app.recsys.config import RecsysConfig
from evaluation.recsys.serving_parity import (
    THRESHOLDS,
    feature_parity,
    kendall_tau_without_ties,
    run_gate,
    top_k,
    write_report,
)
from evaluation.recsys.service_logs import LogBench
from recsys_core import SERVING_CANDIDATE_SPEC, serving
from tests.recsys.parity_sim import simulate, train_model_text

COL = {name: i for i, name in enumerate(serving.FEATURE_NAMES)}


@pytest.fixture(scope="module")
def model_text():
    return train_model_text()


@pytest.fixture(scope="module")
def sim(model_text):
    """뉴스레터 600개(출처별 k와 cap이 걸리는 규모), 사용자 8명, 요청 200건."""
    return simulate(n_items=600, n_users=8, n_requests=200, model_text=model_text, seed=1)


@pytest.fixture(scope="module")
def predict(model_text):
    return lgb.Booster(model_str=model_text).predict


@pytest.fixture(scope="module")
def report(sim, predict):
    return run_gate(sim.logs, sim.requests, predict, sim.serving_spec, meta={"source": "unit test (in-memory)"})


def test_the_replay_exercises_what_the_gate_is_about(sim):
    realtime = [r for r in sim.log.requests if r["source"] == "realtime"]
    slots = np.concatenate([r.slot_features for r in sim.requests])

    assert sim.n_requests == 200 and len(sim.requests) >= 180       # 어댑터 피처가 남은 요청
    assert len(realtime) >= 150
    assert all(r.slot_shadow is not None for r in sim.requests)     # 모든 요청에 shadow 점수
    assert sim.counters["features.computed"] >= 180 and sim.counters.get("features.error", 0) == 0
    # 값이 실제로 다양하다: 0만 비교하는 게이트가 아니다
    for name in ("hist_cos", "short_cos", "sess_cos", "pop_inviews_24h", "cat_share", "hist_len"):
        assert len(np.unique(slots[:, COL[name]])) > 5, name
    for name in ("pop_clicks_6h", "pop_clicks_48h", "pop_ctr_24h"):  # 클릭은 드물다: 0이 아닌 값이 있는지만
        assert (slots[:, COL[name]] > 0).any(), name
    assert (slots[:, COL["sess_len"]] > 0).any() and (slots[:, COL["sess_len"]] == 0).any()
    assert np.isnan(slots[:, COL["hours_since_last_event"]]).any()  # 클릭 이력이 없던 사용자의 요청
    # 후보 수가 출처별 k를 넘는 요청이 있다: 후보 생성기가 풀 전체를 내지 않는다
    assert max(len(r.candidate_ids) for r in sim.requests) > 150


def test_gate_item_1_serving_features_equal_the_harness_path_on_every_column(report):
    f = report["features"]

    assert f["pass"] is True
    assert f["slots"] >= 180 * 20 and f["columns"] == 22
    assert f["max_abs_diff"] < THRESHOLDS["features_max_abs_diff"] == 1e-6
    assert f["nan_position_mismatches"] == 0
    assert set(f["per_column_max_abs_diff"]) == set(serving.FEATURE_NAMES)
    # 세는 열은 오차 없이 같다
    for name in ("hist_len", "short_len", "sess_len", "pop_clicks_6h", "pop_clicks_24h", "pop_clicks_48h",
                 "pop_inviews_24h", "news_category", "user_ncat"):
        assert f["per_column_max_abs_diff"][name] == 0.0, name


def test_gate_item_2_logged_shadow_scores_rank_the_slots_like_the_recomputed_features(report):
    s = report["scores"]

    assert s["pass"] is True
    assert s["requests_with_shadow_scores"] >= 180 and s["requests_without_shadow_scores"] == 0
    assert s["min_kendall_tau"] == 1.0 and s["requests_with_tau_below_1"] == 0
    assert s["max_abs_score_diff"] < 1e-6


def test_gate_item_3_end_to_end_top20_overlap_with_the_harness_candidate_generator(report):
    e = report["end_to_end"]

    assert e["pass"] is True and e["k"] == 20
    assert e["mean_overlap"] >= THRESHOLDS["end_to_end_mean_overlap"] == 0.9
    assert e["requests"] >= 180
    # 두 후보 집합은 같지 않다(후보 생성기가 갈리는 층을 실제로 지났다)
    assert e["mean_candidate_set_jaccard"] < 0.95


def test_gate_item_4_serving_candidate_config_equals_the_harness_serving_config(report):
    c = report["candidate_config"]

    assert c["pass"] is True and c["equal"] is True
    assert c["serving"] == c["harness"] == SERVING_CANDIDATE_SPEC.as_dict()


def test_the_report_carries_what_is_needed_to_read_it(report, tmp_path):
    assert report["pass"] is True
    meta = report["meta"]
    assert meta["feature_schema_version"] == 2 and meta["feature_schema_hash"] == serving.SCHEMA_HASH
    assert meta["thresholds"] == THRESHOLDS and meta["requests"] >= 180 and meta["users"] >= 6

    path = tmp_path / "nested" / "parity.json"
    write_report(report, path)

    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(report))


# ----------------------------------------------------------------------------- 게이트가 어긋남을 잡는가
def _gate(sim, predict, requests=None, spec=None):
    return run_gate(sim.logs, sim.requests if requests is None else requests, predict, spec or sim.serving_spec)


def test_a_feature_that_is_off_by_one_hundred_thousandth_fails_item_1(sim, predict):
    tampered = copy.deepcopy(sim.requests)
    tampered[17].slot_features[3, COL["hist_cos"]] += 1e-5

    report = _gate(sim, predict, tampered)

    assert report["features"]["pass"] is False and report["pass"] is False
    assert report["features"]["per_column_max_abs_diff"]["hist_cos"] == pytest.approx(1e-5, rel=0.05)
    assert report["candidate_config"]["pass"] is True


def test_a_value_logged_where_the_harness_has_none_fails_item_1(sim, predict):
    tampered = copy.deepcopy(sim.requests)
    row = next(r for r in tampered if np.isnan(r.slot_features[:, COL["hours_since_last_event"]]).all())
    row.slot_features[:, COL["hours_since_last_event"]] = 0.0  # "이벤트 없음"을 0시간으로 적는 버그

    report = _gate(sim, predict, tampered)

    assert report["features"]["pass"] is False
    assert report["features"]["nan_position_mismatches"] == len(row.slot_ids)


def test_recomputing_at_the_wrong_time_fails_item_1(sim):
    """요청 로그에 피처의 기준 시각이 남지 않으면 다시 계산할 수 없다: 2초만 달라도 경과 시간 열이 어긋난다."""
    shifted = [replace(r, as_of=r.as_of + timedelta(seconds=2)) for r in sim.requests[:40]]

    result = feature_parity(LogBench(sim.logs), shifted)

    assert result["pass"] is False
    assert result["per_column_max_abs_diff"]["hours_since_pub"] > 1e-4


def test_shadow_scores_in_a_different_order_fail_item_2(sim, predict):
    tampered = copy.deepcopy(sim.requests)
    tampered[5].slot_shadow = tampered[5].slot_shadow[::-1].copy()

    report = _gate(sim, predict, tampered)

    assert report["scores"]["pass"] is False and report["scores"]["requests_with_tau_below_1"] == 1
    assert report["features"]["pass"] is True


def test_a_serving_candidate_generator_that_drops_most_candidates_fails_item_3(sim, predict):
    """서빙이 후보의 일부만 낸 경우(예: 한 출처만 도는 버그): 같은 피처·같은 모델이어도 목록이 갈린다."""
    rng = np.random.default_rng(0)
    tampered = [replace(r, candidate_ids=sorted(rng.choice(r.candidate_ids, size=min(25, len(r.candidate_ids)),
                                                           replace=False).tolist()))
                for r in sim.requests]

    report = _gate(sim, predict, tampered)

    assert report["end_to_end"]["pass"] is False and report["end_to_end"]["mean_overlap"] < 0.9
    assert report["features"]["pass"] is True and report["scores"]["pass"] is True


def test_a_changed_serving_candidate_setting_fails_item_4(sim, predict):
    report = _gate(sim, predict, sim.requests[:20], spec=RecsysConfig(knn_k=50).candidate_spec())

    assert report["candidate_config"]["pass"] is False and report["pass"] is False
    assert report["candidate_config"]["serving"]["sources"][0] == ["knn_profile", 50]


def test_requests_without_shadow_scores_cannot_pass_item_2(sim, predict):
    stripped = [replace(r, slot_shadow=None) for r in sim.requests[:20]]

    report = _gate(sim, predict, stripped)

    assert report["scores"]["pass"] is False and report["scores"]["requests_with_shadow_scores"] == 0


def test_an_empty_log_passes_nothing(sim, predict):
    report = _gate(sim, predict, [])

    assert report["pass"] is False
    assert not report["features"]["pass"] and not report["scores"]["pass"] and not report["end_to_end"]["pass"]


# ----------------------------------------------------------------------------- 작은 부품
def test_kendall_tau_ignores_pairs_tied_on_either_side():
    assert kendall_tau_without_ties([3, 2, 1], [30, 20, 10]) == 1.0
    assert kendall_tau_without_ties([3, 2, 1], [10, 20, 30]) == -1.0
    assert kendall_tau_without_ties([1, 1, 2, 3], [5, 9, 9, 10]) == 1.0   # 동점 쌍은 세지 않는다
    assert kendall_tau_without_ties([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(4 / 6)
    assert kendall_tau_without_ties([1, 1, 1], [1, 2, 3]) is None


def test_top_k_breaks_ties_by_id_so_both_sides_break_them_the_same_way():
    assert top_k([30, 10, 20, 40], [1.0, 1.0, 2.0, 1.0], k=3) == [20, 10, 30]
    assert top_k([1, 2], [0.1, 0.2], k=20) == [2, 1]


def test_a_repository_that_picks_the_latest_clicks_by_microsecond_fails_items_1_and_2(model_text, predict, monkeypatch):
    """CI의 첫 PostgreSQL 실행에서 게이트가 실제로 잡은 어긋남을 고정해 둔다. 요청과 클릭이 밀리초 간격으로
    이어지면 같은 초에 클릭이 여러 건 쌓이고, 최근 20개의 경계가 그 초에 걸린다. 저장소가 그 20개를 마이크로초
    순으로 고르면 정수 초로 다시 계산하는 쪽과 다른 클릭이 남는다(short_cos·sess_cos가 0.04쯤 어긋났다)."""
    from app.recsys.types import ClickEvent
    from tests.recsys.fakes import FakeRepo

    def by_microsecond(self, user_id, since, until, limit):
        recent = sorted(
            (c for c in self.clicks if c[1] == user_id and since <= c[3] < until and c[2] in self.newsletters),
            key=lambda c: (c[3], c[0]), reverse=True,
        )[:limit]
        return [ClickEvent(c[3], self.newsletters[c[2]].embedding, c[2]) for c in recent]

    monkeypatch.setattr(FakeRepo, "recent_clicks", by_microsecond)
    drifted = simulate(n_items=200, n_users=4, n_requests=90, fast_requests=90, model_text=model_text, seed=2)

    report = run_gate(drifted.logs, drifted.requests, predict, drifted.serving_spec)

    assert report["features"]["pass"] is False
    off = {name for name, d in report["features"]["per_column_max_abs_diff"].items() if d > 1e-6}
    assert off == {"short_cos", "sess_cos"}  # 세는 열(short_len 등)은 같고, 어떤 클릭이 남았는지만 다르다
    assert report["scores"]["pass"] is False  # 피처가 어긋나면 같은 모델의 순서도 갈린다


def test_the_committed_report_is_evidence_about_the_current_feature_schema():
    """reports/recsys/parity_v1.json은 CI 실행이 쓴 파일을 옮긴 것이다. 서빙 피처의 정의가 바뀌면 그 파일은 지난
    정의의 증거다: 그대로 두고 "게이트 통과"라고 읽히지 않게, 새 실행의 파일로 바꿀 때까지 실패한다."""
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "reports" / "recsys" / "parity_v1.json"
    committed = json.loads(path.read_text(encoding="utf-8"))

    assert committed["meta"]["feature_schema_hash"] == serving.SCHEMA_HASH, (
        "서빙 피처 스키마가 리포트를 만든 때와 다릅니다. CI의 recsys-parity 아티팩트로 "
        "reports/recsys/parity_v1.json을 갱신하세요(reports/recsys/parity_v1.md)."
    )
    assert committed["meta"]["thresholds"] == THRESHOLDS
    assert committed["candidate_config"]["serving"] == SERVING_CANDIDATE_SPEC.as_dict()
    assert committed["meta"]["ci_run_id"] and committed["meta"]["commit"]  # 로컬에서 만든 파일이 아니다
    assert committed["pass"] is True
