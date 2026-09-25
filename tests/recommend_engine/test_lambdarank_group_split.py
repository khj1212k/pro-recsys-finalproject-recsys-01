import numpy as np
import pandas as pd
from datetime import datetime, timedelta


def _make_grouped_df(n_groups=20, group_size=4, seed=0):
    """n_groups개의 (user_id, timestamp) 그룹을 만들고, 각 그룹은 group_size개의
    연속된 행(positive 1 + negative group_size-1)으로 구성한다. 그룹 순서를 섞어도
    time_ordered_group_safe_split이 내부에서 _timestamp로 다시 정렬한다."""
    rng = np.random.RandomState(seed)
    base = datetime(2026, 1, 1)
    rows = []
    for g in range(n_groups):
        uid = g % 5
        ts = base + timedelta(minutes=g)  # 그룹마다 고유한 시각 -> 그룹 경계가 뚜렷함
        for j in range(group_size):
            rows.append({
                "user_id": uid,
                "news_id": f"{g}-{j}",
                "label": 1 if j == 0 else 0,
                "_timestamp": ts,
                "f0": rng.rand(),
            })
    df = pd.DataFrame(rows).sample(frac=1.0, random_state=seed).reset_index(drop=True)
    return df


def test_compute_group_ids_user_timestamp_matches_click_event():
    from src.data.time_split import compute_group_ids

    df = _make_grouped_df(n_groups=6, group_size=3)
    group_ids = compute_group_ids(df, "user_timestamp")
    # (user_id, _timestamp) 조합이 같은 행은 같은 group_id를 가져야 함
    check = df.assign(_gid=group_ids.values)
    for (_uid, _ts), sub in check.groupby(["user_id", "_timestamp"]):
        assert sub["_gid"].nunique() == 1


def test_compute_group_ids_unknown_key_raises():
    from src.data.time_split import compute_group_ids
    df = pd.DataFrame({"user_id": [1, 2]})
    try:
        compute_group_ids(df, "bogus_key")
        assert False, "ValueError가 발생해야 한다"
    except ValueError:
        pass


def test_find_group_safe_split_index_moves_off_group_middle():
    from src.data.time_split import find_group_safe_split_index

    # group_ids: [A,A,A,A, B,B,B,B, C,C,C,C]  (그룹 크기 4씩, 총 12행)
    group_ids = pd.Series(["A"] * 4 + ["B"] * 4 + ["C"] * 4)

    # target=6은 그룹 B(인덱스 4~7)의 중간 -> 더 가까운 경계인 4 또는 8로 이동해야 함
    split_idx = find_group_safe_split_index(group_ids, target_idx=6)
    assert split_idx in (4, 8)
    # 경계라면 반드시 다른 그룹 값이어야 함(그룹이 안 잘림)
    if 0 < split_idx < len(group_ids):
        assert group_ids.iloc[split_idx - 1] != group_ids.iloc[split_idx]


def test_find_group_safe_split_index_already_on_boundary_is_unchanged():
    from src.data.time_split import find_group_safe_split_index
    group_ids = pd.Series(["A"] * 4 + ["B"] * 4)
    assert find_group_safe_split_index(group_ids, target_idx=4) == 4


def test_find_group_safe_split_index_handles_edges():
    from src.data.time_split import find_group_safe_split_index
    group_ids = pd.Series(["A"] * 5)
    assert find_group_safe_split_index(group_ids, target_idx=0) == 0
    assert find_group_safe_split_index(group_ids, target_idx=5) == 5
    # 전부 한 그룹이면 중간 지점도 그룹 경계 쪽(0 또는 n)으로 밀려나야 함
    assert find_group_safe_split_index(group_ids, target_idx=2) in (0, 5)


def test_time_ordered_group_safe_split_never_cuts_a_group():
    """CORRECTION #5 핵심 요구사항: 분할 결과 어떤 (user_id, timestamp) 그룹도
    train과 valid 양쪽에 걸쳐 나뉘어 있으면 안 된다."""
    from src.data.time_split import time_ordered_group_safe_split

    df = _make_grouped_df(n_groups=37, group_size=5, seed=7)  # 홀수 개 그룹 -> 정확히 안 나뉘는 비율 유도
    train_df, valid_df = time_ordered_group_safe_split(df, val_ratio=0.2, group_key="user_timestamp")

    assert len(train_df) + len(valid_df) == len(df)

    train_groups = set(zip(train_df["user_id"], train_df["_timestamp"]))
    valid_groups = set(zip(valid_df["user_id"], valid_df["_timestamp"]))
    assert train_groups.isdisjoint(valid_groups)

    # 각 그룹이 온전히 한쪽에만 존재하는지 (전체 그룹 크기가 유지되는지)로 재검증
    for uid, ts in train_groups:
        full_count = len(df[(df["user_id"] == uid) & (df["_timestamp"] == ts)])
        train_count = len(train_df[(train_df["user_id"] == uid) & (train_df["_timestamp"] == ts)])
        assert train_count == full_count


def test_time_ordered_group_safe_split_is_roughly_time_ordered():
    from src.data.time_split import time_ordered_group_safe_split

    df = _make_grouped_df(n_groups=30, group_size=4, seed=3)
    train_df, valid_df = time_ordered_group_safe_split(df, val_ratio=0.2, group_key="user_timestamp")

    # 그룹을 자르지 않기 위해 정확히 80/20이 아닐 수 있으나, valid는 train보다 늦은 시점이어야 함
    assert train_df["_timestamp"].max() <= valid_df["_timestamp"].min()
