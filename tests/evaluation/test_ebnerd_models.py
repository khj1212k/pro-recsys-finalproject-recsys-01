import numpy as np
import pandas as pd

from evaluation.recsys.ebnerd.models import ModelSpec, _order_and_groups, train
from evaluation.recsys.ebnerd.prepare import RankTask
from evaluation.recsys.metrics import ranking_metrics
from recsys_core import Requests


def _positive_first_task(n_groups=1500, size=6, seed=0, n_features=3):
    """정답이 항상 쿼리 맨 앞에 오는(무작위 네거티브 과제와 같은) 합성 데이터.
    피처마다 약한 신호: f_j = 0.5 * label + N(0, 1)."""
    rng = np.random.default_rng(seed)
    labels = np.zeros((n_groups, size), dtype=bool)
    labels[:, 0] = True
    feats = {f"f{j}": (0.5 * labels + rng.normal(0, 1.0, size=labels.shape)).ravel() for j in range(n_features)}
    ptr = np.arange(0, n_groups * size + 1, size)
    req = Requests(user=np.arange(n_groups), time=np.zeros(n_groups, dtype=np.int64), cand_ptr=ptr,
                   cand_item=np.zeros(n_groups * size, dtype=np.int64))
    task = RankTask(req=req, labels=labels.ravel(), group_user=np.arange(n_groups), imp_index=np.arange(n_groups),
                    extra={"age": np.zeros(n_groups), "gender": np.zeros(n_groups)})
    return task, pd.DataFrame(feats)


def test_order_shuffles_rows_inside_groups_only():
    task, _ = _positive_first_task(n_groups=50)
    order, sizes = _order_and_groups(task, "request", np.random.default_rng(0))
    assert sizes.tolist() == [6] * 50
    assert sorted(order.tolist()) == list(range(300))
    assert np.array_equal(task.req.pair_req[order], np.repeat(np.arange(50), 6))
    assert not task.labels[order][::6].all()  # 정답이 더는 항상 첫 자리에 있지 않다


def test_early_stopping_score_is_not_inflated_by_positive_first_ties():
    # LightGBM NDCG는 동점을 입력 순서대로 두므로 정답이 맨 앞이면 트리가 적은 초반
    # 반복의 점수가 부풀어 early stopping이 1회차를 고른다. 쿼리 내 셔플 후에는
    # 보고된 ES 점수가 무작위 동점 처리 nDCG와 같아야 한다.
    spec = ModelSpec("t", "random_neg", "lambdarank", "request", ["f0", "f1", "f2"])
    fit, es = _positive_first_task(seed=0), _positive_first_task(seed=1)
    m = train(spec, fit, es, seed=0, num_threads=2)
    scores = m.predict(es[1], es[0])
    unbiased = np.mean([np.nanmean(ranking_metrics(scores, es[0].labels, es[0].req.cand_ptr, ks=(10,),
                                                   seed=s)["ndcg@10"]) for s in range(5)])
    assert abs(m.best_score - unbiased) < 0.005
    assert m.best_iteration > 1


def test_impression_groups_merge_click_requests_of_the_same_impression():
    # 무작위 네거티브 과제는 클릭마다 요청 하나: 노출 7의 클릭 2건은 한 쿼리로 묶여야 한다.
    req = Requests(user=np.array([1, 1, 2]), time=np.zeros(3, dtype=np.int64), cand_ptr=np.array([0, 3, 6, 8]),
                   cand_item=np.arange(8))
    task = RankTask(req=req, labels=np.array([1, 0, 0, 1, 0, 0, 1, 0], bool), group_user=np.array([1, 1, 2]),
                    imp_index=np.array([7, 7, 9]), extra={})
    order, sizes = _order_and_groups(task, "impression", np.random.default_rng(0))
    assert sizes.tolist() == [6, 2]
    assert set(order[:6].tolist()) == set(range(6)) and set(order[6:].tolist()) == {6, 7}
