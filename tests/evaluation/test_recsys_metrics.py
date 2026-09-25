import math

import numpy as np
import pytest

from evaluation.recsys.metrics import (
    catalog_coverage,
    category_entropy,
    cluster_bootstrap,
    group_auc,
    intra_list_diversity,
    paired_bootstrap_diff,
    rank_within_groups,
    ranking_metrics,
    topk_items,
)

L2_3 = 1 / math.log2(3)


def test_rank_within_groups_descending_per_group():
    ranks = rank_within_groups(np.array([3.0, 1.0, 2.0, 5.0, 4.0]), np.array([0, 3, 5]))
    assert ranks.tolist() == [0, 2, 1, 0, 1]


def test_hand_computed_single_impression():
    # 순위: 0.9(음) 0.8(양) 0.5(양) 0.1(음)
    m = ranking_metrics(np.array([0.9, 0.8, 0.1, 0.5]), np.array([0, 1, 0, 1]), np.array([0, 4]), ks=(1, 3))
    assert m["mrr"][0] == pytest.approx(0.5)
    assert m["auc"][0] == pytest.approx(0.5)            # (0.8>0.1)+(0.5>0.1) = 2/4
    ideal = 1 + L2_3
    assert m["ndcg@3"][0] == pytest.approx((L2_3 + 0.5) / ideal)
    assert m["ndcg@1"][0] == 0.0
    assert m["recall@1"][0] == 0.0
    assert m["recall@3"][0] == 1.0


def test_auc_counts_ties_as_half():
    auc = group_auc(np.array([1.0, 1.0, 0.0]), np.array([1, 0, 0]), np.array([0, 3]))
    assert auc[0] == pytest.approx(0.75)


def test_groups_without_positives_or_negatives_are_nan():
    m = ranking_metrics(np.array([0.2, 0.1, 0.5]), np.array([0, 0, 1]), np.array([0, 2, 3]), ks=(5,))
    assert np.isnan(m["auc"]).all()
    assert np.isnan(m["mrr"][0]) and m["mrr"][1] == 1.0
    assert np.isnan(m["ndcg@5"][0]) and m["ndcg@5"][1] == 1.0


def test_n_pos_total_penalises_answers_missing_from_the_pool():
    m = ranking_metrics(np.array([0.9, 0.1]), np.array([1, 0]), np.array([0, 2]), ks=(5,),
                        n_pos_total=np.array([2]))
    assert m["recall@5"][0] == pytest.approx(0.5)
    assert m["ndcg@5"][0] == pytest.approx(1 / (1 + L2_3))


def test_rank_metrics_break_ties_randomly_but_deterministically():
    scores = np.zeros(10)
    labels = np.zeros(10, dtype=int)
    labels[0] = 1
    ptr = np.array([0, 10])
    a = ranking_metrics(scores, labels, ptr, seed=1)["mrr"]
    b = ranking_metrics(scores, labels, ptr, seed=1)["mrr"]
    assert a == b
    # 입력 순서상 첫 번째라는 이유만으로 항상 1위가 되지는 않는다.
    mrrs = [ranking_metrics(scores, labels, ptr, seed=s)["mrr"][0] for s in range(20)]
    assert min(mrrs) < 1.0


def test_topk_items_pads_short_groups():
    out = topk_items(np.array([0.1, 0.9, 0.5]), np.array([10, 11, 12]), np.array([0, 2, 3]), k=2)
    assert out.tolist() == [[11, 10], [12, -1]]


def test_diversity_entropy_and_coverage_hand_computed():
    emb = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])
    lists = np.array([[0, 1, -1], [0, 2, -1], [1, -1, -1]])
    ild = intra_list_diversity(lists, emb)
    assert ild[0] == pytest.approx(1.0)
    assert ild[1] == pytest.approx(0.0)
    assert np.isnan(ild[2])
    ent = category_entropy(lists, np.array([0, 1, 0]), 2)
    assert ent[0] == pytest.approx(1.0)
    assert ent[1] == pytest.approx(0.0)
    assert catalog_coverage(lists, np.array([0, 1, 2, 3])) == pytest.approx(0.75)


def test_cluster_bootstrap_resamples_clusters_not_rows():
    # 유저 A는 노출 100개가 전부 1.0, 유저 B는 1개가 0.0: 행 단위 CI는 매우 좁지만
    # 유저 단위 재표집이면 A가 빠지는 표본이 있으므로 하한이 0 근처까지 내려간다.
    values = np.array([1.0] * 100 + [0.0])
    clusters = np.array(["A"] * 100 + ["B"])
    ci = cluster_bootstrap(values, clusters, n_boot=500, seed=0)
    assert ci["mean"] == pytest.approx(100 / 101)
    assert ci["n_clusters"] == 2 and ci["n"] == 101
    assert ci["lo"] < 0.5


def test_cluster_bootstrap_ignores_nan_and_is_exact_for_constants():
    ci = cluster_bootstrap(np.array([0.3, np.nan, 0.3]), np.array([1, 2, 3]), n_boot=200)
    assert ci["n"] == 2
    assert ci["lo"] == pytest.approx(0.3) and ci["hi"] == pytest.approx(0.3)


def test_paired_bootstrap_diff_sign():
    a = np.array([0.6, 0.7, 0.8, 0.9])
    b = a - 0.1
    ci = paired_bootstrap_diff(a, b, np.array([1, 2, 3, 4]), n_boot=200)
    assert ci["mean"] == pytest.approx(0.1)
    assert ci["lo"] == pytest.approx(0.1)


def test_group_auc_matches_sklearn_on_random_groups_with_ties():
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(0)
    sizes = rng.integers(2, 15, size=40)
    ptr = np.concatenate([[0], np.cumsum(sizes)])
    scores = rng.integers(0, 4, size=ptr[-1]).astype(float)  # 동점이 많도록 정수 점수
    labels = rng.random(ptr[-1]) < 0.3
    ours = group_auc(scores, labels, ptr)
    for g in range(len(sizes)):
        y, s = labels[ptr[g]:ptr[g + 1]], scores[ptr[g]:ptr[g + 1]]
        if y.all() or not y.any():
            assert np.isnan(ours[g])
        else:
            assert ours[g] == pytest.approx(roc_auc_score(y, s))
