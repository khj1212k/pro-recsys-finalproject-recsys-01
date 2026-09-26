"""LightGBM에 들어가는 피처 열의 계약.

팀 원본은 user_age_band/user_gender를 피처로 넣었지만 DataLoader.build_user_profiles가
두 값을 항상 0으로 채웠다(DB에서 읽지 않음) - 모델이 어떤 분할에도 쓸 수 없는 상수 열이다.
LightGBM 4.7은 상수 열을 학습 전에 걸러내므로 열을 빼도 예측은 비트 단위로 같다
(커밋 본문에 3시드 x 열 위치 3가지 비교 결과). 모델 입력 스키마를 명시적으로 고정해
DB가 채우지 않는 값이 다시 피처로 섞여 들어오지 않게 한다.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# LGBMRanker.train()/predict()가 피처로 쓰지 않고 떼어내는 열
NON_FEATURE_COLUMNS = {"user_id", "news_id", "label", "_timestamp"}

EXPECTED_FEATURES = {
    "hours_since_published",
    "is_fresh_24h",
    "is_fresh_7d",
    "history_cosine_similarity",
    "category_match_count",
    "is_category_match",
    "news_category_repr",
    "user_onboarding_cnt",
}


def _feature_engineer():
    from src.data.data_loader import DataLoader, NewsItem, UserProfile
    from src.features.feature_engineer import FeatureEngineer

    loader = DataLoader.__new__(DataLoader)  # DB 연결 없이
    loader.config = {"time_decay": {"news_half_life_days": 7, "min_weight": 0.01}}
    loader._news_dict = None
    loader._user_profiles = None

    now = datetime(2026, 1, 28)
    news_dict = {
        10: NewsItem(news_id=10, title="a", content="", category_ids=[1],
                     embedding=np.array([1.0, 0.0]), timestamp=now - timedelta(hours=3)),
        20: NewsItem(news_id=20, title="b", content="", category_ids=[3],
                     embedding=np.array([0.0, 1.0]), timestamp=now - timedelta(days=3)),
    }
    fe = FeatureEngineer.__new__(FeatureEngineer)
    fe.data_loader = loader
    fe.config = loader.config
    fe.news_dict = news_dict
    fe.logs_df = pd.DataFrame([{"user_id": 1, "news_letter_id": 10, "timestamp": now - timedelta(days=1)}])
    fe.user_profiles = {
        1: UserProfile(user_id=1, onboarding_categories=[1, 2], history_embedding=np.array([1.0, 0.0])),
        2: UserProfile(user_id=2, onboarding_categories=[3], history_embedding=np.array([0.0, 1.0])),
    }
    return fe, now


def _feature_columns(df):
    return set(df.columns) - NON_FEATURE_COLUMNS


def test_training_features_match_the_model_input_contract():
    fe, now = _feature_engineer()

    df = fe.create_features(user_ids=[1, 1, 2, 2], news_ids=[10, 20, 10, 20],
                            labels=[1, 0, 0, 1], timestamps=[now] * 4)

    assert _feature_columns(df) == EXPECTED_FEATURES


def test_inference_features_match_the_training_features():
    fe, now = _feature_engineer()

    train_df = fe.create_features(user_ids=[1, 2], news_ids=[10, 20], labels=[1, 0], timestamps=[now] * 2)
    infer_df = fe.create_features(user_ids=[1, 2], news_ids=[10, 20])

    assert _feature_columns(infer_df) == _feature_columns(train_df)
