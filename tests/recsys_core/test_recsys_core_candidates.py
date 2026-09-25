import numpy as np
import pytest

from recsys_core.candidates import rank_within_groups, source_flags


def test_source_flags_mark_top_k_of_each_source_per_request():
    ptr = np.array([0, 4, 7])
    sources = {
        "pop": np.array([5.0, 1.0, 3.0, 0.0, 2.0, 9.0, 1.0]),
        "recency": np.array([0.0, 4.0, 1.0, 3.0, 7.0, 0.0, 8.0]),
    }
    f = source_flags(sources, ptr, k=2)
    assert f["src_pop"].tolist() == [1, 0, 1, 0, 1, 1, 0]
    assert f["src_recency"].tolist() == [0, 1, 0, 1, 1, 0, 1]
    assert f["src_count"].tolist() == [1, 1, 1, 1, 2, 1, 1]


def test_source_flags_keep_all_candidates_of_short_requests():
    f = source_flags({"a": np.array([0.3, 0.1])}, np.array([0, 2]), k=5)
    assert f["src_a"].tolist() == [1, 1]


def test_rank_ties_are_broken_by_seed_not_by_input_order():
    ptr = np.array([0, 50])
    firsts = {int(np.flatnonzero(rank_within_groups(np.zeros(50), ptr, seed=s) == 0)[0]) for s in range(20)}
    assert len(firsts) > 5


def test_source_flags_reject_length_mismatch():
    with pytest.raises(ValueError):
        source_flags({"a": np.zeros(3)}, np.array([0, 4]), k=1)
