"""file_loader.py 테스트: (1) 아카이브 로딩이 예상 모양대로 나오는가, (2)
FileDataLoader가 실제 recommend_engine(current 버전)의 DataLoader를 상속해서
FeatureEngineer/LGBMDataset가 기대하는 형태(shape/columns)를 그대로 만들어내는가
("로더 패리티").

data/team_archive가 없는 환경(CI 등)에서는 스킵한다.
"""
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

pytestmark = pytest.mark.skipif(
    not FL.DATA_ROOT.exists(), reason="data/team_archive가 없는 환경 (gitignored 아카이브 데이터)"
)


def _install_torch_stub():
    if "torch" not in sys.modules:
        torch_stub = types.ModuleType("torch")
        torch_stub.Tensor = type("Tensor", (), {})
        sys.modules["torch"] = torch_stub


@pytest.fixture(scope="module")
def bundle():
    return FL.load_archive_bundle()


@pytest.fixture(scope="module")
def current_engine():
    if str(ENGINE_ROOT) not in sys.path:
        sys.path.insert(0, str(ENGINE_ROOT))
    _install_torch_stub()
    from src.data.data_loader import DataLoader, NewsItem, UserProfile  # noqa: E402

    return DataLoader, NewsItem, UserProfile


def _basic_config():
    return {
        "data": {"max_history_days": 28, "validation_ratio": 0.2},
        "time_decay": {"news_half_life_days": 7, "min_weight": 0.01},
    }


def test_bundle_shapes(bundle):
    assert len(bundle.newsletters) == 195
    assert set(bundle.newsletters.columns) >= {"news_letter_id", "title", "content", "created_at", "embedding"}
    assert len(bundle.users) == 100
    assert len(bundle.ctr_logs) == 19500
    assert set(bundle.ctr_logs.columns) == {"user_id", "news_letter_id", "timestamp", "is_clicked"}


def test_load_embedded_news_matches_data_loader_news_item_shape(bundle, current_engine):
    DataLoader, NewsItem, _ = current_engine
    config = _basic_config()
    pinned_now = bundle.dataset_end_time.to_pydatetime()
    loader = FL.build_file_data_loader(DataLoader, NewsItem, config, bundle, pinned_now)

    news = loader.load_embedded_news()
    assert len(news) == 195
    sample = next(iter(news.values()))
    # NewsItem 데이터클래스 필드와 정확히 일치해야 팀 FeatureEngineer가 그대로 동작한다.
    assert hasattr(sample, "news_id")
    assert hasattr(sample, "title")
    assert hasattr(sample, "content")
    assert hasattr(sample, "category_ids")
    assert hasattr(sample, "embedding")
    assert hasattr(sample, "timestamp")
    assert sample.embedding.shape == (FL.EMBEDDING_DIM,)
    assert isinstance(sample.category_ids, list)


def test_load_ctr_logs_columns_and_label_mode(bundle, current_engine):
    DataLoader, NewsItem, _ = current_engine
    config = _basic_config()
    pinned_now = bundle.dataset_end_time.to_pydatetime()

    loader_clicks = FL.build_file_data_loader(
        DataLoader, NewsItem, config, bundle, pinned_now, label_mode=FL.LabelMode.CLICKS_ONLY
    )
    logs_clicks = loader_clicks.load_ctr_logs()
    assert list(logs_clicks.columns) == ["user_id", "news_letter_id", "timestamp"]
    assert len(logs_clicks) == int((bundle.ctr_logs["is_clicked"] == 1).sum())

    loader_all = FL.build_file_data_loader(
        DataLoader, NewsItem, config, bundle, pinned_now, label_mode=FL.LabelMode.ALL_ROWS
    )
    logs_all = loader_all.load_ctr_logs()
    assert len(logs_all) == len(bundle.ctr_logs)


def test_max_history_days_filters_relative_to_pinned_now(bundle, current_engine):
    DataLoader, NewsItem, _ = current_engine
    config = _basic_config()
    config["data"]["max_history_days"] = 0  # pinned_now 당일 로그만 남아야 함(사실상 거의 다 제거)
    pinned_now = bundle.dataset_start_time.to_pydatetime()  # 데이터셋 '시작' 시점을 now로 고정
    loader = FL.build_file_data_loader(DataLoader, NewsItem, config, bundle, pinned_now)
    logs = loader.load_ctr_logs()
    # max_history_days=0 -> cutoff == pinned_now, pinned_now 이전 로그가 없으므로 거의 비어야 함
    assert (logs["timestamp"] >= pinned_now).all() or logs.empty


def test_get_all_news_ids_respects_candidate_pool_restriction(bundle, current_engine):
    DataLoader, NewsItem, _ = current_engine
    config = _basic_config()
    pinned_now = bundle.dataset_end_time.to_pydatetime()
    restricted = [4, 5, 6]
    loader = FL.build_file_data_loader(
        DataLoader, NewsItem, config, bundle, pinned_now, candidate_pool_ids=restricted
    )
    assert loader.get_all_news_ids() == restricted
    # load_embedded_news는 candidate_pool과 무관하게 항상 전체(195)를 반환해야 한다
    # (히스토리 임베딩 계산은 후보 풀 제한과 독립적이어야 하므로).
    assert len(loader.load_embedded_news()) == 195


def test_get_all_user_ids_matches_bundle_users(bundle, current_engine):
    DataLoader, NewsItem, _ = current_engine
    config = _basic_config()
    pinned_now = bundle.dataset_end_time.to_pydatetime()
    loader = FL.build_file_data_loader(DataLoader, NewsItem, config, bundle, pinned_now)
    assert sorted(loader.get_all_user_ids()) == sorted(bundle.users["user_id"].tolist())


def test_build_user_profiles_returns_userprofile_per_user(bundle, current_engine):
    DataLoader, NewsItem, UserProfile = current_engine
    config = _basic_config()
    pinned_now = bundle.dataset_end_time.to_pydatetime()
    loader = FL.build_file_data_loader(DataLoader, NewsItem, config, bundle, pinned_now)
    profiles = loader.build_user_profiles()
    assert len(profiles) == 100
    sample = next(iter(profiles.values()))
    assert isinstance(sample, UserProfile)
    assert sample.history_embedding.shape == (FL.EMBEDDING_DIM,)


def test_freeze_datetime_now_pins_module_now(current_engine):
    DataLoader, _, _ = current_engine
    import importlib

    module = importlib.import_module(DataLoader.__module__)
    pinned = datetime(2020, 1, 1, 12, 0, 0)
    FL.freeze_datetime_now(module, pinned)
    assert module.datetime.now() == pinned


def test_compute_history_embedding_is_point_in_time_on_current(bundle, current_engine):
    """current 버전에만 있는 point-in-time 메서드가 cutoff 이전 로그만 쓰는지 직접 검증
    (leakage 수정의 핵심 계약)."""
    DataLoader, NewsItem, _ = current_engine
    config = _basic_config()
    pinned_now = bundle.dataset_end_time.to_pydatetime()
    loader = FL.build_file_data_loader(DataLoader, NewsItem, config, bundle, pinned_now)
    assert hasattr(loader, "compute_history_embedding")

    logs = loader.load_ctr_logs()
    news = loader.load_embedded_news()
    uid = bundle.users["user_id"].iloc[0]

    before_any_click = bundle.dataset_start_time.to_pydatetime() - timedelta(days=1)
    emb_before = loader.compute_history_embedding(uid, before_any_click, logs, news)
    assert np.allclose(emb_before, 0.0)


def test_leaky_history_embedding_mode_ignores_cutoff(bundle, current_engine):
    """leakage_mode='leaky'가 cutoff을 무시하고 pinned_now 기준 전체 로그를 쓰는지 확인
    (decomposition (a) 실험의 전제 조건)."""
    DataLoader, NewsItem, _ = current_engine
    config = _basic_config()
    pinned_now = bundle.dataset_end_time.to_pydatetime()

    loader_leaky = FL.build_file_data_loader(
        DataLoader, NewsItem, config, bundle, pinned_now, history_leakage_mode="leaky"
    )
    logs = loader_leaky.load_ctr_logs()
    news = loader_leaky.load_embedded_news()
    uid = bundle.users["user_id"].iloc[0]

    before_any_click = bundle.dataset_start_time.to_pydatetime() - timedelta(days=1)
    emb_leaky = loader_leaky.compute_history_embedding(uid, before_any_click, logs, news)
    emb_at_end = loader_leaky.compute_history_embedding(uid, pinned_now, logs, news)
    # leaky 모드에서는 cutoff이 아무리 일러도 pinned_now 기준 전체 히스토리를 쓰므로 두 값이 같아야 한다.
    assert np.allclose(emb_leaky, emb_at_end)
