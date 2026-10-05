"""v2에서 고친 개별 BLOCKER/MAJOR의 최소 재현 테스트 + run_one() 전체 배선을
작은 서브샘플로 검증하는 엔드투엔드 스모크 테스트 (요구사항 11).

순수 구조 테스트(1~4)는 실제 아카이브 없이 합성 데이터로 동작한다 - file_loader.
ArchiveBundle과 pipeline._pad_newsletters/_time_group_safe_split은 recommend_engine
클래스를 쓰지 않는 순수 pandas 로직이기 때문이다. 5번(엔드투엔드)만 실제
아카이브(data/team_archive, gitignored)가 필요해 skipif로 건너뛴다.
"""
import argparse
import dataclasses
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TEAM_REPRO_DIR = REPO_ROOT / "evaluation" / "recsys" / "team_repro"
ENGINE_ROOT = REPO_ROOT / "ai_workspace" / "recommend_engine"

sys.path.insert(0, str(TEAM_REPRO_DIR))
import file_loader as FL  # noqa: E402
import importlib.util  # noqa: E402

# 하네스 pipeline.py를 최상위 이름 `pipeline`으로 import하면 ai_workspace의 `pipeline`
# 패키지(tests/test_stage5_run_id.py 등이 쓰는)를 sys.modules에서 가려버려, 전체
# 스위트에서 수집 순서에 따라 다른 테스트가 깨진다 - 고유한 별칭으로만 로드한다.
_spec = importlib.util.spec_from_file_location("team_repro_pipeline", TEAM_REPRO_DIR / "pipeline.py")
PIPE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PIPE)
import protocol as PR  # noqa: E402


def _install_torch_stub():
    if "torch" not in sys.modules:
        torch_stub = types.ModuleType("torch")
        torch_stub.Tensor = type("Tensor", (), {})
        sys.modules["torch"] = torch_stub


def _tiny_bundle(n_newsletters: int = 10) -> FL.ArchiveBundle:
    base = datetime(2026, 1, 1)
    newsletters = pd.DataFrame(
        {
            "news_letter_id": list(range(1, n_newsletters + 1)),
            "title": [f"t{i}" for i in range(1, n_newsletters + 1)],
            "content": [f"c{i}" for i in range(1, n_newsletters + 1)],
            "created_at": [base - timedelta(days=1)] * n_newsletters,
            "embedding": [np.random.default_rng(i).normal(size=8).astype(np.float32) for i in range(n_newsletters)],
        }
    )
    categories = pd.DataFrame(
        {
            "news_letter_id": list(range(1, n_newsletters + 1)),
            "category_id": [1] * n_newsletters,
            "category_name": ["정치"] * n_newsletters,
            "source": ["json_title"] * n_newsletters,
            "confidence": [1.0] * n_newsletters,
        }
    )
    ctr_logs = pd.DataFrame(columns=["user_id", "news_letter_id", "timestamp", "is_clicked"])
    users = pd.DataFrame({"user_id": [1, 2]})
    empty_pref_cat = pd.DataFrame(columns=["user_id", "category_id"])
    empty_pref_nl = pd.DataFrame(columns=["user_id", "news_letter_id"])
    empty_onboarding = pd.DataFrame(columns=["user_id", "category_id", "shown_news_letter_ids"])
    gen_cols = ["user_id", "news_letter_id", "timestamp", "is_clicked"]
    return FL.ArchiveBundle(
        newsletters=newsletters, categories=categories, ctr_logs=ctr_logs, users=users,
        preferred_categories=empty_pref_cat, preferred_newsletters=empty_pref_nl,
        onboarding_log=empty_onboarding,
        generator_train_keys=pd.DataFrame(columns=gen_cols), generator_valid_keys=pd.DataFrame(columns=gen_cols),
    )


# --- 1) BLOCKER #4: padded_400이 실제로 400건을 쓰는지 --------------------------


def test_padded_400_produces_exactly_400_candidates():
    bundle = _tiny_bundle(n_newsletters=195)
    padded = PIPE._pad_newsletters(bundle, target_size=400, seed=0)
    assert len(padded.newsletters) == 400
    candidate_ids = PIPE.resolve_candidate_pool(padded, "padded_400", datetime(2026, 1, 2), seed=0)
    n_candidates = len(candidate_ids) if candidate_ids is not None else len(padded.newsletters)
    assert n_candidates == 400


def test_padded_400_noop_when_already_at_target():
    bundle = _tiny_bundle(n_newsletters=400)
    padded = PIPE._pad_newsletters(bundle, target_size=400, seed=0)
    assert len(padded.newsletters) == 400  # 이미 400이면 추가 패딩 없음


# --- 2) BLOCKER #6: generator_split 학습 로그가 valid 뉴스레터를 절대 포함하지 않음 ----


def test_generator_split_training_bundle_excludes_valid_newsletters():
    base = datetime(2026, 1, 1)
    bundle = _tiny_bundle(n_newsletters=10)
    train_keys = pd.DataFrame(
        {
            "user_id": [1, 1, 2],
            "news_letter_id": [1, 2, 3],  # train: 1~3
            "timestamp": [base, base, base],
            "is_clicked": [1, 1, 1],
        }
    )
    valid_keys = pd.DataFrame(
        {
            "user_id": [1, 2],
            "news_letter_id": [8, 9],  # valid: 8~9 (train과 겹치지 않음)
            "timestamp": [base, base],
            "is_clicked": [1, 1],
        }
    )
    bundle = dataclasses.replace(bundle, generator_train_keys=train_keys, generator_valid_keys=valid_keys)

    train_bundle = FL.bundle_for_generator_split_training(bundle)
    train_nids = set(train_bundle.ctr_logs["news_letter_id"].tolist())
    valid_nids = set(valid_keys["news_letter_id"].tolist())
    assert train_nids.isdisjoint(valid_nids)
    assert train_nids == {1, 2, 3}


# --- 3) BLOCKER #1: point-in-time 로더가 answer_start 이후 로그를 물리적으로 배제 ------


def test_bundle_before_excludes_logs_at_or_after_cutoff():
    base = datetime(2026, 1, 1, 0, 0, 0)
    bundle = _tiny_bundle(n_newsletters=5)
    logs = pd.DataFrame(
        {
            "user_id": [1, 1, 1],
            "news_letter_id": [1, 2, 3],
            "timestamp": [base, base + timedelta(minutes=5), base + timedelta(minutes=10)],
            "is_clicked": [1, 1, 1],
        }
    )
    bundle = dataclasses.replace(bundle, ctr_logs=logs)
    cutoff = base + timedelta(minutes=5)

    restricted = FL.bundle_before(bundle, cutoff)
    assert (restricted.ctr_logs["timestamp"] < cutoff).all()
    assert len(restricted.ctr_logs) == 1  # base(00:00)만 cutoff(00:05) 이전


# --- 4) MAJOR #5: inner-validation 분할이 정답 구간(answer_start 이후)을 절대 포함하지 않음 ----


def test_inner_validation_split_never_includes_answer_window_rows():
    base = datetime(2026, 1, 1, 0, 0, 0)
    answer_start = base + timedelta(minutes=100)
    rows = []
    # 학습 구간(0~99분) 50건 + 정답 구간(100~119분) 20건 - 후자는 train_all 필터링으로 배제되어야 함
    for i in range(50):
        rows.append({"user_id": i % 5, "news_id": i, "label": i % 2, "_timestamp": base + timedelta(minutes=i)})
    for i in range(20):
        rows.append({"user_id": i % 5, "news_id": 1000 + i, "label": 1, "_timestamp": answer_start + timedelta(minutes=i)})
    full_df = pd.DataFrame(rows)

    train_all = full_df[full_df["_timestamp"] < answer_start].reset_index(drop=True)
    assert len(train_all) == 50

    train_df, inner_valid_df = PIPE._time_group_safe_split(train_all, val_ratio=0.2, group_key="user_id")

    assert (train_df["_timestamp"] < answer_start).all()
    assert (inner_valid_df["_timestamp"] < answer_start).all()
    assert len(train_df) + len(inner_valid_df) == len(train_all)
    # 두 분할이 실제로 서로 다른 news_id 집합을 커버해야 함 (겹치지 않는 분할)
    assert set(train_df["news_id"]).isdisjoint(set(inner_valid_df["news_id"]))


def test_time_group_safe_split_keeps_group_intact():
    base = datetime(2026, 1, 1)
    # user 3의 클릭 시각(base+9분) 부근에 그룹 경계가 오도록 구성: user별 같은 timestamp를
    # 공유하는 2행(양성+음성)짜리 그룹 10개.
    rows = []
    for i in range(10):
        ts = base + timedelta(minutes=i)
        rows.append({"user_id": i, "news_id": i * 2, "label": 1, "_timestamp": ts})
        rows.append({"user_id": i, "news_id": i * 2 + 1, "label": 0, "_timestamp": ts})
    df = pd.DataFrame(rows)
    train_df, valid_df = PIPE._time_group_safe_split(df, val_ratio=0.2, group_key="user_timestamp")
    # (user_id, _timestamp) 조합이 train/valid 양쪽에 걸쳐 나타나면 안 됨
    train_groups = set(zip(train_df["user_id"], train_df["_timestamp"]))
    valid_groups = set(zip(valid_df["user_id"], valid_df["_timestamp"]))
    assert train_groups.isdisjoint(valid_groups)


# --- 5) 엔드투엔드: run_one()이 실제로 1차(point-in-time)/2차(as-written) 두 행을 만드는지 ----
# 위 1~4번(순수 구조 테스트)은 합성 데이터만 쓰므로 이 skipif의 영향을 받지 않는다 -
# 모듈 전체에 pytestmark를 걸지 않고, 실제 아카이브가 필요한 테스트에만 개별 데코레이터로 건다.
_needs_archive = pytest.mark.skipif(
    not FL.DATA_ROOT.exists(), reason="data/team_archive가 없는 환경 (gitignored 아카이브 데이터)"
)


@pytest.fixture(scope="module")
def small_real_bundle():
    bundle = FL.load_archive_bundle()
    small_users = bundle.users.head(20).reset_index(drop=True)
    small_news = bundle.newsletters.head(50).reset_index(drop=True)
    keep_uids = set(small_users["user_id"])
    keep_nids = set(small_news["news_letter_id"])
    small_logs = bundle.ctr_logs[
        bundle.ctr_logs["user_id"].isin(keep_uids) & bundle.ctr_logs["news_letter_id"].isin(keep_nids)
    ].reset_index(drop=True)
    small_cats = bundle.categories[bundle.categories["news_letter_id"].isin(keep_nids)].reset_index(drop=True)
    small_pref_cats = bundle.preferred_categories[bundle.preferred_categories["user_id"].isin(keep_uids)]
    small_pref_nl = bundle.preferred_newsletters[bundle.preferred_newsletters["user_id"].isin(keep_uids)]
    return dataclasses.replace(
        bundle, newsletters=small_news, users=small_users, ctr_logs=small_logs, categories=small_cats,
        preferred_categories=small_pref_cats, preferred_newsletters=small_pref_nl,
    )


@_needs_archive
def test_run_one_team_split_produces_primary_and_as_written_rows(small_real_bundle, monkeypatch):
    monkeypatch.setattr(FL, "load_archive_bundle", lambda: small_real_bundle)
    _install_torch_stub()

    clicks = small_real_bundle.ctr_logs[small_real_bundle.ctr_logs["is_clicked"] == 1]
    assert len(clicks) >= 10, "서브샘플에 클릭이 너무 적습니다 - head() 크기를 늘려야 합니다."
    answer_start = PR.compute_fixed_answer_start(clicks, val_ratio=0.3)

    args = argparse.Namespace(
        engine_root=str(ENGINE_ROOT), version="current", protocol="team_split",
        label_mode="clicks_only", leakage_mode="fixed", candidate_pool="full_195",
        negative_source="random", objective_override="none",
        answer_start=answer_start.isoformat(), code_sha="test", harness_sha="test",
        seed=0, top_k=5, out="/tmp/_unused_test_pipeline_v2_out.json",
    )
    result = PIPE.run_one(args)

    for key in ("primary", "as_written"):
        assert key in result
        assert "aggregate_metrics" in result[key]
        assert "per_user_metrics" in result[key]

    assert result["answer_start"] == answer_start.isoformat()
    assert result["n_cold"] + result["n_warm"] == len(small_real_bundle.users)
    assert isinstance(result["config_hash"], str) and result["config_hash"]
    assert result["n_train"] > 0
    assert "cold" in result["primary"] and "warm" in result["primary"]
    assert "seen_filtered" in result["primary"]

    for mk in ("mrr",):
        if result["primary"]["aggregate_metrics"]:
            assert 0.0 <= result["primary"]["aggregate_metrics"][mk] <= 1.0
        if result["as_written"]["aggregate_metrics"]:
            assert 0.0 <= result["as_written"]["aggregate_metrics"][mk] <= 1.0

    assert "team_final_written" not in result  # version="current"에는 없어야 함


@_needs_archive
def test_run_one_generator_split_trains_only_on_train_keys(monkeypatch):
    """195건 전체를 후보 풀로 쓰되(모델이 안전하게 생성기 train/valid의 실제 id
    범위를 모두 보게), 유저만 20명으로 줄여 빠르게 돈다 - head(50) 뉴스레터
    서브샘플은 generator_valid_keys(id 155~198)와 겹치지 않아 이 테스트가
    스킵되어 버렸다(회귀: 실제로는 아무것도 검증하지 않고 항상 통과/스킵)."""
    _install_torch_stub()
    full_bundle = FL.load_archive_bundle()
    keep_uids = set(full_bundle.users["user_id"].head(20))

    small_logs = full_bundle.ctr_logs[full_bundle.ctr_logs["user_id"].isin(keep_uids)].reset_index(drop=True)
    small_pref_cats = full_bundle.preferred_categories[full_bundle.preferred_categories["user_id"].isin(keep_uids)]
    small_pref_nl = full_bundle.preferred_newsletters[full_bundle.preferred_newsletters["user_id"].isin(keep_uids)]
    small_users = full_bundle.users[full_bundle.users["user_id"].isin(keep_uids)].reset_index(drop=True)
    gen_train = full_bundle.generator_train_keys[full_bundle.generator_train_keys["user_id"].isin(keep_uids)]
    gen_valid = full_bundle.generator_valid_keys[full_bundle.generator_valid_keys["user_id"].isin(keep_uids)]
    if gen_train.empty or gen_valid.empty:
        pytest.skip("서브샘플 유저에 generator_train/valid 로그가 없습니다 - head() 크기를 늘려야 합니다.")

    bundle = dataclasses.replace(
        full_bundle, users=small_users, ctr_logs=small_logs, preferred_categories=small_pref_cats,
        preferred_newsletters=small_pref_nl, generator_train_keys=gen_train, generator_valid_keys=gen_valid,
    )
    monkeypatch.setattr(FL, "load_archive_bundle", lambda: bundle)

    args = argparse.Namespace(
        engine_root=str(ENGINE_ROOT), version="current", protocol="generator_split",
        label_mode="clicks_only", leakage_mode="fixed", candidate_pool="full_195",
        negative_source="random", objective_override="none",
        answer_start=None, code_sha="test", harness_sha="test",
        seed=0, top_k=5, out="/tmp/_unused_test_pipeline_v2_gen_out.json",
    )
    result = PIPE.run_one(args)
    assert result["answer_start"] is None
    assert "primary" in result
    valid_nids = set(gen_valid["news_letter_id"].tolist())
    assert result["n_train_source_newsletters"] <= len(set(gen_train["news_letter_id"].tolist()))
    # 학습 소스 뉴스레터 집합이 valid 뉴스레터와 절대 겹치지 않아야 함 (BLOCKER #6 회귀)
    train_bundle = FL.bundle_for_generator_split_training(bundle)
    assert set(train_bundle.ctr_logs["news_letter_id"].unique()).isdisjoint(valid_nids)


# --- v2.1 ---------------------------------------------------------------------


def test_time_group_safe_split_user_id_groups_never_straddle_when_interleaved():
    """user_id 그룹이 시간상 섞여 있으면 인접 행 검사만으로는 한 유저가 양쪽에 걸친다
    (v2 리뷰: 85명 중 32명). 그룹 소속 검사 후 그룹 단위 분할로 바꿔야 한다."""
    base = datetime(2026, 1, 1)
    rows = []
    for i in range(100):  # 10명 유저의 행이 1분 간격으로 번갈아 나타남
        rows.append({"user_id": i % 10, "news_id": i, "label": i % 2, "_timestamp": base + timedelta(minutes=i)})
    df = pd.DataFrame(rows)
    train_df, valid_df = PIPE._time_group_safe_split(df, val_ratio=0.2, group_key="user_id")
    assert set(train_df["user_id"]).isdisjoint(set(valid_df["user_id"]))
    assert len(train_df) + len(valid_df) == len(df)
    assert len(valid_df) == 20  # 10행짜리 그룹 2개 = 목표 20%에 정확히 맞음
    assert PIPE._n_groups_on_both_sides(train_df, valid_df, "user_id") == 0


def test_team_final_written_ground_truth_clicks_only_vs_all_rows():
    """정답 정의 BLOCKER 회귀: 올바른 번역은 클릭만, v2 정의 오류는 노출 전체."""
    base = pd.Timestamp("2026-01-01")
    logs = pd.DataFrame(
        {
            "user_id": [1, 1, 1, 2],
            "news_letter_id": [10, 11, 12, 10],
            "timestamp": [base] * 4,
            "is_clicked": [1, 0, 0, 1],
        }
    )
    clicks = PIPE.team_final_written_ground_truth(logs, clicks_only=True)
    all_rows = PIPE.team_final_written_ground_truth(logs, clicks_only=False)
    assert clicks == {1: {10}, 2: {10}}
    assert all_rows == {1: {10, 11, 12}, 2: {10}}


class _OrderKeepingReranker:
    """MMR 대신 입력 순서를 그대로 top_k로 자르는 가짜 reranker(동점 처리만 격리)."""

    def rerank_for_user(self, scores, embeddings, top_k, num_preferred_categories):
        return [(i, scores[i]) for i in range(min(top_k, len(scores)))]


def _scored_df_with_ties():
    # 유저 1: 아이템 1이 최고점, 2~9는 동점
    return pd.DataFrame(
        {
            "user_id": [1] * 9,
            "news_id": list(range(1, 10)),
            "score": [1.0] + [0.5] * 8,
        }
    )


def test_build_recommendations_tie_rng_randomizes_only_tied_items():
    scored = _scored_df_with_ties()
    news_dict = {i: types.SimpleNamespace(embedding=np.zeros(2)) for i in range(1, 10)}
    det = PIPE._build_recommendations(scored, _OrderKeepingReranker(), news_dict, {}, top_k=4)
    det2 = PIPE._build_recommendations(scored, _OrderKeepingReranker(), news_dict, {}, top_k=4)
    assert det == det2  # tie_rng=None이면 결정적(기존 경로 유지)
    orders = set()
    for seed in range(20):
        rec = PIPE._build_recommendations(
            scored, _OrderKeepingReranker(), news_dict, {}, top_k=4, tie_rng=np.random.default_rng(seed)
        )
        assert rec[1][0] == 1  # 동점이 아닌 최고점 아이템은 항상 1위
        orders.add(tuple(rec[1][1:]))
    assert len(orders) > 1  # 동점 구간의 순서는 추첨마다 바뀐다


def test_top_tie_sizes_counts_items_tied_at_max():
    assert PIPE._top_tie_sizes(_scored_df_with_ties()) == [1]
    flat = pd.DataFrame({"user_id": [2] * 4, "news_id": [1, 2, 3, 4], "score": [0.3] * 4})
    assert PIPE._top_tie_sizes(flat) == [4]


@_needs_archive
def test_run_one_tie_draws_and_fixed_rounds_are_recorded(small_real_bundle, monkeypatch):
    monkeypatch.setattr(FL, "load_archive_bundle", lambda: small_real_bundle)
    _install_torch_stub()
    clicks = small_real_bundle.ctr_logs[small_real_bundle.ctr_logs["is_clicked"] == 1]
    answer_start = PR.compute_fixed_answer_start(clicks, val_ratio=0.3)
    args = argparse.Namespace(
        engine_root=str(ENGINE_ROOT), version="current", protocol="team_split",
        label_mode="clicks_only", leakage_mode="fixed", candidate_pool="full_195",
        negative_source="random", objective_override="none",
        answer_start=answer_start.isoformat(), code_sha="test", harness_sha="test",
        seed=0, top_k=5, out="/tmp/_unused_test_pipeline_v21_out.json", tie_draws=2, fixed_rounds=3,
    )
    result = PIPE.run_one(args)
    assert result["rounds_policy"] == "fixed_3_rounds_no_early_stopping"
    assert result["n_trees"] == 3 and result["best_iteration"] is None
    assert result["degenerate_single_tree"] is False
    tr = result["primary"]["tie_random"]
    assert tr["n_draws"] == 2 and "mrr" in tr["aggregate_mean"]
    assert tr["aggregate_min"]["mrr"] <= tr["aggregate_mean"]["mrr"] <= tr["aggregate_max"]["mrr"]
    assert result["as_written"]["tie_random"]["n_draws"] == 2
    assert "unshown_filtered" in result["primary"]
    assert result["inner_split_groups_on_both_sides"] == 0


# --- v2.2: 조기 종료 지표의 동점 처리 아티팩트 ----------------------------------------


def _positive_first_groups(n_groups: int = 300, group_size: int = 6):
    """엔진의 create_train_dataset이 만드는 모양: 그룹마다 positive 1행이 먼저, negative가 뒤."""
    labels = np.zeros(n_groups * group_size)
    labels[::group_size] = 1.0
    group_ids = np.repeat(np.arange(n_groups), group_size)
    return labels, group_ids


def _lgb_ndcg_of_constant_model(labels, group_sizes):
    import lightgbm as lgb

    X = np.ones((len(labels), 1))
    train = lgb.Dataset(X, labels, group=group_sizes)
    valid = lgb.Dataset(X, labels, group=group_sizes, reference=train)
    history = {}
    lgb.train(
        {"objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [5], "label_gain": [0, 1], "verbose": -1},
        train, num_boost_round=1, valid_sets=[valid], callbacks=[lgb.record_evaluation(history)],
    )
    return float(history["valid_0"]["ndcg@5"][0])


def test_lightgbm_ndcg_breaks_ties_by_row_order():
    """아티팩트의 전제를 고정한다: 모든 점수가 같은(상수) 모델의 LightGBM NDCG@5는 positive가
    그룹 첫 행이면 1.0, 마지막 행이면 0.0이다. LightGBM이 이 동작을 바꾸면 이 테스트가
    알려준다(그때는 섞기가 필요 없어진다)."""
    labels, group_ids = _positive_first_groups()
    sizes = [6] * (len(labels) // 6)
    assert _lgb_ndcg_of_constant_model(labels, sizes) == pytest.approx(1.0)
    last = labels.reshape(-1, 6)[:, ::-1].ravel()
    assert _lgb_ndcg_of_constant_model(last, sizes) == pytest.approx(0.0)


def test_ndcg_by_tie_policy_constant_scores():
    labels, group_ids = _positive_first_groups(n_groups=50)
    out = PIPE.ndcg_by_tie_policy(np.zeros(len(labels)), labels, group_ids, k=5)
    disc = 1.0 / np.log2(2.0 + np.arange(5))
    assert out["data_order"] == pytest.approx(1.0)
    assert out["reversed_order"] == pytest.approx(0.0)
    assert out["tie_expected"] == pytest.approx(disc.sum() / 6.0)  # 6자리 중 앞 5자리에 균등하게 놓일 기대값
    assert out["tied_positive_group_share"] == pytest.approx(1.0)
    assert out["n_groups"] == 50


def test_ndcg_by_tie_policy_without_ties_all_policies_agree():
    rng = np.random.default_rng(0)
    labels, group_ids = _positive_first_groups(n_groups=40)
    scores = rng.normal(size=len(labels))  # 연속값이라 동점 없음
    out = PIPE.ndcg_by_tie_policy(scores, labels, group_ids, k=5)
    assert out["data_order"] == pytest.approx(out["reversed_order"])
    assert out["data_order"] == pytest.approx(out["tie_expected"])
    assert out["tied_positive_group_share"] == 0.0


def test_ndcg_by_tie_policy_data_order_matches_lightgbm_and_expected_is_order_invariant():
    """data_order는 LightGBM이 기록하는 NDCG와 같아야 하고(동점 포함), tie_expected는 행
    순서를 바꿔도 같아야 한다."""
    import lightgbm as lgb

    rng = np.random.default_rng(1)
    n_groups, size = 200, 6
    labels, group_ids = _positive_first_groups(n_groups, size)
    X = rng.integers(0, 3, size=(len(labels), 2)).astype(float)  # 값이 3종류뿐이라 동점이 많다
    X[:, 0] += labels * (rng.random(len(labels)) < 0.4)  # 약한 신호
    sizes = [size] * n_groups
    train = lgb.Dataset(X, labels, group=sizes)
    valid = lgb.Dataset(X, labels, group=sizes, reference=train)
    history = {}
    booster = lgb.train(
        {"objective": "lambdarank", "metric": "ndcg", "ndcg_eval_at": [5], "label_gain": [0, 1],
         "verbose": -1, "num_leaves": 4, "min_data_in_leaf": 5},
        train, num_boost_round=2, valid_sets=[valid], callbacks=[lgb.record_evaluation(history)],
    )
    scores = booster.predict(X)
    assert len(set(np.round(scores, 10))) < 30  # 동점이 실제로 있다
    out = PIPE.ndcg_by_tie_policy(scores, labels, group_ids, k=5)
    assert out["data_order"] == pytest.approx(history["valid_0"]["ndcg@5"][-1], abs=1e-9)
    assert out["data_order"] > out["tie_expected"] > out["reversed_order"]  # positive가 먼저라 부풀려진다

    perm = rng.permutation(len(labels))
    shuffled = PIPE.ndcg_by_tie_policy(scores[perm], labels[perm], group_ids[perm], k=5)
    assert shuffled["tie_expected"] == pytest.approx(out["tie_expected"], abs=1e-12)


def test_ndcg_by_tie_policy_group_without_positive_counts_as_one_like_lightgbm():
    out = PIPE.ndcg_by_tie_policy([0.3, 0.2, 0.1], [0, 0, 0], ["g", "g", "g"], k=5)
    assert out["data_order"] == out["reversed_order"] == out["tie_expected"] == 1.0


def _inner_valid_frame(n_groups: int = 200):
    labels, group_ids = _positive_first_groups(n_groups)
    base = datetime(2026, 1, 1)
    return pd.DataFrame(
        {
            "user_id": group_ids,
            "news_id": np.arange(len(labels)),
            "label": labels.astype(int),
            "_timestamp": [base + timedelta(seconds=int(g)) for g in group_ids],
            "f0": 1.0,
        }
    )


def test_order_inner_valid_engine_keeps_rows_and_shuffled_is_seeded_permutation():
    df = _inner_valid_frame()
    assert PIPE.order_inner_valid_for_early_stopping(df, "engine", seed=42) is df
    a = PIPE.order_inner_valid_for_early_stopping(df, "shuffled", seed=42)
    b = PIPE.order_inner_valid_for_early_stopping(df, "shuffled", seed=42)
    c = PIPE.order_inner_valid_for_early_stopping(df, "shuffled", seed=43)
    pd.testing.assert_frame_equal(a, b)  # 같은 시드면 같은 순서(캐시·재현성)
    assert not a["news_id"].equals(c["news_id"])
    assert sorted(a["news_id"]) == sorted(df["news_id"])  # 행을 잃거나 복제하지 않는다
    assert not a["news_id"].equals(df["news_id"])
    with pytest.raises(ValueError):
        PIPE.order_inner_valid_for_early_stopping(df, "sorted", seed=1)


def test_shuffling_removes_positive_first_ordering():
    df = _inner_valid_frame(n_groups=600)
    gid = PIPE._group_ids_for(df, "user_timestamp")
    assert PIPE.positive_first_share(df["label"].to_numpy(), gid) == 1.0
    shuffled = PIPE.order_inner_valid_for_early_stopping(df, "shuffled", seed=7)
    share = PIPE.positive_first_share(shuffled["label"].to_numpy(), PIPE._group_ids_for(shuffled, "user_timestamp"))
    assert 1 / 6 - 0.06 < share < 1 / 6 + 0.06  # 그룹 6행 중 positive 1행


def test_lightgbm_ndcg_on_shuffled_valid_tracks_tie_expected_value():
    """섞은 inner-valid 위에서는 상수 모델의 LightGBM NDCG가 1.0이 아니라 동점 무작위
    기대값 근처로 내려온다 - 섞기가 조기 종료 지표의 부풀림을 없앤다는 직접 확인."""
    df = _inner_valid_frame(n_groups=600)
    shuffled = PIPE.order_inner_valid_for_early_stopping(df, "shuffled", seed=3)
    # 엔진의 _build_groups와 같은 방식: 그룹 id로 안정 정렬
    gid = PIPE._group_ids_for(shuffled, "user_timestamp")
    ordered = shuffled.assign(_g=gid.values).sort_values("_g", kind="mergesort")
    sizes = ordered.groupby("_g", sort=False).size().tolist()
    measured = _lgb_ndcg_of_constant_model(ordered["label"].to_numpy().astype(float), sizes)
    expected = PIPE.ndcg_by_tie_policy(np.zeros(len(df)), df["label"].to_numpy(), PIPE._group_ids_for(df, "user_timestamp"), k=5)
    assert expected["data_order"] == pytest.approx(1.0)
    assert abs(measured - expected["tie_expected"]) < 0.04
    assert measured < 0.6


@_needs_archive
@pytest.mark.parametrize("es_valid_order", ["shuffled", "engine"])
def test_run_one_records_es_valid_order_and_tie_diagnostic(small_real_bundle, monkeypatch, es_valid_order):
    monkeypatch.setattr(FL, "load_archive_bundle", lambda: small_real_bundle)
    _install_torch_stub()
    clicks = small_real_bundle.ctr_logs[small_real_bundle.ctr_logs["is_clicked"] == 1]
    answer_start = PR.compute_fixed_answer_start(clicks, val_ratio=0.3)
    args = argparse.Namespace(
        engine_root=str(ENGINE_ROOT), version="current", protocol="team_split",
        label_mode="clicks_only", leakage_mode="fixed", candidate_pool="full_195",
        negative_source="random", objective_override="none",
        answer_start=answer_start.isoformat(), code_sha="test", harness_sha="test",
        seed=0, top_k=5, out="/tmp/_unused_test_pipeline_v22_out.json", tie_draws=0, fixed_rounds=0,
        es_valid_order=es_valid_order,
    )
    result = PIPE.run_one(args)
    assert result["es_valid_order"] == es_valid_order
    assert result["rounds_policy"] == "inner_valid_early_stopping"
    assert result["low_best_iteration"] == (result["best_iteration"] <= 5)
    diag = result["es_tie_diagnostic"]
    assert diag["rank_group_key"] == "user_timestamp"
    frames = diag["frames"]
    # 엔진이 만든 순서에서는 모든 그룹의 첫 행이 positive다(아티팩트의 전제).
    assert frames["engine_row_order"]["positive_first_share"] == 1.0
    if es_valid_order == "engine":
        assert frames["as_evaluated"]["positive_first_share"] == 1.0
    else:
        assert frames["as_evaluated"]["positive_first_share"] < 0.5
    # 하네스가 계산한 data_order NDCG는 LightGBM이 조기 종료에 쓴 값과 같아야 한다.
    best = str(result["best_iteration"])
    for metric_name, recorded in result["best_score_inner_valid"]["valid_0"].items():
        mine = frames["as_evaluated"]["by_iteration"][best][metric_name]["data_order"]
        assert mine == pytest.approx(recorded, abs=1e-6)
    # 동점 무작위 기대값은 행 순서와 무관하다.
    for it, entry in frames["as_evaluated"]["by_iteration"].items():
        other = frames["engine_row_order"]["by_iteration"][it]
        assert entry["ndcg@5"]["tie_expected"] == pytest.approx(other["ndcg@5"]["tie_expected"], abs=1e-9)
    assert "list_len_stats" in result["primary"]["unshown_filtered"]


@_needs_archive
def test_run_one_binary_objective_has_no_ndcg_tie_diagnostic(small_real_bundle, monkeypatch):
    """AUC로 조기 종료하는 설정에는 NDCG 동점 진단이 없다(AUC는 동점을 묶어 계산한다)."""
    monkeypatch.setattr(FL, "load_archive_bundle", lambda: small_real_bundle)
    _install_torch_stub()
    clicks = small_real_bundle.ctr_logs[small_real_bundle.ctr_logs["is_clicked"] == 1]
    answer_start = PR.compute_fixed_answer_start(clicks, val_ratio=0.3)
    args = argparse.Namespace(
        engine_root=str(ENGINE_ROOT), version="current", protocol="team_split",
        label_mode="clicks_only", leakage_mode="fixed", candidate_pool="full_195",
        negative_source="random", objective_override="binary",
        answer_start=answer_start.isoformat(), code_sha="test", harness_sha="test",
        seed=0, top_k=5, out="/tmp/_unused_test_pipeline_v22_bin_out.json", tie_draws=0, fixed_rounds=0,
    )
    result = PIPE.run_one(args)
    assert result["objective_used"] == "binary"
    assert result["es_valid_order"] == "shuffled"  # 섞기는 모든 arm에 똑같이 적용된다
    assert result["es_tie_diagnostic"] is None
