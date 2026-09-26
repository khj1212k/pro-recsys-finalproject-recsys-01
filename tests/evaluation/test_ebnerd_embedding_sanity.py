import numpy as np

from evaluation.recsys.ebnerd.embedding_sanity import knn_category_accuracy


def _unit(rows):
    x = np.asarray(rows, dtype=np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def test_knn_leave_one_out_hand_computed():
    # 두 무리(0: x축 근처, 1: y축 근처) + 1번 무리 안에 섞인 0 라벨 하나.
    emb = _unit([[1, 0.1], [1, 0.2], [1, 0.0], [0.1, 1], [0.2, 1], [0.0, 1]])
    labels = np.array([0, 0, 0, 1, 1, 0])
    res = knn_category_accuracy(emb, labels, k=1, chunk=2)
    # 1-NN(자기 자신 제외, 각도 기준): 0->1, 1->0, 2->0, 3->4, 4->3, 5->3.
    # 마지막 점(라벨 0)만 최근접이 3(라벨 1)이라 오답.
    assert res["accuracy"] == 5 / 6
    assert res["majority_baseline"] == 4 / 6
    assert res["n"] == 6


def test_knn_majority_vote_ignores_self_and_breaks_ties_by_nearest():
    emb = _unit([[1, 0], [1, 0.05], [1, 0.3], [0, 1]])
    labels = np.array([0, 1, 1, 0])
    # 0번의 이웃 2개(1, 2)는 둘 다 라벨 1 -> 오답. 1번 이웃(0, 2)은 1:1 동률 -> 더 가까운 0(라벨 0) -> 오답.
    res = knn_category_accuracy(emb, labels, k=2, chunk=3)
    per_item = res["per_item_correct"]
    assert per_item[0] == 0 and per_item[1] == 0


def test_knn_skips_zero_vectors():
    emb = np.array([[1, 0], [1, 0.1], [0, 0], [0, 1]], dtype=np.float32)
    emb[:2] /= np.linalg.norm(emb[:2], axis=1, keepdims=True)
    labels = np.array([0, 0, 1, 1])
    res = knn_category_accuracy(emb, labels, k=1)
    assert res["n"] == 3
