import os
import sys

import numpy as np
import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "ai_workspace"),
)

from evaluation.warmup.cluster import labels_from_groups, membership, pipeline_fn


def test_labels_follow_input_id_order_and_mark_unassigned_as_noise():
    ids = [501, 502, 503, 504, 505]
    labels = labels_from_groups(ids, {0: [503, 501], 1: [505]})
    assert labels.tolist() == [0, -1, 0, -1, 1]


def test_labels_reject_ids_outside_input_or_in_two_groups():
    with pytest.raises(ValueError):
        labels_from_groups([1, 2], {0: [3]})
    with pytest.raises(ValueError):
        labels_from_groups([1, 2], {0: [1], 1: [1]})


def test_membership_keeps_only_ids_sizes_and_press_counts():
    ids = [10, 11, 12, 13]
    final = np.array([0, 0, -1, 1])
    hdb = np.array([4, 4, -1, 4])  # split_v2가 부모 4를 두 그룹으로 나눈 상황
    mem = membership(ids, final, hdb, ["A", "B", "A", "A"])

    assert mem["noise_ids"] == [12]
    assert mem["clusters"] == [
        {"cluster_idx": 0, "size": 2, "raw_news_ids": [10, 11], "n_press": 2, "hdbscan_parent_labels": [4]},
        {"cluster_idx": 1, "size": 1, "raw_news_ids": [13], "n_press": 1, "hdbscan_parent_labels": [4]},
    ]


def test_pipeline_fn_labels_align_with_input_rows_for_stability_resampling():
    rng = np.random.default_rng(0)
    centers = rng.normal(size=(3, 16))
    X = np.vstack([c + 0.01 * rng.normal(size=(8, 16)) for c in centers])
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    titles = [f"주제{k // 8} 기사" for k in range(len(X))]

    fn = pipeline_fn(titles, min_cluster_size=3, min_samples=2)
    rows = np.array([3, 9, 17, 0, 10, 18, 5, 12, 20, 1, 11, 22])
    X_aug = np.hstack([rows[:, None].astype(np.float64), X[rows]])
    labels = fn(X_aug)

    assert labels.shape == (len(rows),)
    # 같은 중심에서 뽑힌 행끼리 같은 라벨이어야 한다(행 번호 → 제목 매핑이 어긋나지 않음)
    by_center = {}
    for r, lab in zip(rows, labels):
        by_center.setdefault(r // 8, set()).add(int(lab))
    assert all(len(s) == 1 and -1 not in s for s in by_center.values())
    assert len({next(iter(s)) for s in by_center.values()}) == 3
