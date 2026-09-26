"""protocol.py 테스트: 고정 answer_start 계산, cold/warm 분류, seen-item filtering,
후보 풀로 정답 제한하기 (v2 요구사항 1, 3, 7)."""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "evaluation" / "recsys" / "team_repro"))

import protocol as PR  # noqa: E402


def _ts(s):
    return pd.Timestamp(s)


def _clicks_df(rows):
    """rows: [(user_id, news_letter_id, timestamp_str), ...]"""
    return pd.DataFrame(
        [{"user_id": u, "news_letter_id": n, "timestamp": _ts(t)} for u, n, t in rows]
    )


def test_compute_fixed_answer_start_leaves_expected_tail_ratio():
    # 10건을 1분 간격으로 배치하면, val_ratio=0.2일 때 index 8(0-based)이 경계.
    rows = [(1, i, f"2026-01-01T00:{i:02d}:00") for i in range(10)]
    clicks = _clicks_df(rows)
    boundary = PR.compute_fixed_answer_start(clicks, val_ratio=0.2)
    assert boundary == _ts("2026-01-01T00:08:00")


def test_compute_fixed_answer_start_empty_raises():
    with pytest.raises(ValueError):
        PR.compute_fixed_answer_start(pd.DataFrame(columns=["timestamp"]))


def test_cold_warm_split_classifies_users_by_pre_cutoff_clicks():
    rows = [
        (1, 10, "2026-01-01T00:00:00"),  # user1: cutoff 이전 클릭 있음 -> warm
        (2, 11, "2026-01-01T00:10:00"),  # user2: cutoff 이후만 있음 -> cold
    ]
    clicks = _clicks_df(rows)
    cutoff = _ts("2026-01-01T00:05:00")
    cold, warm = PR.cold_warm_split([1, 2, 3], clicks, cutoff)
    assert warm == {1}
    assert cold == {2, 3}  # user3: 클릭 자체가 없음 -> cold


def test_seen_items_by_user_only_counts_pre_cutoff_clicks():
    rows = [
        (1, 10, "2026-01-01T00:00:00"),
        (1, 11, "2026-01-01T00:10:00"),  # cutoff 이후 -> seen에 포함되면 안 됨
    ]
    clicks = _clicks_df(rows)
    seen = PR.seen_items_by_user(clicks, _ts("2026-01-01T00:05:00"))
    assert seen[1] == {10}


def test_filter_seen_removes_already_clicked_items_without_backfilling():
    recs = {1: [10, 11, 12, 13]}
    seen = {1: {11, 13}}
    out = PR.filter_seen(recs, seen)
    assert out[1] == [10, 12]  # 자리 채움 없이 그대로 제거


def test_restrict_ground_truth_to_pool_drops_answers_outside_pool_and_empty_users():
    gt = {1: {10, 20}, 2: {30}}
    pool = [10, 40]
    out = PR.restrict_ground_truth_to_pool(gt, pool)
    assert out == {1: {10}}  # user2의 유일한 정답(30)이 풀 밖 -> 유저 자체가 빠짐


def test_restrict_ground_truth_to_pool_none_is_noop():
    gt = {1: {10, 20}}
    assert PR.restrict_ground_truth_to_pool(gt, None) == gt


def test_split_metrics_by_group_averages_only_selected_users():
    per_user = {1: {"mrr": 1.0}, 2: {"mrr": 0.0}, 3: {"mrr": 0.5}}
    out = PR.split_metrics_by_group(per_user, {1, 2})
    assert out["mrr"] == pytest.approx(0.5)
    assert out["num_users"] == 2


def test_split_metrics_by_group_empty_group_returns_empty_dict():
    assert PR.split_metrics_by_group({1: {"mrr": 1.0}}, set()) == {}


def test_seen_share_in_topk_computes_fraction_of_already_clicked():
    recs = {1: [10, 11, 12], 2: [20, 21, 22]}
    seen = {1: {10}, 2: {20, 21}}
    share = PR.seen_share_in_topk(recs, seen, k=3)
    assert share == pytest.approx(3 / 6)


def test_shown_items_by_user_counts_unclicked_exposures_before_cutoff():
    """unshown-only 필터: 경계 이전 노출은 클릭 여부와 무관하게 '이미 보여준 것'이다."""
    logs = pd.DataFrame(
        [
            {"user_id": 1, "news_letter_id": 10, "timestamp": _ts("2026-01-01T00:00:00"), "is_clicked": 1},
            {"user_id": 1, "news_letter_id": 11, "timestamp": _ts("2026-01-01T00:01:00"), "is_clicked": 0},
            {"user_id": 1, "news_letter_id": 12, "timestamp": _ts("2026-01-01T00:10:00"), "is_clicked": 0},
        ]
    )
    shown = PR.shown_items_by_user(logs, _ts("2026-01-01T00:05:00"))
    assert shown[1] == {10, 11}
    seen = PR.seen_items_by_user(logs[logs["is_clicked"] == 1], _ts("2026-01-01T00:05:00"))
    assert seen[1] == {10}
