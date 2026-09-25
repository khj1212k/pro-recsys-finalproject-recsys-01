import numpy as np
import pandas as pd
from datetime import datetime, timedelta


def _synthetic_dataset_with_timestamp(n_users=8, events_per_user=2, negs_per_event=3, n_features=4, seed=0):
    """유저당 events_per_user개의 서로 다른 클릭 시각(event)을 만들고, 각 이벤트마다
    positive 1개 + negs_per_event개의 negative가 '동일한 timestamp'를 공유하도록
    합성 데이터를 만든다. lgbm_dataset.py가 실제로 만드는 형태를 재현한 것으로,
    group_key='user_timestamp'(기본값)의 그룹 크기가 (negs_per_event + 1)이 되어야 함."""
    rng = np.random.RandomState(seed)
    base = datetime(2026, 1, 1)
    rows = []
    for uid in range(n_users):
        for e in range(events_per_user):
            ts = base + timedelta(hours=uid * 100 + e * 5)
            for j in range(negs_per_event + 1):
                row = {"user_id": uid, "news_id": f"{uid}-{e}-{j}", "label": 1 if j == 0 else 0, "_timestamp": ts}
                for f in range(n_features):
                    row[f"f{f}"] = rng.rand()
                rows.append(row)
    df = pd.DataFrame(rows).sample(frac=1.0, random_state=seed).reset_index(drop=True)
    return df


def _synthetic_dataset_no_timestamp(n_users=3, rows_per_user=4, n_features=4, seed=0):
    rng = np.random.RandomState(seed)
    rows = []
    for uid in range(n_users):
        for j in range(rows_per_user):
            row = {"user_id": uid, "news_id": j, "label": 1 if j == 0 else 0}
            for f in range(n_features):
                row[f"f{f}"] = rng.rand()
            rows.append(row)
    return pd.DataFrame(rows).sample(frac=1.0, random_state=seed).reset_index(drop=True)


def test_build_groups_default_groups_by_user_and_click_timestamp():
    """CORRECTION #5: 기본 group_key='user_timestamp'는 (user_id, 클릭 시각) 단위로
    묶여야 한다 - lgbm_dataset.py의 positive+negative 묶음과 일치."""
    from src.models.lgbm_ranker import LGBMRanker

    df = _synthetic_dataset_with_timestamp(n_users=3, events_per_user=2, negs_per_event=3)
    ranker = LGBMRanker()
    assert ranker.group_key == "user_timestamp"

    sorted_df, groups = ranker._build_groups(df)

    assert sum(groups) == len(df)
    # 유저 3명 x 이벤트 2개 = 6개 그룹, 각 그룹은 positive 1 + negative 3 = 4행
    assert list(groups) == [4] * 6


def test_build_groups_user_id_group_key_groups_by_user_only():
    """레거시 group_key='user_id'는 _timestamp 없이도 동작하고, 유저 전체를 한 그룹으로 묶는다."""
    from src.models.lgbm_ranker import LGBMRanker

    df = _synthetic_dataset_no_timestamp(n_users=3, rows_per_user=4)
    ranker = LGBMRanker(group_key="user_id")
    sorted_df, groups = ranker._build_groups(df)

    assert list(sorted_df["user_id"]) == sorted(sorted_df["user_id"])
    assert sum(groups) == len(df)
    assert list(groups) == [4, 4, 4]


def test_build_groups_requires_timestamp_column_for_user_timestamp_key():
    from src.models.lgbm_ranker import LGBMRanker

    df = _synthetic_dataset_no_timestamp(n_users=2, rows_per_user=3)
    ranker = LGBMRanker()  # 기본 group_key='user_timestamp'
    try:
        ranker._build_groups(df)
        assert False, "ValueError가 발생해야 한다"
    except ValueError:
        pass


def test_lgbm_ranker_trains_with_lambdarank_objective():
    from src.models.lgbm_ranker import LGBMRanker

    ranker = LGBMRanker()
    assert ranker.params["objective"] == "lambdarank"
    assert "group" not in ranker.params  # group은 params가 아니라 Dataset에 전달됨

    train_df = _synthetic_dataset_with_timestamp(n_users=10, events_per_user=3, negs_per_event=5, seed=1)
    valid_df = _synthetic_dataset_with_timestamp(n_users=4, events_per_user=3, negs_per_event=5, seed=2)

    ranker.num_boost_round = 5  # 테스트 속도를 위해 축소
    ranker.early_stopping_rounds = 3
    ranker.train(train_df, valid_df=valid_df)

    assert ranker.model is not None

    inference_df = _synthetic_dataset_with_timestamp(
        n_users=2, events_per_user=2, negs_per_event=4, seed=3
    ).drop(columns=["label"])
    scored = ranker.predict(inference_df)
    assert "score" in scored.columns
    assert len(scored) == len(inference_df)
    # _timestamp는 피처로 쓰이지 않아야 함(그룹핑에만 사용) - predict 결과 컬럼엔 남아있어도 무방
    assert "_timestamp" not in ranker.model.feature_name()
