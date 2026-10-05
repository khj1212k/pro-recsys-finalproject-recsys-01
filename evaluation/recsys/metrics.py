"""그룹(노출/요청) 단위 랭킹 지표와 유저 클러스터 부트스트랩 - numpy 전용.

입력은 CSR 형태: ptr(길이 G+1)로 나뉜 scores/labels. 모든 지표는 그룹별 값을 돌려주고,
평균·신뢰구간은 cluster_bootstrap으로 따로 계산한다(노출 평균을 먼저 내고 유저 단위로
재표집해야 헤비 유저가 CI를 과하게 좁히지 않는다).

동점 처리: AUC는 Mann-Whitney 정의대로 동점 0.5, 순위 지표(MRR/nDCG/Recall)는
seed 고정 무작위 순서로 동점을 깬다(인기도 0인 후보가 많은 베이스라인에서 입력 순서가
결과를 좌우하지 않도록).
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from recsys_core.candidates import group_ids, rank_within_groups  # noqa: F401 (재노출)


def group_auc(scores: np.ndarray, labels: np.ndarray, ptr: np.ndarray) -> np.ndarray:
    """그룹별 AUC(동점 0.5). 양성 또는 음성이 없는 그룹은 nan."""
    scores = np.asarray(scores, dtype=np.float64)
    labels = np.asarray(labels).astype(bool)
    ptr = np.asarray(ptr, dtype=np.int64)
    n_groups = len(ptr) - 1
    out = np.full(n_groups, np.nan)
    if len(scores) == 0:
        return out
    g = group_ids(ptr)
    order = np.lexsort((scores, g))
    s, lab, gg = scores[order], labels[order], g[order]
    neg = (~lab).astype(np.float64)
    new_block = np.ones(len(s), dtype=bool)
    new_block[1:] = (gg[1:] != gg[:-1]) | (s[1:] != s[:-1])
    block_start = np.flatnonzero(new_block)
    block_id = np.cumsum(new_block) - 1
    excl = np.concatenate([[0.0], np.cumsum(neg)])
    neg_below = excl[block_start] - excl[ptr[gg[block_start]]]
    neg_in_block = np.add.reduceat(neg, block_start)
    contrib = neg_below[block_id] + 0.5 * neg_in_block[block_id]
    npos = np.bincount(gg, weights=lab.astype(float), minlength=n_groups)
    nneg = np.bincount(gg, weights=neg, minlength=n_groups)
    num = np.bincount(gg[lab], weights=contrib[lab], minlength=n_groups)
    ok = (npos > 0) & (nneg > 0)
    out[ok] = num[ok] / (npos[ok] * nneg[ok])
    return out


def _idcg_table(k: int, max_pos: int) -> np.ndarray:
    disc = 1.0 / np.log2(np.arange(k) + 2.0)
    table = np.concatenate([[0.0], np.cumsum(disc)])
    return table[np.minimum(np.arange(max_pos + 1), k)]


def ranking_metrics(scores: np.ndarray, labels: np.ndarray, ptr: np.ndarray,
                    ks: Sequence[int] = (5, 10), seed: int = 0,
                    n_pos_total: Optional[np.ndarray] = None,
                    with_auc: bool = True) -> dict[str, np.ndarray]:
    """그룹별 AUC, MRR, nDCG@k, Recall@k.

    n_pos_total: 후보 풀 밖(예: 48h 창 밖, 이미 본 아이템 필터로 제거)으로 빠진 정답까지
    포함한 그룹별 정답 수. 주면 nDCG의 이상적 DCG와 Recall 분모에 이 값을 써서, 풀에서
    놓친 정답이 지표에 벌점으로 반영된다. 생략하면 풀 안 정답 수.
    """
    labels = np.asarray(labels).astype(bool)
    ptr = np.asarray(ptr, dtype=np.int64)
    n_groups = len(ptr) - 1
    g = group_ids(ptr)
    ranks = rank_within_groups(scores, ptr, seed=seed)
    npos_in = np.bincount(g, weights=labels.astype(float), minlength=n_groups)
    npos = npos_in if n_pos_total is None else np.asarray(n_pos_total, dtype=np.float64)
    out: dict[str, np.ndarray] = {}
    if with_auc:
        out["auc"] = group_auc(scores, labels, ptr)
    first = np.full(n_groups, np.inf)
    np.minimum.at(first, g[labels], ranks[labels])
    mrr = np.where(np.isfinite(first), 1.0 / (first + 1.0), 0.0)
    out["mrr"] = np.where(npos > 0, mrr, np.nan)
    gain = 1.0 / np.log2(ranks + 2.0)
    for k in ks:
        hit = labels & (ranks < k)
        dcg = np.bincount(g[hit], weights=gain[hit], minlength=n_groups)
        idcg = _idcg_table(k, int(npos.max()) if len(npos) else 0)[npos.astype(np.int64)]
        with np.errstate(invalid="ignore", divide="ignore"):
            out[f"ndcg@{k}"] = np.where(npos > 0, dcg / idcg, np.nan)
            out[f"recall@{k}"] = np.where(npos > 0, np.bincount(g[hit], minlength=n_groups) / npos, np.nan)
    return out


def topk_items(scores: np.ndarray, items: np.ndarray, ptr: np.ndarray, k: int, seed: int = 0) -> np.ndarray:
    """그룹별 상위 k개 아이템 [G, k] (부족하면 -1로 채움)."""
    ptr = np.asarray(ptr, dtype=np.int64)
    ranks = rank_within_groups(scores, ptr, seed=seed)
    g = group_ids(ptr)
    out = np.full((len(ptr) - 1, k), -1, dtype=np.int64)
    sel = ranks < k
    out[g[sel], ranks[sel]] = np.asarray(items)[sel]
    return out


def intra_list_diversity(lists: np.ndarray, emb: np.ndarray, chunk: int = 4096) -> np.ndarray:
    """목록 내 아이템 쌍의 평균 코사인 거리(1-cos). 아이템이 2개 미만이면 nan."""
    out = np.full(len(lists), np.nan)
    for s in range(0, len(lists), chunk):
        block = lists[s:s + chunk]
        valid = block >= 0
        e = emb[np.where(valid, block, 0)] * valid[..., None]
        sim = np.einsum("gid,gjd->gij", e, e)
        n = valid.sum(axis=1).astype(np.float64)
        pair_sum = sim.sum(axis=(1, 2)) - np.einsum("gii->g", sim)
        n_pairs = n * (n - 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[s:s + chunk] = np.where(n >= 2, 1.0 - pair_sum / n_pairs, np.nan)
    return out


def category_entropy(lists: np.ndarray, item_category: np.ndarray, n_categories: int) -> np.ndarray:
    """목록 내 카테고리 분포의 섀넌 엔트로피(bit)."""
    valid = lists >= 0
    cats = np.where(valid, item_category[np.where(valid, lists, 0)], 0)
    rows = np.repeat(np.arange(len(lists)), lists.shape[1])
    counts = np.bincount(rows[valid.ravel()] * n_categories + cats.ravel()[valid.ravel()],
                         minlength=len(lists) * n_categories).reshape(len(lists), n_categories)
    p = counts / np.maximum(counts.sum(axis=1, keepdims=True), 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        h = -np.where(p > 0, p * np.log2(p), 0.0).sum(axis=1)
    return np.where(valid.any(axis=1), h, np.nan)


def catalog_coverage(lists: np.ndarray, pool_items: np.ndarray) -> float:
    """추천 목록 전체에 한 번이라도 등장한 아이템 수 / 후보 풀 전체의 서로 다른 아이템 수."""
    shown = np.unique(lists[lists >= 0])
    pool = np.unique(pool_items)
    return float(len(shown)) / max(len(pool), 1)


def cluster_bootstrap(values: np.ndarray, clusters: np.ndarray, n_boot: int = 1000, seed: int = 0,
                      alpha: float = 0.05) -> dict:
    """클러스터(유저) 단위 재표집 부트스트랩. 값은 관측(노출)별, nan은 제외."""
    values = np.asarray(values, dtype=np.float64)
    clusters = np.asarray(clusters)
    ok = ~np.isnan(values)
    v, c = values[ok], clusters[ok]
    if len(v) == 0:
        return {"mean": float("nan"), "lo": float("nan"), "hi": float("nan"), "n": 0, "n_clusters": 0}
    _, inv = np.unique(c, return_inverse=True)
    n_cl = int(inv.max()) + 1
    sums = np.bincount(inv, weights=v, minlength=n_cl)
    cnts = np.bincount(inv, minlength=n_cl).astype(np.float64)
    rng = np.random.default_rng(seed)
    boot = np.empty(n_boot)
    for b0 in range(0, n_boot, 100):
        b1 = min(b0 + 100, n_boot)
        w = rng.multinomial(n_cl, np.full(n_cl, 1.0 / n_cl), size=b1 - b0).astype(np.float64)
        boot[b0:b1] = (w @ sums) / np.maximum(w @ cnts, 1.0)
    lo, hi = np.quantile(boot, [alpha / 2, 1 - alpha / 2])
    return {"mean": float(v.mean()), "lo": float(lo), "hi": float(hi), "n": int(len(v)), "n_clusters": n_cl}


def paired_bootstrap_diff(a: np.ndarray, b: np.ndarray, clusters: np.ndarray, **kw) -> dict:
    """같은 노출에 대한 두 방법의 지표 차이(a-b)의 클러스터 부트스트랩."""
    return cluster_bootstrap(np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64), clusters, **kw)
