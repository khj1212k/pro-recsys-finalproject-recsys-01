import time
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest
import pandas as pd


def _make_loader(config):
    from src.data.data_loader import DataLoader
    loader = DataLoader.__new__(DataLoader)  # DB 연결 없이 인스턴스 생성 (테스트 전용)
    loader.config = config
    loader._news_dict = None
    loader._user_profiles = None
    return loader


def _config():
    return {
        "database": {"user": "u", "password": "p", "host": "h", "port": 5432, "dbname": "d"},
        "time_decay": {"news_half_life_days": 7, "min_weight": 0.01},
    }


def _naive_compute_history_embedding(config, user_id, cutoff_time, logs_df, news_dict):
    """수정 전(iterrows + 필터) 구현을 그대로 재현한 참조 구현.
    새 구현(사전 정렬 + 메모이즈)과의 값 동치성 검증용."""
    news_half_life = config['time_decay']['news_half_life_days']
    min_weight = config['time_decay']['min_weight']

    hist_emb = np.zeros(1024, dtype=np.float32)
    if logs_df.empty:
        return hist_emb

    user_logs = logs_df[
        (logs_df['user_id'] == user_id) & (logs_df['timestamp'] < cutoff_time)
    ]
    if user_logs.empty:
        return hist_emb

    vectors, weights = [], []
    for _, log in user_logs.iterrows():
        nid = int(log['news_letter_id'])
        if nid not in news_dict:
            continue
        vec = news_dict[nid].embedding
        days_ago = (cutoff_time - log['timestamp']).days
        w = pow(0.5, days_ago / news_half_life)
        w = max(w, min_weight)
        vectors.append(vec)
        weights.append(w)

    if vectors:
        hist_emb = np.average(vectors, axis=0, weights=weights)
    return hist_emb


def test_compute_history_embedding_excludes_future_logs():
    from src.data.data_loader import NewsItem

    loader = _make_loader(_config())

    now = datetime(2026, 1, 28, 0, 0, 0)
    cutoff = datetime(2026, 1, 10, 0, 0, 0)  # 학습 데이터의 '그 시점'

    news_dict = {
        1: NewsItem(news_id=1, title="A", content="", category_ids=[], embedding=np.array([1.0, 0.0]), timestamp=now - timedelta(days=20)),
        2: NewsItem(news_id=2, title="B", content="", category_ids=[], embedding=np.array([0.0, 1.0]), timestamp=now - timedelta(days=2)),
    }

    logs_df = pd.DataFrame([
        {"user_id": 1, "news_letter_id": 1, "timestamp": cutoff - timedelta(days=1)},  # cutoff 이전 (과거) -> 포함되어야 함
        {"user_id": 1, "news_letter_id": 2, "timestamp": cutoff + timedelta(days=5)},  # cutoff 이후 (미래) -> 제외되어야 함
    ])

    result = loader.compute_history_embedding(
        user_id=1, cutoff_time=cutoff, logs_df=logs_df, news_dict=news_dict
    )

    # 미래 로그(뉴스 2, 임베딩 [0,1])가 섞이지 않고 과거 로그(뉴스 1, 임베딩 [1,0])만 반영되어야 함
    np.testing.assert_allclose(result, np.array([1.0, 0.0]), atol=1e-6)


def test_compute_history_embedding_empty_logs_returns_zero_vector():
    loader = _make_loader(_config())
    result = loader.compute_history_embedding(
        user_id=999, cutoff_time=datetime(2026, 1, 10), logs_df=pd.DataFrame(columns=["user_id", "news_letter_id", "timestamp"]), news_dict={}
    )
    assert np.all(result == 0)
    assert result.shape == (1024,)


def test_create_features_uses_point_in_time_history_not_global_profile():
    from src.data.data_loader import NewsItem, UserProfile
    from src.features.feature_engineer import FeatureEngineer

    loader = _make_loader(_config())

    now = datetime(2026, 1, 28)
    early_time = datetime(2026, 1, 5)  # 학습 데이터 중 '이른 시점' row

    news_dict = {
        10: NewsItem(news_id=10, title="early-news", content="", category_ids=[], embedding=np.array([1.0, 0.0]), timestamp=now - timedelta(days=25)),
        20: NewsItem(news_id=20, title="future-news", content="", category_ids=[], embedding=np.array([0.0, 1.0]), timestamp=now - timedelta(days=1)),
    }
    loader._news_dict = news_dict

    # 유저 1은 1/3(이른 시점)에 뉴스10을, 1/25(늦은 시점=미래)에 뉴스20을 클릭
    logs_df = pd.DataFrame([
        {"user_id": 1, "news_letter_id": 10, "timestamp": datetime(2026, 1, 3)},
        {"user_id": 1, "news_letter_id": 20, "timestamp": datetime(2026, 1, 25)},
    ])

    # build_user_profiles가 호출하는 DB 로더들을 우회하기 위해 FeatureEngineer를 직접 구성
    fe = FeatureEngineer.__new__(FeatureEngineer)
    fe.data_loader = loader
    fe.config = loader.config
    fe.news_dict = news_dict
    fe.logs_df = logs_df
    fe.user_profiles = {
        1: UserProfile(user_id=1, onboarding_categories=[],
                        history_embedding=np.array([0.3, 0.7]))  # '현재 시점' 글로벌 스냅샷 (누출된 값이라고 가정)
    }

    # 이른 시점(early_time) row에 대해 feature 생성 -> 미래 클릭(뉴스20)이 반영되면 안 됨
    df = fe.create_features(
        user_ids=[1], news_ids=[10], labels=[1], timestamps=[early_time]
    )

    # 뉴스10 자기 자신과의 코사인 유사도는 1.0에 가까워야 함 (이른 시점엔 뉴스10 클릭만 유효)
    # 만약 글로벌 프로필([0.3, 0.7])을 그대로 썼다면 이 값과 달랐을 것
    assert df.iloc[0]['history_cosine_similarity'] > 0.99


def _synthetic_logs_and_news(n_users=300, n_news=200, seed=0):
    rng = np.random.RandomState(seed)
    base = datetime(2026, 1, 1)

    from src.data.data_loader import NewsItem
    news_dict = {}
    for nid in range(n_news):
        news_dict[nid] = NewsItem(
            news_id=nid, title=f"n{nid}", content="", category_ids=[],
            embedding=rng.rand(1024).astype(np.float32),
            timestamp=base + timedelta(hours=int(rng.randint(0, 24 * 20))),
        )

    rows = []
    for uid in range(n_users):
        n_clicks = rng.randint(3, 8)
        clicked_news = rng.choice(n_news, size=n_clicks, replace=False)
        for nid in clicked_news:
            rows.append({
                "user_id": uid,
                "news_letter_id": int(nid),
                "timestamp": base + timedelta(hours=int(rng.randint(0, 24 * 25))),
            })
    logs_df = pd.DataFrame(rows)
    return logs_df, news_dict


def test_compute_history_embedding_is_memoized_per_user_and_cutoff():
    """CORRECTION #4: (user_id, cutoff_time) 조합당 실제 계산은 1회만 수행되어야 한다.
    create_inference_dataset은 유저 1명당 뉴스 전체 건수만큼 동일한 (uid, eval_timestamp)
    쌍으로 compute_history_embedding을 호출하므로, 메모이즈가 없으면 O(users x news)로
    재계산되고 있으면 O(unique (user_id, cutoff) 조합)만 계산된다."""
    loader = _make_loader(_config())
    logs_df, news_dict = _synthetic_logs_and_news(n_users=5, n_news=20, seed=1)

    eval_ts = datetime(2026, 2, 1)
    user_ids = list(range(5))
    n_news_per_user = 20  # 추론 시나리오: 유저당 전체 뉴스에 대해 반복 호출

    from src.data.data_loader import DataLoader
    with patch.object(
        DataLoader, "_compute_history_embedding_uncached",
        wraps=loader._compute_history_embedding_uncached,
    ) as spy:
        for uid in user_ids:
            for _ in range(n_news_per_user):
                loader.compute_history_embedding(
                    user_id=uid, cutoff_time=eval_ts, logs_df=logs_df, news_dict=news_dict
                )

        # unique (user_id, cutoff) 조합 수 == len(user_ids) (cutoff가 전부 동일하므로)
        assert spy.call_count == len(user_ids)


def test_compute_history_embedding_matches_naive_unmemoized_values():
    loader = _make_loader(_config())
    logs_df, news_dict = _synthetic_logs_and_news(n_users=15, n_news=40, seed=2)

    cutoffs = [datetime(2026, 1, 10), datetime(2026, 1, 15), datetime(2026, 1, 20)]
    for uid in range(15):
        for cutoff in cutoffs:
            fast = loader.compute_history_embedding(
                user_id=uid, cutoff_time=cutoff, logs_df=logs_df, news_dict=news_dict
            )
            naive = _naive_compute_history_embedding(
                loader.config, user_id=uid, cutoff_time=cutoff, logs_df=logs_df, news_dict=news_dict
            )
            np.testing.assert_allclose(fast, naive, atol=1e-5)


@pytest.mark.benchmark
def test_history_embedding_memoized_inference_is_faster_than_naive_benchmark(capsys):
    """CORRECTION #4 벤치마크: 300 유저 x 200 뉴스 추론 시나리오에서
    사전 분할 + 메모이즈 구현이 naive(수정 전) 구현보다 유의미하게 빨라야 한다."""
    n_users, n_news = 300, 200
    logs_df, news_dict = _synthetic_logs_and_news(n_users=n_users, n_news=n_news, seed=3)
    eval_ts = datetime(2026, 2, 5)

    naive_loader = _make_loader(_config())
    start = time.perf_counter()
    for uid in range(n_users):
        for _ in range(n_news):
            _naive_compute_history_embedding(
                naive_loader.config, user_id=uid, cutoff_time=eval_ts, logs_df=logs_df, news_dict=news_dict
            )
    naive_seconds = time.perf_counter() - start

    fast_loader = _make_loader(_config())
    start = time.perf_counter()
    for uid in range(n_users):
        for _ in range(n_news):
            fast_loader.compute_history_embedding(
                user_id=uid, cutoff_time=eval_ts, logs_df=logs_df, news_dict=news_dict
            )
    fast_seconds = time.perf_counter() - start

    print(f"\n[benchmark] history_embedding: naive(unmemoized)={naive_seconds:.4f}s, "
          f"memoized={fast_seconds:.4f}s, speedup={naive_seconds / max(fast_seconds, 1e-9):.1f}x "
          f"(n_users={n_users}, n_news={n_news})")

    assert fast_seconds < naive_seconds
