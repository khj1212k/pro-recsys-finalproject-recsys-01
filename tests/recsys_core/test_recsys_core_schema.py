"""ranker v2 피처 스키마(recsys_core/schema.py): 열 순서·dtype·결측 표기, 지문, 후보 구성 기본값."""
import numpy as np
import pytest

from recsys_core import SERVING_CANDIDATE_SPEC, CandidateSpec, feature_groups, round_robin_union, schema


def test_ranker_v2_columns_are_the_training_order_and_come_from_the_feature_groups_plus_extras():
    produced = {name for group in schema.ALL_GROUPS for name in feature_groups()[group]}

    assert len(schema.RANKER_V2_FEATURES) == len(set(schema.RANKER_V2_FEATURES)) == 22
    assert set(schema.RANKER_V2_FEATURES) == produced | set(schema.EXTRA_COLUMNS)
    assert schema.RANKER_V2_FEATURES[:3] == ("hours_since_pub", "is_fresh_24h", "is_fresh_7d")
    assert schema.RANKER_V2_FEATURES[-2:] == ("cat_share", "hist_len")


def test_assemble_stacks_float32_columns_in_the_requested_order_and_extras_win():
    feats = {"a": np.array([1, 2], dtype=np.int64), "b": np.array([0.5, np.nan]), "x": np.array([9.0, 9.0])}
    extra = {"x": np.array([np.nan, 7.0])}

    m = schema.assemble(feats, extra, ["b", "x", "a"])

    assert m.dtype == np.float32 and m.shape == (2, 3)
    assert np.array_equal(m, np.array([[0.5, np.nan, 1], [np.nan, 7, 2]], dtype=np.float32), equal_nan=True)
    assert schema.assemble(feats, {}, []).shape == (2, 0)
    with pytest.raises(KeyError):
        schema.assemble(feats, {}, ["missing"])


def test_schema_hash_changes_with_column_order_and_with_the_definition():
    cols = list(schema.RANKER_V2_FEATURES)
    base = schema.schema_hash(cols, {"half_life_days": 7.0, "short_max_events": 20})

    assert base == schema.schema_hash(tuple(cols), {"short_max_events": 20, "half_life_days": 7.0})
    assert base != schema.schema_hash(cols[::-1], {"half_life_days": 7.0, "short_max_events": 20})
    assert base != schema.schema_hash(cols, {"half_life_days": 7.0, "short_max_events": None})
    assert len(base) == 64


def test_serving_candidate_spec_defaults():
    assert SERVING_CANDIDATE_SPEC == CandidateSpec(
        window_h=72,
        sources=(("knn_profile", 100), ("knn_short", 100), ("recent", 100), ("popular", 100), ("category", 50)),
        cap=300,
    )
    assert SERVING_CANDIDATE_SPEC.as_dict() == {
        "window_h": 72.0, "cap": 300,
        "sources": [["knn_profile", 100], ["knn_short", 100], ["recent", 100], ["popular", 100], ["category", 50]],
    }


def test_round_robin_union_alternates_sources_skips_duplicates_and_stops_at_the_cap():
    merged, contributed = round_robin_union({"a": [1, 2, 3, 4], "b": [2, 5], "c": []}, cap=5)

    assert merged == [1, 2, 3, 5, 4]
    assert contributed == {"a": 3, "b": 2, "c": 0}  # 2는 b가 먼저 넣었다
    assert round_robin_union({"a": [1, 2, 3], "b": [9, 8, 7]}, cap=3) == ([1, 9, 2], {"a": 2, "b": 1})
