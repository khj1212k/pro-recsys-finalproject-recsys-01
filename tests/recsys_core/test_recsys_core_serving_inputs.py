"""서빙이 쓰는 recsys_core 옵션: 단기 상한, 아이템 쪽 기준 시각 지연, 인기도 창 집계 스냅숏, 세션 나누기.

기본값(상한 없음, 지연 0, 스냅숏 없음)은 EB-NeRD 하네스의 정의이고 이 옵션들로 바뀌지 않아야 한다.
"""
import numpy as np
import pytest

from recsys_core import (
    HOUR,
    EventIndex,
    FeatureConfig,
    FeatureContext,
    ItemCatalog,
    ItemWindowCounts,
    Requests,
    compute_feature_columns,
    compute_features,
    request_sessions,
    sessionize,
    window_vectors,
)

T = 1_760_000_000


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _catalog():
    emb = np.stack([_unit([1, 0, 0]), _unit([0, 1, 0]), _unit([0, 0, 1]), _unit([1, 1, 0])])
    return ItemCatalog(ids=[10, 11, 12, 13], emb=emb, pub_time=[T - HOUR] * 4, category=[0, 1, 1, 2],
                       n_categories=3)


def _index(events):
    events = list(events)
    if not events:
        return EventIndex([], [], [])
    k, t, i = zip(*events)
    return EventIndex(k, t, i)


def _req(cands=(0, 1, 2, 3), user=7, t=T, session=None):
    return Requests(user=[user], time=[t], cand_ptr=[0, len(cands)], cand_item=list(cands),
                    session=None if session is None else [session])


# ------------------------------------------------------------------ 단기 상한
def test_short_max_events_keeps_only_the_most_recent_events_of_the_window():
    # 24h 창 안에 item0(3h 전), item1(2h 전), item2(1h 전). 상한 2면 item1+item2만 남는다.
    log = _index([(7, T - 3 * HOUR, 0), (7, T - 2 * HOUR, 1), (7, T - HOUR, 2)])
    capped = FeatureContext(catalog=_catalog(), user_log=log, config=FeatureConfig(short_max_events=2))
    free = FeatureContext(catalog=_catalog(), user_log=log, config=FeatureConfig())

    c = compute_feature_columns(capped, _req(), groups=["short_term"])
    f = compute_feature_columns(free, _req(), groups=["short_term"])

    s2 = _unit([0, 1, 1])
    assert c["short_cos"].tolist() == pytest.approx([0.0, s2[1], s2[2], float(s2 @ _unit([1, 1, 0]))], abs=1e-6)
    assert c["short_len"].tolist() == [2, 2, 2, 2]
    s3 = _unit([1, 1, 1])
    assert f["short_cos"].tolist() == pytest.approx([s3[0], s3[1], s3[2], float(s3 @ _unit([1, 1, 0]))], abs=1e-6)
    assert f["short_len"].tolist() == [3, 3, 3, 3]
    # 상한은 마지막 이벤트까지의 시간에는 영향이 없다
    assert c["hours_since_last_event"].tolist() == f["hours_since_last_event"].tolist() == [1.0] * 4


def test_short_max_events_also_caps_the_session_vector():
    session_log = _index([(5, T - 50 * 60, 0), (5, T - 30 * 60, 1), (5, T - 10 * 60, 2)])
    ctx = FeatureContext(catalog=_catalog(), user_log=_index([]), session_log=session_log,
                         config=FeatureConfig(short_max_events=1))

    f = compute_feature_columns(ctx, _req(session=5), groups=["short_term"])

    assert f["sess_len"].tolist() == [1, 1, 1, 1]
    assert f["sess_cos"].tolist() == pytest.approx([0.0, 0.0, 1.0, 0.0], abs=1e-6)


def test_a_cap_larger_than_the_window_changes_nothing():
    log = _index([(7, T - 3 * HOUR, 0), (7, T - HOUR, 2)])
    a = compute_feature_columns(
        FeatureContext(catalog=_catalog(), user_log=log, config=FeatureConfig(short_max_events=20)),
        _req(), groups=["short_term"])
    b = compute_feature_columns(FeatureContext(catalog=_catalog(), user_log=log), _req(), groups=["short_term"])

    for name in a:
        assert np.array_equal(a[name], b[name], equal_nan=True)


def test_window_vectors_max_events_counts_what_it_used():
    log = _index([(7, T - 3 * HOUR, 0), (7, T - 2 * HOUR, 1), (7, T - HOUR, 2)])
    vec, n = window_vectors(log, _catalog().emb, [7, 8], T - 24 * HOUR, T, max_events=2)

    assert n.tolist() == [2, 0]
    assert vec[0] == pytest.approx(_unit([0, 1, 1]), abs=1e-6) and not vec[1].any()


# ------------------------------------------------------------------ 아이템 쪽 기준 시각
def _pop_ctx(clicks, inviews, **cfg):
    return FeatureContext(
        catalog=_catalog(), user_log=_index([]),
        item_clicks=_index((i, t, i) for i, t in clicks),
        item_inviews=_index((i, t, i) for i, t in inviews),
        config=FeatureConfig(**cfg),
    )


def test_item_lag_moves_the_end_of_every_popularity_window_back():
    # item1 클릭: 1초 전, 5초 전, 6h+3초 전. 노출: 1초 전, 10초 전.
    clicks = [(1, T - 1), (1, T - 5), (1, T - 6 * HOUR - 3)]
    inviews = [(1, T - 1), (1, T - 10)]

    now = compute_feature_columns(_pop_ctx(clicks, inviews), _req(), groups=["popularity"])
    lagged = compute_feature_columns(_pop_ctx(clicks, inviews, item_lag_s=2), _req(), groups=["popularity"])

    assert now["pop_clicks_6h"][1] == 2 and now["pop_clicks_24h"][1] == 3 and now["pop_inviews_24h"][1] == 2
    # 지연 2초: 1초 전의 클릭·노출은 아직 세지 않는다. 6h 창은 [t-2-6h, t-2)라 6h+3초 전 클릭은 여전히 밖이다.
    assert lagged["pop_clicks_6h"][1] == 1 and lagged["pop_clicks_24h"][1] == 2
    assert lagged["pop_inviews_24h"][1] == 1
    assert lagged["pop_ctr_24h"][1] == pytest.approx(2.0)
    assert lagged["pop_clicks_6h"][[0, 2, 3]].tolist() == [0, 0, 0]


def test_item_lag_does_not_touch_user_side_or_recency_features():
    user_log = _index([(7, T - 1, 0)])
    base = FeatureContext(catalog=_catalog(), user_log=user_log, item_clicks=_index([]), item_inviews=_index([]))
    lag = FeatureContext(catalog=_catalog(), user_log=user_log, item_clicks=_index([]), item_inviews=_index([]),
                         config=FeatureConfig(item_lag_s=2))
    groups = ["recency", "history", "category", "short_term"]

    a = compute_feature_columns(base, _req(), groups=groups)
    b = compute_feature_columns(lag, _req(), groups=groups)

    for name in a:
        assert np.array_equal(a[name], b[name], equal_nan=True)
    assert a["hist_len"].tolist() == [1, 1, 1, 1]  # 1초 전의 유저 이벤트는 보인다


def test_window_count_snapshot_equals_counting_the_event_logs():
    rng = np.random.default_rng(3)
    lag = 2
    clicks = [(int(i), int(T - s)) for i, s in zip(rng.integers(0, 4, 400), rng.integers(0, 60 * HOUR, 400))]
    inviews = [(int(i), int(T - s)) for i, s in zip(rng.integers(0, 4, 900), rng.integers(0, 60 * HOUR, 900))]
    from_logs = compute_feature_columns(_pop_ctx(clicks, inviews, item_lag_s=lag), _req(), groups=["popularity"])

    end = T - lag

    def count(events, item, window_h):
        return sum(1 for i, t in events if i == item and end - window_h * HOUR <= t < end)

    snapshot = ItemWindowCounts(
        clicks={w: np.array([count(clicks, i, w) for i in range(4)]) for w in (6, 24, 48)},
        inviews=np.array([count(inviews, i, 24) for i in range(4)]),
    )
    ctx = FeatureContext(catalog=_catalog(), user_log=_index([]), item_window_counts=snapshot,
                         config=FeatureConfig(item_lag_s=lag))
    from_snapshot = compute_feature_columns(ctx, _req(), groups=["popularity"])

    assert list(from_snapshot) == list(from_logs)
    for name in from_logs:
        assert from_snapshot[name].dtype == np.float32
        assert np.array_equal(from_snapshot[name], from_logs[name])
    assert from_logs["pop_inviews_24h"].sum() > 0 and from_logs["pop_clicks_6h"].sum() > 0


def test_a_snapshot_without_a_configured_window_is_an_error():
    snapshot = ItemWindowCounts(clicks={6: np.zeros(4), 24: np.zeros(4)}, inviews=np.zeros(4))
    ctx = FeatureContext(catalog=_catalog(), user_log=_index([]), item_window_counts=snapshot)

    with pytest.raises(ValueError, match="48"):
        compute_feature_columns(ctx, _req(), groups=["popularity"])


def test_compute_features_is_the_dataframe_of_the_columns():
    ctx = _pop_ctx([(1, T - 5)], [(1, T - 5)])
    cols = compute_feature_columns(ctx, _req(), groups=["recency", "popularity"])
    frame = compute_features(ctx, _req(), groups=["recency", "popularity"])

    assert list(frame.columns) == list(cols)
    for name in cols:
        assert np.array_equal(frame[name].to_numpy(), cols[name])


# ------------------------------------------------------------------ 세션 나누기
def test_sessionize_starts_a_new_session_only_when_the_gap_is_exceeded():
    user = [1, 1, 1, 1, 2, 2]
    time = [T, T + 1800, T + 1800 + 1801, T + 1800 + 1801 + 60, T + 10, T + 5000]

    sessions = sessionize(user, time, gap_s=1800)

    assert sessions.tolist() == [0, 0, 1, 1, 2, 3]  # 정확히 1800초는 같은 세션, 1801초는 새 세션


def test_sessionize_returns_ids_in_input_order_whatever_the_event_order():
    rng = np.random.default_rng(0)
    user = rng.integers(0, 5, 200)
    time = T + rng.integers(0, 6 * HOUR, 200)
    perm = rng.permutation(200)

    a = sessionize(user, time)
    b = sessionize(user[perm], time[perm])

    assert np.array_equal(a[perm], b)
    assert sessionize([], []).tolist() == []


def test_request_sessions_joins_the_last_session_only_within_the_gap():
    ev_user = np.array([1, 1, 1, 2])
    ev_time = np.array([T, T + 600, T + 10_000, T + 50])
    ev_session = sessionize(ev_user, ev_time, gap_s=1800)  # [0, 0, 1, 2]

    got = request_sessions(
        ev_user, ev_time, ev_session,
        req_user=[1, 1, 1, 1, 2, 3, 1],
        req_time=[T + 601, T + 600 + 1800, T + 600 + 1801, T + 10_001, T + 50, T + 60, T],
        gap_s=1800,
    )

    # 직후 / 정확히 gap / gap 초과 / 다음 세션 / 같은 초의 이벤트는 아직 없음 / 이벤트 없는 유저 / 첫 이벤트와 같은 초
    assert got.tolist() == [0, 0, -1, 1, -1, -1, -1]


def test_sessionized_log_reproduces_session_features_of_an_explicit_session_id():
    """세션 번호를 직접 준 로그와, 시각으로 나눈 로그가 같은 세션 피처를 낸다(세션 경계가 같을 때)."""
    cat = _catalog()
    ev_user = np.array([7, 7, 7])
    ev_time = np.array([T - 5 * HOUR, T - 20 * 60, T - 5 * 60])
    ev_item = np.array([2, 0, 1])
    sess = sessionize(ev_user, ev_time)
    current = request_sessions(ev_user, ev_time, sess, [7], [T])
    by_time = FeatureContext(catalog=cat, user_log=EventIndex(ev_user, ev_time, ev_item),
                             session_log=EventIndex(sess, ev_time, ev_item))
    explicit = FeatureContext(catalog=cat, user_log=EventIndex(ev_user, ev_time, ev_item),
                              session_log=_index([(42, T - 20 * 60, 0), (42, T - 5 * 60, 1)]))

    a = compute_feature_columns(by_time, _req(session=int(current[0])), groups=["short_term"])
    b = compute_feature_columns(explicit, _req(session=42), groups=["short_term"])

    assert a["sess_len"].tolist() == [2, 2, 2, 2]
    for name in a:
        assert np.array_equal(a[name], b[name], equal_nan=True)
