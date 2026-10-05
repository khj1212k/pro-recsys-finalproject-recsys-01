"""장기 프로필의 증분 상태(recsys_core/profile.py)가 로그 전체에서 다시 계산한 값과 같은지 본다.

기준은 두 가지다: (1) 정의식으로 한 번에 계산한 상태(rebuild), (2) 오프라인 하네스가 쓰는 경로
(compute_features가 이벤트 인덱스에서 계산하는 hist_cos / hist_len / cat_share / hours_since_last_event).
"""
import numpy as np
import pytest

from recsys_core import (
    DAY,
    HOUR,
    EventIndex,
    FeatureConfig,
    FeatureContext,
    ItemCatalog,
    Requests,
    UserHistState,
    compute_feature_columns,
)
from recsys_core.profile import (
    NO_CATEGORY,
    HistState,
    apply_event,
    counts_row,
    rebuild,
    same_state,
    unit_rows,
)

T0 = 1_760_000_000
DIM = 32
N_ITEMS = 60
N_CAT = 6
GROUPS = ("history", "category", "short_term")


def _catalog(rng):
    emb = unit_rows(rng.standard_normal((N_ITEMS, DIM)).astype(np.float32) * rng.uniform(0.2, 5.0, (N_ITEMS, 1)))
    return ItemCatalog(
        ids=np.arange(1000, 1000 + N_ITEMS), emb=emb,
        pub_time=T0 - rng.integers(0, 5 * DAY, N_ITEMS),
        category=rng.integers(0, N_CAT, N_ITEMS), n_categories=N_CAT,
    )


def _events(rng, n, span_days=30):
    """(시각, 아이템) n건. 같은 초에 겹치는 이벤트와 같은 아이템의 반복 클릭이 섞인다."""
    times = T0 + np.sort(rng.integers(0, int(span_days * DAY), n))
    times[rng.random(n) < 0.1] = times[0]  # 일부를 같은 시각으로
    return np.sort(times), rng.integers(0, N_ITEMS, n)


def _incremental(cat, times, items, upto=None):
    state = HistState()
    for t, i in zip(times, items):
        if upto is not None and t >= upto:
            continue
        state = apply_event(state, int(t), cat.emb[i], int(cat.category[i]))
    return state


def _snapshot(state, user=0):
    return UserHistState(
        users=[user],
        hist_sum=np.zeros((1, DIM)) if state.empty else state.hist_sum[None, :],
        hist_len=[state.hist_len],
        cat_counts=counts_row(state.cat_counts, N_CAT)[None, :],
        last_time=[-1 if state.empty else state.anchor_s],
    )


def _features(cat, user_log, read_at, snapshot=None):
    ctx = FeatureContext(catalog=cat, user_log=user_log, user_hist_state=snapshot, config=FeatureConfig())
    req = Requests(user=[0], time=[read_at], cand_ptr=[0, N_ITEMS], cand_item=np.arange(N_ITEMS))
    return compute_feature_columns(ctx, req, groups=GROUPS)


@pytest.mark.parametrize("seed,n_events", [(0, 1), (1, 7), (2, 40), (3, 300)])
def test_incremental_state_equals_the_definition_computed_in_one_pass(seed, n_events):
    rng = np.random.default_rng(seed)
    cat = _catalog(rng)
    times, items = _events(rng, n_events)

    inc = _incremental(cat, times, items)
    batch = rebuild((int(t), cat.emb[i], int(cat.category[i])) for t, i in zip(times, items))

    assert same_state(inc, batch, cos_tol=1e-12)
    assert inc.hist_len == n_events and sum(inc.cat_counts.values()) == n_events
    assert inc.anchor_s == int(times.max())


def test_events_arriving_out_of_order_give_the_same_state():
    rng = np.random.default_rng(7)
    cat = _catalog(rng)
    times, items = _events(rng, 50)
    shuffled = rng.permutation(len(times))

    in_order = _incremental(cat, times, items)
    out_of_order = _incremental(cat, times[shuffled], items[shuffled])

    assert same_state(in_order, out_of_order, cos_tol=1e-12)


@pytest.mark.parametrize("seed,n_events", [(11, 5), (12, 60), (13, 400)])
def test_serving_snapshot_gives_the_harness_features_at_any_read_time(seed, n_events):
    """읽는 시각을 이벤트 사이사이와 마지막 이벤트 한참 뒤로 옮겨 가며, 그때까지의 이벤트만 반영한 증분 상태로
    계산한 피처가 로그 전체를 든 하네스 경로와 같은지 본다. 읽을 때 감쇠를 다시 계산하지 않는다."""
    rng = np.random.default_rng(seed)
    cat = _catalog(rng)
    times, items = _events(rng, n_events)
    user_log = EventIndex(np.zeros(n_events, dtype=np.int64), times, items)
    # 단기 창·세션 피처는 두 경로가 같은 이벤트 인덱스를 본다: 여기서 다른 것은 장기 상태의 출처뿐이다.
    read_times = np.unique(np.concatenate([
        times[rng.integers(0, n_events, 8)] + 1,      # 어떤 이벤트 직후
        times[rng.integers(0, n_events, 4)],          # 어떤 이벤트와 같은 초(그 이벤트는 아직 안 보인다)
        [int(times.max()) + 3 * HOUR, int(times.max()) + 90 * DAY, int(times.min())],
    ]))

    worst = {}
    for read_at in read_times:
        state = _incremental(cat, times, items, upto=read_at)
        offline = _features(cat, user_log, int(read_at))
        serving = _features(cat, user_log, int(read_at), snapshot=_snapshot(state))
        for name in offline:
            diff = np.abs(offline[name].astype(np.float64) - serving[name].astype(np.float64))
            both_nan = np.isnan(offline[name]) & np.isnan(serving[name])
            worst[name] = max(worst.get(name, 0.0), float(np.max(np.where(both_nan, 0.0, diff))))
            assert offline[name].dtype == serving[name].dtype == np.float32
        assert np.array_equal(offline["hist_len"], serving["hist_len"])
        assert np.array_equal(offline["cat_share"], serving["cat_share"])
        assert np.array_equal(offline["hours_since_last_event"], serving["hours_since_last_event"], equal_nan=True)

    assert max(worst.values()) < 1e-6, worst


def test_an_empty_state_reads_as_no_history():
    rng = np.random.default_rng(5)
    cat = _catalog(rng)
    empty_log = EventIndex([], [], [])

    offline = _features(cat, empty_log, T0)
    serving = _features(cat, empty_log, T0, snapshot=_snapshot(HistState()))

    assert not serving["hist_cos"].any() and not serving["hist_len"].any() and not serving["cat_share"].any()
    assert np.isnan(serving["hours_since_last_event"]).all()
    for name in offline:
        assert np.array_equal(offline[name], serving[name], equal_nan=True)


def test_a_user_missing_from_the_snapshot_reads_as_no_history():
    rng = np.random.default_rng(6)
    cat = _catalog(rng)
    state = apply_event(HistState(), T0, cat.emb[3], int(cat.category[3]))

    serving = _features(cat, EventIndex([], [], []), T0 + HOUR, snapshot=_snapshot(state, user=99))

    assert not serving["hist_cos"].any() and not serving["hist_len"].any()


def test_an_item_without_a_category_is_counted_under_the_no_category_slot():
    e = np.ones(DIM, dtype=np.float32)
    state = apply_event(apply_event(HistState(), T0, e), T0 + 5, e, category=3)

    assert state.cat_counts == {NO_CATEGORY: 1, 3: 1}
    assert counts_row(state.cat_counts, 5).tolist() == [1, 0, 0, 1, 0]
    with pytest.raises(ValueError):
        counts_row({7: 1}, 5)


def test_the_stored_sum_is_built_from_unit_vectors_whatever_the_raw_norm():
    rng = np.random.default_rng(9)
    v = rng.standard_normal(DIM).astype(np.float32)

    a = apply_event(apply_event(HistState(), T0, v), T0 + DAY, 3.0 * v)
    b = apply_event(apply_event(HistState(), T0, 10.0 * v), T0 + DAY, 0.1 * v)

    # 단위 벡터로 만드는 연산이 float32라 두 상태는 그 정밀도 안에서 같다
    assert same_state(a, b, cos_tol=1e-6)
    assert np.linalg.norm(a.hist_sum) == pytest.approx(1.0 + 0.5 ** (1 / 7), rel=1e-6)


def test_same_state_tells_apart_states_that_differ_in_length_anchor_or_direction():
    e0 = np.eye(DIM, dtype=np.float32)[0]
    e1 = np.eye(DIM, dtype=np.float32)[1]
    base = apply_event(HistState(), T0, e0, 1)

    assert same_state(base, apply_event(HistState(), T0, e0, 1))
    assert not same_state(base, apply_event(base, T0, e0, 1))            # 이벤트 수
    assert not same_state(base, apply_event(HistState(), T0 + 1, e0, 1))  # 기준 시각
    assert not same_state(base, apply_event(HistState(), T0, e1, 1))      # 방향
    assert not same_state(base, apply_event(HistState(), T0, e0, 2))      # 카테고리
    assert same_state(HistState(), HistState()) and not same_state(base, HistState())
