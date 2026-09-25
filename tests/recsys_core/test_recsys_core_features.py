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
    compute_features,
    window_vectors,
)

T = 1_700_000_000  # 요청 시각 기준점 (epoch 초)


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _catalog():
    emb = np.stack([_unit([1, 0, 0]), _unit([0, 1, 0]), _unit([0, 0, 1]), _unit([1, 1, 0])])
    return ItemCatalog(
        ids=[10, 11, 12, 13],
        emb=emb,
        pub_time=[T - 2 * HOUR, T - 30 * HOUR, T - 10 * DAY, T + HOUR],
        category=[0, 1, 1, 2],
        n_categories=3,
    )


def _ctx(user_events, session_events=(), click_events=(), inview_events=(), static=None, **cfg):
    def idx(ev):
        ev = list(ev)
        if not ev:
            return EventIndex([], [], [])
        k, t, i = zip(*ev)
        return EventIndex(k, t, i)

    items_clicks = [(i, t, i) for (i, t) in click_events]
    items_inviews = [(i, t, i) for (i, t) in inview_events]
    return FeatureContext(
        catalog=_catalog(),
        user_log=idx(user_events),
        session_log=idx(session_events),
        item_clicks=idx(items_clicks),
        item_inviews=idx(items_inviews),
        static_categories=static,
        config=FeatureConfig(**cfg),
    )


def _req(cands, user=7, t=T, session=None, cutoff=None):
    return Requests(
        user=[user], time=[t], cand_ptr=[0, len(cands)], cand_item=cands,
        session=None if session is None else [session],
        profile_cutoff=None if cutoff is None else [cutoff],
    )


def test_recency_features_hand_computed():
    f = compute_features(_ctx([]), _req([0, 1, 2, 3]), groups=["recency"])
    assert f["hours_since_pub"].tolist() == pytest.approx([2.0, 30.0, 240.0, 0.0])  # 미래 발행은 0으로 절단
    assert f["is_fresh_24h"].tolist() == [1, 0, 0, 1]
    assert f["is_fresh_7d"].tolist() == [1, 1, 0, 1]


def test_history_cosine_uses_decayed_weights():
    # 1일 전 item0([1,0,0]), 8일 전 item1([0,1,0]) -> 가중치 0.5^(1/7), 0.5^(8/7)
    ctx = _ctx([(7, T - DAY, 0), (7, T - 8 * DAY, 1)])
    f = compute_features(ctx, _req([0, 1, 3]), groups=["history"])
    w0, w1 = 0.5 ** (1 / 7), 0.5 ** (8 / 7)
    h = _unit([w0, w1, 0])
    expected = [h[0], h[1], float(h @ _unit([1, 1, 0]))]
    assert f["hist_cos"].tolist() == pytest.approx(expected, abs=1e-6)
    assert f["hist_len"].tolist() == [2, 2, 2]


def test_future_and_same_instant_events_never_leak_into_features():
    past = [(7, T - DAY, 0)]
    base_ctx = _ctx(past, session_events=[(5, T - 60, 0)], click_events=[(1, T - HOUR)],
                    inview_events=[(1, T - HOUR)])
    # t 이후(및 정확히 t)의 이벤트를 모든 로그에 추가해도 피처는 그대로여야 한다.
    future = [(7, T, 1), (7, T + 5, 2), (7, T + DAY, 3)]
    leak_ctx = _ctx(past + future,
                    session_events=[(5, T - 60, 0), (5, T, 1), (5, T + 60, 2)],
                    click_events=[(1, T - HOUR), (1, T), (1, T + HOUR), (2, T + 1)],
                    inview_events=[(1, T - HOUR), (1, T), (2, T + 1)])
    groups = ["recency", "history", "category", "popularity", "short_term"]
    a = compute_features(base_ctx, _req([0, 1, 2, 3], session=5), groups=groups)
    b = compute_features(leak_ctx, _req([0, 1, 2, 3], session=5), groups=groups)
    assert a.equals(b)
    assert a["pop_clicks_6h"].tolist() == [0, 1, 0, 0]


def test_profile_cutoff_hides_user_events_after_cutoff_but_not_item_popularity():
    ctx = _ctx([(7, T - 3 * DAY, 0), (7, T - HOUR, 1)], click_events=[(1, T - HOUR)],
               inview_events=[(1, T - HOUR)])
    daily = compute_features(ctx, _req([0, 1], cutoff=T - DAY), groups=["history", "popularity"])
    live = compute_features(ctx, _req([0, 1]), groups=["history", "popularity"])
    assert daily["hist_len"].tolist() == [1, 1]
    assert live["hist_len"].tolist() == [2, 2]
    assert daily["hist_cos"].tolist() == pytest.approx([1.0, 0.0], abs=1e-6)
    assert daily["pop_clicks_24h"].tolist() == live["pop_clicks_24h"].tolist() == [0, 1]


def test_profile_cutoff_in_the_future_is_rejected():
    with pytest.raises(ValueError):
        _req([0], cutoff=T + 1)


def test_short_term_and_session_windows():
    user_events = [(7, T - 30 * HOUR, 2), (7, T - 2 * HOUR, 0), (7, T - HOUR, 1)]
    ctx = _ctx(user_events, session_events=[(5, T - HOUR, 1)])
    f = compute_features(ctx, _req([0, 1, 2], session=5), groups=["short_term"])
    s = _unit([1, 1, 0])  # 24h 창 안의 item0+item1 (감쇠 없음)
    assert f["short_cos"].tolist() == pytest.approx([s[0], s[1], 0.0], abs=1e-6)
    assert f["short_len"].tolist() == [2, 2, 2]
    assert f["sess_cos"].tolist() == pytest.approx([0.0, 1.0, 0.0], abs=1e-6)
    assert f["sess_len"].tolist() == [1, 1, 1]
    assert f["hours_since_last_event"].tolist() == pytest.approx([1.0, 1.0, 1.0])


def test_cold_user_gets_zero_similarity_and_nan_gap():
    f = compute_features(_ctx([]), _req([0, 1]), groups=["history", "short_term", "category"])
    assert f["hist_cos"].tolist() == [0, 0]
    assert f["hist_len"].tolist() == [0, 0]
    assert f["cat_share"].tolist() == [0, 0]
    assert np.isnan(f["hours_since_last_event"]).all()


def test_category_share_hand_computed():
    # category: item0->0, item1->1, item2->1, item3->2. 과거 3건 중 카테고리1이 2건.
    ctx = _ctx([(7, T - 3 * HOUR, 1), (7, T - 2 * HOUR, 2), (7, T - HOUR, 0)])
    f = compute_features(ctx, _req([0, 1, 3]), groups=["category"])
    assert f["cat_share"].tolist() == pytest.approx([1 / 3, 2 / 3, 0.0])


def test_popularity_windows_and_ctr():
    clicks = [(0, T - 5 * HOUR), (0, T - 20 * HOUR), (0, T - 40 * HOUR), (0, T - 50 * HOUR)]
    inviews = [(0, T - 5 * HOUR), (0, T - 6 * HOUR), (0, T - 20 * HOUR), (0, T - 23 * HOUR)]
    f = compute_features(_ctx([], click_events=clicks, inview_events=inviews), _req([0, 1]), groups=["popularity"])
    assert f["pop_clicks_6h"].tolist() == [1, 0]
    assert f["pop_clicks_24h"].tolist() == [2, 0]
    assert f["pop_clicks_48h"].tolist() == [3, 0]
    assert f["pop_inviews_24h"].tolist() == [4, 0]
    assert f["pop_ctr_24h"].tolist() == pytest.approx([0.5, 0.0])


def test_team_category_proxy_uses_static_matrix():
    static = (np.array([7]), np.array([[False, True, True]]))
    f = compute_features(_ctx([], static=static), _req([0, 1, 3]), groups=["team_category"])
    assert f["cat_match_count"].tolist() == [0, 1, 1]
    assert f["user_ncat"].tolist() == [2, 2, 2]
    unknown = compute_features(_ctx([], static=static), _req([1], user=8), groups=["team_category"])
    assert unknown["cat_match_count"].tolist() == [0]


def test_chunking_does_not_change_results():
    rng = np.random.default_rng(0)
    n_users, n_req = 20, 50
    user_events = [(int(rng.integers(n_users)), T - int(rng.integers(1, 20 * DAY)), int(rng.integers(4)))
                   for _ in range(300)]
    users = rng.integers(n_users, size=n_req)
    times = T + rng.integers(0, DAY, size=n_req)
    ncand = rng.integers(1, 5, size=n_req)
    ptr = np.concatenate([[0], np.cumsum(ncand)])
    cands = rng.integers(0, 4, size=ptr[-1])
    req = Requests(user=users, time=times, cand_ptr=ptr, cand_item=cands)
    big = compute_features(_ctx(user_events), req, groups=["history", "category", "short_term"])
    small = compute_features(_ctx(user_events, request_chunk=3, event_chunk=7, pair_chunk=5), req,
                             groups=["history", "category", "short_term"])
    np.testing.assert_allclose(big.to_numpy(), small.to_numpy(), rtol=1e-5, atol=1e-6)


def test_window_vectors_zero_for_empty_and_unit_norm_otherwise():
    idx = EventIndex([1, 1], [T - 10, T - 5], [0, 1])
    v, cnt = window_vectors(idx, _catalog().emb, [1, 2], 0, T)
    assert cnt.tolist() == [2, 0]
    assert np.linalg.norm(v[0]) == pytest.approx(1.0)
    assert not v[1].any()


def test_unknown_group_is_rejected():
    with pytest.raises(ValueError):
        compute_features(_ctx([]), _req([0]), groups=["nope"])
