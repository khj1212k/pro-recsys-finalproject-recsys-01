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
import pipeline as PIPE  # noqa: E402
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
