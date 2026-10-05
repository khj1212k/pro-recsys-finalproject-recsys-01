"""BGE-M3 임베딩이 덴마크어 기사에서도 의미를 담는지 보는 값싼 점검: 카테고리 kNN leave-one-out.

코사인 최근접 k개(자기 자신 제외)의 다수결 카테고리가 실제 카테고리와 맞는 비율을
다수 클래스 비율과 나란히 보고한다. 동률은 가장 가까운 이웃이 속한 카테고리로 깬다.
노름이 0인 행(임베딩 없음)은 질의와 이웃 양쪽에서 뺀다.
"""
from __future__ import annotations

import numpy as np


def knn_category_accuracy(emb: np.ndarray, labels: np.ndarray, k: int = 10, chunk: int = 2048) -> dict:
    emb = np.asarray(emb, dtype=np.float32)
    norms = np.linalg.norm(emb, axis=1)
    keep = norms > 0
    x = emb[keep] / norms[keep, None]
    _, lab = np.unique(np.asarray(labels)[keep], return_inverse=True)
    n, n_cls = len(x), int(lab.max()) + 1 if len(lab) else 0
    k = min(k, n - 1)
    correct = np.zeros(n, dtype=bool)
    for s in range(0, n, chunk):
        e = min(s + chunk, n)
        sims = x[s:e] @ x.T
        sims[np.arange(e - s), np.arange(s, e)] = -np.inf
        part = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        order = np.argsort(-np.take_along_axis(sims, part, axis=1), axis=1, kind="stable")
        nbr = lab[np.take_along_axis(part, order, axis=1)]
        rows = np.arange(e - s)
        counts = np.zeros((e - s, n_cls), dtype=np.int64)
        nearest_bonus = np.zeros((e - s, n_cls), dtype=np.int64)
        for j in range(k):
            np.add.at(counts, (rows, nbr[:, j]), 1)
            nearest_bonus[rows, nbr[:, j]] = np.maximum(nearest_bonus[rows, nbr[:, j]], k - j)
        pred = np.argmax(counts * (k + 1) + nearest_bonus, axis=1)
        correct[s:e] = pred == lab[s:e]
    majority = float(np.bincount(lab).max() / n) if n else float("nan")
    return {"k": int(k), "n": int(n), "n_categories": n_cls, "accuracy": float(correct.mean()) if n else float("nan"),
            "majority_baseline": majority, "per_item_correct": correct}
