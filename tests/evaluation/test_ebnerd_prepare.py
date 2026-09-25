"""EB-NeRD 과제 구성(P1/P2/네거티브 샘플링)의 point-in-time 성질. demo 데이터가 없으면(CI) skip."""
import numpy as np
import pytest

from evaluation.recsys.ebnerd.loaders import ebnerd_root
from recsys_core import DAY, HOUR, expand_ranges

DEMO = ebnerd_root() / "ebnerd_demo"
pytestmark = pytest.mark.skipif(not (DEMO / "articles.parquet").exists(),
                                reason="EB-NeRD demo 데이터 없음 (로컬 전용)")


@pytest.fixture(scope="module")
def bench():
    from evaluation.recsys.ebnerd.prepare import load_bench
    return load_bench(DEMO, fake_dim=8)


@pytest.fixture(scope="module")
def windows(bench):
    from evaluation.recsys.ebnerd.prepare import protocol_windows
    return protocol_windows(bench)


def _sample(bench, split, window, n=300, seed=0):
    from evaluation.recsys.ebnerd.prepare import impressions_in
    idx = impressions_in(bench.imps[split], window)
    return np.sort(np.random.default_rng(seed).choice(idx, size=min(n, len(idx)), replace=False))


def _seen_pairs(bench, split, req):
    log = bench.ctx[split].user_log
    lo, hi = log.bounds(req.user, 0, req.profile_cutoff)
    rows, pos = expand_ranges(lo, hi)
    return set(zip(rows.tolist(), log.item[pos].tolist()))


def test_windows_are_ordered_and_disjoint(windows):
    assert windows["fit"][0] < windows["fit"][1] == windows["es"][0] < windows["es"][1] == windows["test"][0]
    assert windows["es"][1] - windows["es"][0] == DAY


def test_p2_pool_is_published_within_window_and_excludes_seen(bench, windows):
    from evaluation.recsys.ebnerd.prepare import p2_task
    idx = _sample(bench, "validation", windows["test"])
    task = p2_task(bench, "validation", idx, window_h=48, exclude_seen=True)
    req = task.req
    t = req.time[req.pair_req]
    pub = bench.catalog.pub_time[req.cand_item]
    assert np.all(pub <= t) and np.all(pub >= t - 48 * HOUR)
    seen = _seen_pairs(bench, "validation", req)
    assert not any((r, i) in seen for r, i in zip(req.pair_req.tolist(), req.cand_item.tolist()))
    # 라벨은 해당 노출의 클릭 기사와 정확히 일치해야 한다.
    imp = bench.imps["validation"].subset(idx)
    for r in range(0, len(idx), 50):
        clicked = set(bench.catalog.index_of(imp.clicked_article[imp.clicked_ptr[r]:imp.clicked_ptr[r + 1]]).tolist())
        pool = req.cand_item[req.cand_ptr[r]:req.cand_ptr[r + 1]]
        lab = task.labels[req.cand_ptr[r]:req.cand_ptr[r + 1]]
        assert set(pool[lab].tolist()) == clicked & set(pool.tolist())
    assert np.all(task.n_pos_total >= np.bincount(req.pair_req[task.labels], minlength=req.n))


def test_random_negatives_follow_team_rules(bench, windows):
    from evaluation.recsys.ebnerd.prepare import random_negative_task
    idx = _sample(bench, "train", windows["fit"])
    task = random_negative_task(bench, "train", idx, np.random.default_rng(0), n_neg=5, pool_days=7)
    req = task.req
    assert np.all(req.n_candidates <= 6)
    first = req.cand_ptr[:-1]
    assert task.labels[first].all() and task.labels.sum() == req.n
    neg = ~task.labels
    t = req.time[req.pair_req][neg]
    pub = bench.catalog.pub_time[req.cand_item[neg]]
    assert np.all(pub <= t) and np.all(pub >= t - 7 * DAY)
    user_items = set(bench.user_items["train"].tolist())
    keys = (req.user[req.pair_req][neg] << 32) | req.cand_item[neg]
    assert not any(k in user_items for k in keys.tolist())


def test_pool_negatives_exclude_only_past_reads_and_current_clicks(bench, windows):
    from evaluation.recsys.ebnerd.prepare import pool_negative_task
    idx = _sample(bench, "train", windows["fit"])
    task = pool_negative_task(bench, "train", idx, np.random.default_rng(0), n_neg=10, include_inview=True)
    req = task.req
    seen = _seen_pairs(bench, "train", req)
    neg = ~task.labels
    pairs = list(zip(req.pair_req[neg].tolist(), req.cand_item[neg].tolist()))
    imp = bench.imps["train"].subset(idx)
    inview = set()
    for r in range(len(idx)):
        for a in bench.catalog.index_of(imp.inview_article[imp.inview_ptr[r]:imp.inview_ptr[r + 1]]).tolist():
            inview.add((r, a))
    pool_negs = [p for p in pairs if p not in inview]
    assert pool_negs and not any(p in seen for p in pool_negs)
    t = req.time[req.pair_req]
    pool_mask = np.array([p not in inview for p in zip(req.pair_req.tolist(), req.cand_item.tolist())]) & neg
    pub = bench.catalog.pub_time[req.cand_item[pool_mask]]
    assert np.all(pub <= t[pool_mask]) and np.all(pub >= t[pool_mask] - 48 * HOUR)
    # 한 요청 안에서 후보가 중복되지 않는다.
    keys = req.pair_req * len(bench.catalog) + req.cand_item
    assert len(np.unique(keys)) == len(keys)
    assert (np.bincount(req.pair_req[task.labels], minlength=req.n) >= 1).all()
