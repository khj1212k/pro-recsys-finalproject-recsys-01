"""서빙 어댑터(recsys_core/serving.py)가 내는 행렬이, 같은 로그를 하네스 방식으로 적재해 계산한
행렬(evaluation/recsys/service_logs.py)과 같은지 본다. 열 이름·순서·dtype·결측 표기 포함.

여기서 서빙 쪽 입력은 로그에서 직접 만든 참조값(tests/recsys/serving_world.py)이다. 실제 저장소(SQL)가
같은 입력을 주는지는 parity 게이트(tests/integration)가 본다.
"""
from datetime import timedelta

import numpy as np
import pytest

from evaluation.recsys.ebnerd import models
from evaluation.recsys.service_logs import LogBench, epoch_us
from recsys_core import schema
from recsys_core.profile import HistState
from recsys_core.serving import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    ITEM_LAG_S,
    SCHEMA_HASH,
    SHORT_MAX_EVENTS,
    FeatureInputsMissing,
    WindowCounts,
    epoch_seconds,
    features,
    item_window_end,
    request_second,
    short_window_start,
)
from tests.recsys.serving_world import T0, WorldState, make_world

COL = {name: i for i, name in enumerate(FEATURE_NAMES)}


def _request_times(world, user, rng, n=6):
    """그 사용자의 클릭 직후(1마이크로초 뒤), 같은 초의 클릭 직전, 그리고 아무 때나."""
    mine = sorted(at for u, _, at in world.clicks if u == user)
    times = [T0 + timedelta(days=12, seconds=30), T0 + timedelta(days=30)]
    for at in (mine[i] for i in rng.integers(0, len(mine), n)) if mine else ():
        times += [at + timedelta(microseconds=1), at, at - timedelta(microseconds=1), at + timedelta(seconds=400)]
    times += [T0 + timedelta(seconds=int(rng.integers(0, 12 * 86400))) for _ in range(n)]
    if user == 1:  # 클릭을 몰아 넣은 사용자: 30분 안에 이어지는 긴 세션의 끝 무렵(세션·단기 상한이 걸리는 곳)
        times += [at + timedelta(microseconds=1) for at in mine[-45:]]
    return times


def _compare(world, bench, user, now, candidate_ids):
    items = [world.items[i] for i in candidate_ids]
    serving = features(world.state(user, now, candidate_ids), items, now)
    offline = bench.request_features(user, epoch_us(now), candidate_ids)
    return serving, offline


def test_adapter_columns_are_the_harness_ranker_v2_columns():
    assert features.feature_names == list(models.V2_FEATURES) == list(schema.RANKER_V2_FEATURES)
    assert features.schema_version == FEATURE_SCHEMA_VERSION == 2
    assert features.schema_hash == SCHEMA_HASH and len(SCHEMA_HASH) == 64


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_adapter_matrix_equals_the_harness_path_on_the_same_logs(seed):
    world = make_world(seed)
    bench = LogBench(world.logs())
    rng = np.random.default_rng(100 + seed)
    ids = np.array(sorted(world.items))
    worst = np.zeros(len(FEATURE_NAMES))
    n_requests = 0
    seen = {"capped_short": False, "in_session": False, "no_history": False, "long_session": False}

    for user in sorted(world.user_categories):
        for now in _request_times(world, user, rng):
            cands = rng.choice(ids, size=int(rng.integers(1, 60)), replace=False).tolist()
            serving, offline = _compare(world, bench, user, now, cands)
            n_requests += 1

            assert serving.shape == offline.shape == (len(cands), 22)
            assert serving.dtype == offline.dtype == np.float32
            assert np.array_equal(np.isnan(serving), np.isnan(offline))
            worst = np.maximum(worst, np.nanmax(np.abs(serving.astype(np.float64) - offline), axis=0, initial=0.0))
            # 세는 값과 0/1 값은 오차 없이 같아야 한다
            for name in ("hist_len", "short_len", "sess_len", "pop_clicks_6h", "pop_clicks_24h", "pop_clicks_48h",
                         "pop_inviews_24h", "news_category", "is_cat_match", "user_ncat", "is_fresh_24h"):
                assert np.array_equal(serving[:, COL[name]], offline[:, COL[name]]), name
            assert np.isnan(serving[:, COL["user_age"]]).all() and np.isnan(serving[:, COL["user_gender"]]).all()

            seen["capped_short"] |= bool(serving[0, COL["short_len"]] == SHORT_MAX_EVENTS)
            seen["in_session"] |= bool(serving[0, COL["sess_len"]] > 0)
            seen["long_session"] |= bool(serving[0, COL["sess_len"]] == SHORT_MAX_EVENTS)
            seen["no_history"] |= bool(serving[0, COL["hist_len"]] == 0)

    assert n_requests >= 100
    assert worst.max() < 1e-6, dict(zip(FEATURE_NAMES, worst))
    assert all(seen.values()), seen  # 상한·세션·무이력 경우가 실제로 지나갔다


def test_the_cap_boundary_inside_one_second_keeps_the_same_clicks_on_both_paths():
    """3초 안에 26번 클릭한 사용자: 최근 20개의 경계가 같은 초의 클릭 여러 건에 걸린다. 서빙이 마이크로초로
    최근 20개를 고르면 로그 재계산(정수 초)과 서로 다른 클릭이 남아 short_cos가 어긋난다."""
    world = make_world(8)
    bench = LogBench(world.logs())
    rapid = sorted(at for u, _, at in world.clicks if u == 2)[-26:]
    cands = sorted(world.items)[:40]
    by_second = {}
    for at in rapid:
        by_second[epoch_seconds(at)] = by_second.get(epoch_seconds(at), 0) + 1
    assert max(by_second.values()) >= 8 and rapid[-1] - rapid[0] < timedelta(seconds=3)

    for now in (rapid[-1] + timedelta(microseconds=1), rapid[22] + timedelta(microseconds=1),
                rapid[-1] + timedelta(seconds=20)):
        serving, offline = _compare(world, bench, 2, now, cands)
        assert serving[0, COL["short_len"]] == offline[0, COL["short_len"]] == SHORT_MAX_EVENTS
        assert np.nanmax(np.abs(serving.astype(np.float64) - offline)) < 1e-6

    # 마이크로초 순으로 최근 20개를 고른 입력은 실제로 다른 값을 낸다(이 테스트가 그 차이를 볼 수 있다는 확인)
    now = rapid[-1] + timedelta(microseconds=1)
    state = world.state(2, now, cands)
    since = short_window_start(now)
    by_micro = sorted(((at, nid) for u, nid, at in world.clicks if u == 2 and since <= at < now))[-SHORT_MAX_EVENTS:]
    wrong = WorldState(**{**state.__dict__, "recent_clicks": [
        type(state.recent_clicks[0])(at, world.items[nid].embedding, nid) for at, nid in by_micro]})
    drifted = features(wrong, [world.items[i] for i in cands], now)
    assert np.abs(drifted[:, COL["short_cos"]] - bench.request_features(2, epoch_us(now), cands)[:, COL["short_cos"]]).max() > 1e-4


def test_a_click_just_before_the_request_counts_and_one_just_after_does_not():
    world = make_world(3)
    bench = LogBench(world.logs())
    user = 2
    at = max(c[2] for c in world.clicks if c[0] == user)
    cands = sorted(world.items)[:10]

    before, _ = _compare(world, bench, user, at, cands)                               # 그 클릭과 같은 시각: 아직 없다
    after, off_after = _compare(world, bench, user, at + timedelta(microseconds=1), cands)

    assert after[0, COL["hist_len"]] == before[0, COL["hist_len"]] + 1
    assert after[0, COL["hours_since_last_event"]] == pytest.approx(1 / 3600)  # 같은 초의 클릭도 1초 전으로 센다
    assert np.allclose(after, off_after, atol=1e-6, equal_nan=True)


def test_popularity_windows_end_before_the_request_so_fresh_rows_are_not_counted():
    world = make_world(4)
    nid = sorted(world.items)[0]
    now = T0 + timedelta(days=5, microseconds=250_000)
    # 요청의 0.1초 전·1.2초 전 노출은 창 밖이고, 2.5초 전 노출부터 센다
    world.inviews = [(nid, now - timedelta(seconds=s)) for s in (0.1, 1.2, 2.5, 3600)]
    world.clicks = [(9, nid, now - timedelta(seconds=s)) for s in (0.1, 2.5)]
    bench = LogBench(world.logs())

    serving, offline = _compare(world, bench, 9, now, [nid])

    assert item_window_end(now) == now.replace(microsecond=0) + timedelta(seconds=1 - ITEM_LAG_S)
    assert serving[0, COL["pop_inviews_24h"]] == offline[0, COL["pop_inviews_24h"]] == 2
    assert serving[0, COL["pop_clicks_6h"]] == offline[0, COL["pop_clicks_6h"]] == 1
    # 유저 쪽은 지연이 없다: 0.1초 전의 자기 클릭은 히스토리에 들어간다
    assert serving[0, COL["hist_len"]] == offline[0, COL["hist_len"]] == 2


def test_time_helpers_use_whole_seconds_without_float_rounding():
    now = T0.replace(microsecond=999_999)

    assert request_second(now) == epoch_seconds(T0) + 1
    assert request_second(T0) == epoch_seconds(T0) + 1
    assert short_window_start(now) == T0 + timedelta(seconds=1) - timedelta(hours=24)
    with pytest.raises(ValueError):
        epoch_seconds(now.replace(tzinfo=None))


def _state(world, user, now, cands, **overrides):
    s = world.state(user, now, cands)
    return WorldState(**{**s.__dict__, **overrides})


def test_missing_or_inconsistent_inputs_raise_instead_of_producing_features():
    world = make_world(5)
    user, now = 2, T0 + timedelta(days=12, seconds=1)
    cands = sorted(world.items)[:5]
    items = [world.items[i] for i in cands]
    with pytest.raises(FeatureInputsMissing, match="popularity"):
        features(_state(world, user, now, cands, popularity=None), items, now)

    # 장기 상태가 요청 시각보다 앞서 있다(요청 뒤의 클릭이 이미 반영됨)
    late = now + timedelta(seconds=5)
    with pytest.raises(FeatureInputsMissing, match="이후의 클릭이 반영"):
        features(_state(world, user, now, cands, hist=world.hist(user, late + timedelta(days=1))), items,
                 max(c[2] for c in world.clicks if c[0] == user) - timedelta(seconds=5))

    # 최근 클릭은 있는데 장기 상태가 비어 있다(상태가 로그보다 뒤처짐)
    at = max(c[2] for c in world.clicks if c[0] == user)
    soon = at + timedelta(seconds=10)
    behind = _state(world, user, soon, cands, hist=HistState())
    with pytest.raises(FeatureInputsMissing, match="뒤처져"):
        features(behind, items, soon)

    # 요청 시각 이후의 클릭이 최근 클릭 목록에 들어 있다
    with pytest.raises(FeatureInputsMissing, match="recent_clicks"):
        features(world.state(user, soon, cands), items, at)


def test_no_candidates_and_a_user_with_nothing():
    world = make_world(6)
    nobody = max(world.user_categories)  # 클릭 없는 사용자
    now = T0 + timedelta(days=3)
    cands = sorted(world.items)[:7]

    empty = features(world.state(nobody, now, []), [], now)
    m = features(world.state(nobody, now, cands), [world.items[i] for i in cands], now)

    assert empty.shape == (0, 22) and empty.dtype == np.float32
    for name in ("hist_cos", "hist_len", "cat_share", "short_cos", "short_len", "sess_cos", "sess_len"):
        assert not m[:, COL[name]].any(), name
    assert np.isnan(m[:, COL["hours_since_last_event"]]).all()


def test_wrong_window_count_shape_and_out_of_range_category_are_errors():
    world = make_world(7)
    now = T0 + timedelta(days=3)
    cands = sorted(world.items)[:3]
    items = [world.items[i] for i in cands]

    with pytest.raises(ValueError, match="창의 수"):
        features(_state(world, 2, now, cands, popularity={cands[0]: WindowCounts((1, 2), 3)}), items, now)
    with pytest.raises(ValueError, match="카테고리 ID"):
        features(_state(world, 2, now, cands, category_ids=[10**7]), items, now)
