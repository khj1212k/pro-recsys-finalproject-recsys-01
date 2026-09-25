# src/data/time_split.py
"""시간 정렬된 학습 데이터를 LambdaRank 그룹(쿼리) 경계를 자르지 않고
Train/Valid로 분할하기 위한 순수 함수 모음.

main_lgbm.py / scripts/tune_hyperparams.py / scripts/ablation_objective_comparison.py가
공유한다.

CORRECTION #5: lgbm_dataset.py는 각 positive 로그와 그 negative 샘플들에 동일한
클릭 시각(_timestamp)을 부여한다. LambdaRank의 그룹(쿼리)을 이 (user_id, timestamp)
조합으로 잡으면, 같은 그룹에 속한 행이 train/valid 양쪽에 걸쳐 나뉘어서는 안 된다 -
걸치면 해당 그룹은 어느 쪽에서도 완전한 쿼리로 학습/평가되지 못한다.
"""
import pandas as pd


def compute_group_ids(df: pd.DataFrame, group_key: str) -> pd.Series:
    """행마다 LambdaRank 그룹 식별자를 계산한다.

    - 'user_id': 유저 전체 이력을 하나의 쿼리 그룹으로 묶는다(레거시 동작).
      학습 기간 전체가 한 쿼리가 되어버려 시간순 분할과 함께 쓰면 그룹이
      분할 경계에서 거의 항상 잘린다.
    - 'user_timestamp'(기본값): (user_id, 클릭 시각) 조합. lgbm_dataset.py가
      만드는 '한 번의 노출(positive + sampled negatives)' 단위와 정확히 일치한다.
    """
    if group_key == "user_id":
        return df["user_id"].astype(str)
    if group_key == "user_timestamp":
        if "_timestamp" not in df.columns:
            raise ValueError("group_key='user_timestamp'는 '_timestamp' 컬럼이 필요합니다.")
        return df["user_id"].astype(str) + "|" + df["_timestamp"].astype(str)
    raise ValueError(f"알 수 없는 ranking.group_key: {group_key!r}")


def find_group_safe_split_index(group_ids: pd.Series, target_idx: int) -> int:
    """target_idx에 가장 가까우면서 그룹을 자르지 않는 분할 지점을 찾는다.

    group_ids는 이미 정렬되어 같은 그룹의 행이 항상 연속되어 있다고 가정한다.
    target_idx가 어떤 그룹의 중간이면, 그 그룹 전체가 train 또는 valid 한쪽에만
    속하도록 더 가까운 경계(왼쪽/오른쪽) 쪽으로 이동한다.
    """
    n = len(group_ids)
    target_idx = max(0, min(target_idx, n))
    if target_idx in (0, n):
        return target_idx

    values = group_ids.to_numpy()
    if values[target_idx - 1] != values[target_idx]:
        return target_idx  # 이미 그룹 경계

    left = target_idx
    while left > 0 and values[left - 1] == values[target_idx]:
        left -= 1
    right = target_idx
    while right < n and values[right] == values[target_idx]:
        right += 1

    return left if (target_idx - left) <= (right - target_idx) else right


def time_ordered_group_safe_split(df: pd.DataFrame, val_ratio: float, group_key: str = "user_timestamp"):
    """'_timestamp' 기준 시간순 정렬 후, len(df)*(1-val_ratio) 근방에서 그룹을
    자르지 않는 지점으로 Train/Valid를 나눈다. 반환되는 두 DataFrame 모두
    시간순으로 정렬된 상태다.
    """
    if "_timestamp" not in df.columns:
        raise ValueError("time_ordered_group_safe_split은 '_timestamp' 컬럼이 필요합니다.")

    sorted_df = df.sort_values(by="_timestamp", kind="mergesort").reset_index(drop=True)
    target_idx = int(len(sorted_df) * (1 - val_ratio))
    group_ids = compute_group_ids(sorted_df, group_key)
    split_idx = find_group_safe_split_index(group_ids, target_idx)

    train_df = sorted_df.iloc[:split_idx].copy()
    valid_df = sorted_df.iloc[split_idx:].copy()
    return train_df, valid_df
