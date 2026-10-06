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
from recsys_core.profile import HistState, apply_event
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


def _hist_read_at(world, user, read_at):
    """read_at에 읽은 장기 상태(그때까지 커밋된 클릭이 전부 들어 있다)와 그 마지막 클릭 시각."""
    return {"hist": world.hist(user, read_at), "hist_last_event_at": world.hist_last_event_at(user, read_at)}


def test_missing_or_inconsistent_inputs_raise_instead_of_producing_features():
    world = make_world(5)
    user, now = 2, T0 + timedelta(days=12, seconds=1)
    cands = sorted(world.items)[:5]
    items = [world.items[i] for i in cands]
    with pytest.raises(FeatureInputsMissing, match="popularity"):
        features(_state(world, user, now, cands, popularity=None), items, now)

    # 장기 상태가 요청 시각보다 앞서 있다(요청 뒤의 클릭이 이미 반영됨)
    at = max(c[2] for c in world.clicks if c[0] == user)
    early = at - timedelta(seconds=5)
    ahead = _state(world, user, early, cands, **_hist_read_at(world, user, now + timedelta(days=1)))
    with pytest.raises(FeatureInputsMissing, match="이후의 클릭이 반영"):
        features(ahead, items, early)

    # 최근 클릭은 있는데 장기 상태가 비어 있다(상태가 로그보다 뒤처짐)
    soon = at + timedelta(seconds=10)
    behind = _state(world, user, soon, cands, hist=HistState(), hist_last_event_at=None)
    with pytest.raises(FeatureInputsMissing, match="뒤처져"):
        features(behind, items, soon)

    # 요청 시각 이후의 클릭이 최근 클릭 목록에 들어 있다(장기 상태는 요청 시각의 것)
    with pytest.raises(FeatureInputsMissing, match="recent_clicks"):
        features(_state(world, user, soon, cands, **_hist_read_at(world, user, at)), items, at)


def _same_second_click(world, user, now, offset_us):
    """now와 같은 초 안에서 offset_us만큼 떨어진 시각에 그 사용자의 클릭 하나를 로그에 더한다."""
    at = now + timedelta(microseconds=offset_us)
    assert epoch_seconds(at) == epoch_seconds(now)
    clicked = {nid for u, nid, _ in world.clicks if u == user}
    nid = next(i for i in sorted(world.items) if i not in clicked)
    world.clicks.append((user, nid, at))
    return at


@pytest.mark.parametrize("offset_us", [0, 1, 300_000])
def test_a_click_at_or_after_the_request_time_in_the_same_second_is_noticed_in_the_long_term_state(offset_us):
    """요청 시각 now와 장기 상태를 읽는 사이에 같은 사용자의 클릭이 커밋된 경우. 그 클릭이 now와 같은 초 안이면
    초 단위 비교(상태의 기준 초 >= 요청 초)로는 보이지 않는다: 상태에는 요청 이후의 클릭이 들어 있고 최근 클릭
    목록(now 미만)에는 없어, hist_cos·cat_share·hist_len이 로그 재계산과 조용히 어긋난다. 마이크로초로 봐야 잡힌다."""
    world = make_world(5)
    user = 2
    now = T0 + timedelta(days=12, hours=2, microseconds=200_000)
    cands = sorted(world.items)[:8]
    items = [world.items[i] for i in cands]
    before = world.state(user, now, cands)
    post = _same_second_click(world, user, now, offset_us)

    # 그 클릭이 커밋된 뒤에 읽은 상태: 초 단위의 기준 시각은 요청 초보다 앞이라 예전 검사는 지나갔다
    racy = _state(world, user, now, cands, **_hist_read_at(world, user, post + timedelta(microseconds=1)))
    assert racy.hist.hist_len == before.hist.hist_len + 1 and racy.hist.anchor_s < request_second(now)
    assert [c.news_letter_id for c in racy.recent_clicks] == [c.news_letter_id for c in before.recent_clicks]

    with pytest.raises(FeatureInputsMissing, match="이후의 클릭이 반영"):
        features(racy, items, now)

    # 그 클릭이 반영되기 전에 읽은 상태는 로그 재계산(그 클릭은 now 이후라 세지 않는다)과 같은 값을 낸다
    serving = features(before, items, now)
    offline = LogBench(world.logs()).request_features(user, epoch_us(now), cands)
    assert np.allclose(serving, offline, atol=1e-6, equal_nan=True)
    assert serving[0, COL["hist_len"]] == before.hist.hist_len


def test_a_same_second_click_missing_from_the_long_term_state_is_noticed():
    """반대 방향: 요청 이전의 클릭이 최근 클릭 목록에는 있는데 장기 상태에는 아직 없다(상태를 읽은 뒤에 커밋됨).
    그 클릭이 상태의 마지막 클릭과 같은 초면 초 단위 비교로는 뒤처진 것이 보이지 않는다."""
    world = make_world(5)
    user = 2
    base = T0 + timedelta(days=12, hours=2, microseconds=100_000)
    earlier = _same_second_click(world, user, base, 0)
    now = base + timedelta(microseconds=600_000)
    cands = sorted(world.items)[:8]
    items = [world.items[i] for i in cands]
    stale = _hist_read_at(world, user, earlier + timedelta(microseconds=1))
    later = _same_second_click(world, user, base, 250_000)   # earlier와 같은 초, now 이전

    state = _state(world, user, now, cands, **stale)
    assert state.hist.anchor_s == epoch_seconds(later) and later in [c.at for c in state.recent_clicks]

    with pytest.raises(FeatureInputsMissing, match="뒤처져"):
        features(state, items, now)


def test_a_click_missing_from_the_state_is_noticed_only_while_it_is_the_newest_one():
    """알고 있는 한계(ADR 0033)를 고정해 둔다. 어댑터가 "상태가 로그보다 뒤처졌다"를 아는 근거는 시각 하나다:
    최근 클릭 중 상태의 마지막 클릭보다 늦은 것이 있는가. 클릭 시점에 뉴스레터의 임베딩이 없어 상태에 들어가지
    못한 클릭(임베딩은 나중에 채워져 로그 재계산에는 들어간다)이 그 예다.
    - 빠진 클릭이 가장 최근이면: 알아채고 값을 내지 않는다(그 사용자는 재구축 전까지 계속 그렇다).
    - 그 뒤에 다른 클릭이 반영되면: 시각으로는 빠진 것이 보이지 않아 값이 로그 재계산과 어긋난 채 나간다.
      이것을 찾는 것은 `rebuild_user_state --check`다."""
    world = make_world(5)
    user = 2
    base = T0 + timedelta(days=12, hours=3)
    cands = sorted(world.items)[:8]
    items = [world.items[i] for i in cands]
    skipped = _same_second_click(world, user, base, 100_000)          # 상태에 들어가지 못한 클릭
    without_skipped = _hist_read_at(world, user, skipped)             # 그 클릭 직전까지의 상태

    soon = skipped + timedelta(seconds=30)
    with pytest.raises(FeatureInputsMissing, match="뒤처져"):
        features(_state(world, user, soon, cands, **without_skipped), items, soon)

    # 그 뒤의 클릭 하나가 정상적으로 반영된다: 상태 = (빠진 클릭 없는 상태) + 새 클릭
    later = skipped + timedelta(seconds=60)
    nid = next(i for i in sorted(world.items) if i not in {n for u, n, _ in world.clicks if u == user})
    world.clicks.append((user, nid, later))
    item = world.items[nid]
    hist = apply_event(without_skipped["hist"], epoch_seconds(later), item.embedding, item.category_id or 0)
    now = later + timedelta(seconds=30)
    stale = _state(world, user, now, cands, hist=hist, hist_last_event_at=later)

    serving = features(stale, items, now)  # 알아채지 못한다
    offline = LogBench(world.logs()).request_features(user, epoch_us(now), cands)
    assert serving[0, COL["hist_len"]] == offline[0, COL["hist_len"]] - 1
    assert np.abs(serving[:, COL["hist_cos"]] - offline[:, COL["hist_cos"]]).max() > 1e-3


def test_a_long_term_state_without_a_matching_last_event_time_is_not_usable():
    """마이크로초 비교의 근거(hist_last_event_at)가 없거나 상태의 기준 초와 다르면 값을 내지 않는다."""
    world = make_world(5)
    user, now = 2, T0 + timedelta(days=12, seconds=1)
    cands = sorted(world.items)[:5]
    items = [world.items[i] for i in cands]
    good = world.state(user, now, cands)
    assert not good.hist.empty and epoch_seconds(good.hist_last_event_at) == good.hist.anchor_s

    with pytest.raises(FeatureInputsMissing, match="hist_last_event_at"):
        features(_state(world, user, now, cands, hist_last_event_at=None), items, now)
    with pytest.raises(FeatureInputsMissing, match="hist_last_event_at"):
        features(_state(world, user, now, cands, hist_last_event_at=good.hist_last_event_at - timedelta(seconds=2)),
                 items, now)
    assert features(good, items, now).shape == (5, 22)


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
