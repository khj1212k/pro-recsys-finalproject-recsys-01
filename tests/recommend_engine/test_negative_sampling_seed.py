import random
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import numpy as np


def test_sample_negatives_excludes_clicked_ids():
    from src.data.lgbm_dataset import _sample_negatives

    rng = random.Random(42)
    all_news_ids = list(range(20))
    excluded = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}

    sampled = _sample_negatives(rng, all_news_ids, excluded, neg_ratio=5)

    assert len(sampled) == 5
    assert excluded.isdisjoint(sampled)


def test_sample_negatives_is_deterministic_given_same_seed():
    from src.data.lgbm_dataset import _sample_negatives

    all_news_ids = list(range(100))
    excluded = {1, 2, 3}

    result_a = _sample_negatives(random.Random(42), all_news_ids, excluded, neg_ratio=10)
    result_b = _sample_negatives(random.Random(42), all_news_ids, excluded, neg_ratio=10)

    assert result_a == result_b


def test_sample_negatives_differs_across_different_seeds():
    from src.data.lgbm_dataset import _sample_negatives

    all_news_ids = list(range(1000))
    excluded = set()

    result_a = _sample_negatives(random.Random(1), all_news_ids, excluded, neg_ratio=10)
    result_b = _sample_negatives(random.Random(2), all_news_ids, excluded, neg_ratio=10)

    assert result_a != result_b


def test_lgbm_dataset_uses_seeded_local_rng_not_global_random():
    from src.data.lgbm_dataset import LGBMDataset

    ds = LGBMDataset.__new__(LGBMDataset)
    ds._init_rng(seed=123)

    assert isinstance(ds._rng, random.Random)
    # 동일 시드로 만든 별도 Random 인스턴스와 같은 시퀀스를 내야 함 (재현성)
    reference = random.Random(123)
    assert [ds._rng.random() for _ in range(5)] == [reference.random() for _ in range(5)]


def _make_dataset_with_fake_news(news_timestamps):
    """CORRECTION #16 테스트용: DB 연결 없이 LGBMDataset을 만들고, news_dict만
    가짜 NewsItem(timestamp만 있으면 됨)으로 채운다."""
    from src.data.data_loader import NewsItem
    from src.data.lgbm_dataset import LGBMDataset

    ds = LGBMDataset.__new__(LGBMDataset)
    ds.data_loader = MagicMock()
    ds.data_loader.config = {"lightgbm": {"params": {"random_state": 7}}}
    ds._init_rng()
    ds._news_ids_by_time = None
    ds._news_timestamps_by_time = None

    fe = MagicMock()
    fe.news_dict = {
        nid: NewsItem(news_id=nid, title=f"n{nid}", content="", category_ids=[],
                      embedding=np.zeros(2), timestamp=ts)
        for nid, ts in news_timestamps.items()
    }
    ds.fe = fe
    return ds


def test_eligible_news_ids_as_of_excludes_future_news():
    """CORRECTION #16: 클릭 시각 이후에 생성된 뉴스는 negative 후보에서 제외되어야 한다."""
    base = datetime(2026, 1, 1)
    news_timestamps = {
        1: base,                          # 과거 -> eligible
        2: base + timedelta(hours=1),     # 클릭 시각과 동일 -> eligible (<=)
        3: base + timedelta(hours=2),     # 미래 -> 제외
    }
    ds = _make_dataset_with_fake_news(news_timestamps)

    click_time = base + timedelta(hours=1)
    eligible = ds._eligible_news_ids_as_of(click_time)

    assert set(eligible) == {1, 2}


def test_eligible_news_ids_as_of_is_memoized_index_built_once():
    base = datetime(2026, 1, 1)
    news_timestamps = {i: base + timedelta(hours=i) for i in range(10)}
    ds = _make_dataset_with_fake_news(news_timestamps)

    assert ds._news_ids_by_time is None
    ds._eligible_news_ids_as_of(base + timedelta(hours=5))
    assert ds._news_ids_by_time is not None
    cached_ref = ds._news_ids_by_time
    ds._eligible_news_ids_as_of(base + timedelta(hours=9))
    assert ds._news_ids_by_time is cached_ref  # 재구축되지 않고 그대로 재사용됨


def test_create_train_dataset_never_samples_future_news_as_negative():
    """CORRECTION #16 통합 검증: create_train_dataset()이 실제로 미래 뉴스를
    negative로 섞지 않는지, DB 대신 fake DataLoader/FeatureEngineer로 확인."""
    import pandas as pd
    from src.data.data_loader import NewsItem
    from src.data.lgbm_dataset import LGBMDataset

    base = datetime(2026, 1, 1)
    # 유저 1이 base+3h 시점에 클릭. 그 시점에는 뉴스 0~3만 존재(뉴스 4는 미래).
    news_timestamps = {i: base + timedelta(hours=i) for i in range(5)}
    news_dict = {
        nid: NewsItem(news_id=nid, title=f"n{nid}", content="", category_ids=[],
                      embedding=np.zeros(2), timestamp=ts)
        for nid, ts in news_timestamps.items()
    }

    click_time = base + timedelta(hours=3)
    logs_df = pd.DataFrame([{"user_id": 1, "news_letter_id": 3, "timestamp": click_time}])

    fake_loader = MagicMock()
    fake_loader.config = {"lightgbm": {"params": {"random_state": 1}}}
    fake_loader.load_ctr_logs.return_value = logs_df

    fake_fe = MagicMock()
    fake_fe.news_dict = news_dict

    captured = {}

    def fake_create_features(user_ids, news_ids, labels, timestamps):
        captured['news_ids'] = list(news_ids)
        captured['labels'] = list(labels)
        return pd.DataFrame({"user_id": user_ids, "news_id": news_ids, "label": labels, "_timestamp": timestamps})

    fake_fe.create_features.side_effect = fake_create_features

    ds = LGBMDataset(fake_loader, fake_fe, seed=1)
    ds.create_train_dataset(neg_ratio=10)

    negative_news_ids = [nid for nid, lbl in zip(captured['news_ids'], captured['labels']) if lbl == 0]
    assert 4 not in negative_news_ids  # 미래 뉴스(뉴스 4)는 절대 negative가 될 수 없음
    assert 3 not in negative_news_ids  # 유저가 이미 클릭한 뉴스도 제외
