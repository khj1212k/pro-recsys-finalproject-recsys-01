import numpy as np
import pytest

from recsys_core.events import EventIndex, expand_ranges


def _index():
    # user 1: t=10(a=100), 20(a=101), 30(a=102) / user 2: t=15(a=200)
    return EventIndex(key=[1, 2, 1, 1], time=[20, 15, 10, 30], item=[101, 200, 100, 102])


def test_bounds_are_half_open_so_event_at_cutoff_is_excluded():
    idx = _index()
    assert idx.count([1], 0, 20).tolist() == [1]     # t=20 이벤트는 [.,20)에서 제외
    assert idx.count([1], 20, 31).tolist() == [2]    # t_lo=20은 포함
    assert idx.count([1], 0, 10).tolist() == [0]


def test_bounds_respect_key_blocks():
    idx = _index()
    lo, hi = idx.bounds([1, 2, 3], [0, 0, 0], [100, 100, 100])
    assert (hi - lo).tolist() == [3, 1, 0]
    rows, pos = expand_ranges(lo, hi)
    assert rows.tolist() == [0, 0, 0, 1]
    assert idx.item[pos].tolist() == [100, 101, 102, 200]


def test_negative_key_means_no_events():
    idx = _index()
    assert idx.count([-1], 0, 100).tolist() == [0]


def test_query_times_before_first_event_are_safe():
    idx = _index()
    assert idx.count([1], -1000, 5).tolist() == [0]
    assert idx.count([1], -1000, 11).tolist() == [1]


def test_dedupe_removes_exact_duplicates_only():
    idx = EventIndex(key=[1, 1, 1], time=[5, 5, 5], item=[7, 7, 8], dedupe=True)
    assert len(idx) == 2


def test_last_time_before_is_strict():
    idx = _index()
    out = idx.last_time_before([1, 1, 1, 2, 9], [10, 11, 100, 15, 100])
    assert out.tolist() == [-1, 10, 30, -1, -1]


def test_rejects_out_of_range_keys():
    with pytest.raises(ValueError):
        EventIndex(key=[-1], time=[0], item=[0])


def test_expand_ranges_empty():
    rows, pos = expand_ranges(np.array([3, 5]), np.array([3, 5]))
    assert len(rows) == 0 and len(pos) == 0
