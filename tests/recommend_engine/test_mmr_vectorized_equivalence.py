"""MMRReranker.rerank 벡터화 전후로 선택 결과가 같은지 확인한다.

_reference_rerank는 벡터화 이전(성승우 작성) 구현의 탐욕 루프를 그대로 옮긴 오라클이다.
"""
import zlib

import numpy as np
import pytest

from src.core.reranker import MMRReranker


def _reference_rerank(lambda_param, pool_multiplier, scores, embeddings, top_k):
    n_items = len(scores)
    if n_items <= top_k:
        sorted_indices = np.argsort(scores)[::-1]
        return [(idx, scores[idx]) for idx in sorted_indices]
    pool_size = min(top_k * pool_multiplier, n_items)
    top_indices = np.argsort(scores)[::-1][:pool_size]
    pool_scores = scores[top_indices]
    pool_embeddings = embeddings[top_indices]
    min_score, max_score = pool_scores.min(), pool_scores.max()
    if max_score > min_score:
        norm_scores = (pool_scores - min_score) / (max_score - min_score)
    else:
        norm_scores = np.ones_like(pool_scores)
    norms = np.linalg.norm(pool_embeddings, axis=1, keepdims=True)
    norms = np.where(norms > 0, norms, 1.0)
    norm_embeddings = pool_embeddings / norms
    selected, selected_embs = [], []
    remaining = list(range(pool_size))
    for _ in range(top_k):
        if not remaining:
            break
        best_idx, best_mmr = None, float("-inf")
        for idx in remaining:
            relevance = norm_scores[idx]
            if selected_embs:
                max_sim = np.dot(np.stack(selected_embs), norm_embeddings[idx]).max()
            else:
                max_sim = 0.0
            mmr = lambda_param * relevance - (1 - lambda_param) * max_sim
            if mmr > best_mmr:
                best_mmr, best_idx = mmr, idx
        selected.append((top_indices[best_idx], pool_scores[best_idx]))
        selected_embs.append(norm_embeddings[best_idx])
        remaining.remove(best_idx)
    return selected


def _case(rng, n, dim, kind):
    emb = rng.normal(size=(n, dim)).astype(np.float32)
    scores = rng.normal(size=n)
    if kind == "duplicates":
        emb[1::3] = emb[0]
    elif kind == "tied_scores":
        scores = np.round(scores, 1)
    elif kind == "constant_scores":
        scores = np.full(n, 0.5)
    elif kind == "clustered":
        centers = rng.normal(size=(4, dim)).astype(np.float32)
        emb = centers[rng.integers(0, 4, n)] + 0.1 * emb
    return scores, emb


@pytest.mark.parametrize("kind", ["random", "duplicates", "tied_scores", "constant_scores", "clustered"])
@pytest.mark.parametrize("lam", [0.3, 0.7, 1.0])
def test_vectorized_mmr_matches_reference_selection(kind, lam):
    rng = np.random.default_rng(zlib.crc32(f"{kind}-{lam}".encode()))
    for n, dim, top_k in [(300, 64, 20), (120, 1024, 20), (25, 16, 20), (10, 8, 20), (50, 8, 5)]:
        scores, emb = _case(rng, n, dim, kind)
        got = MMRReranker(lambda_param=lam, pool_multiplier=4).rerank(scores, emb, top_k)
        want = _reference_rerank(lam, 4, scores, emb, top_k)
        assert [int(i) for i, _ in got] == [int(i) for i, _ in want], (kind, lam, n, dim, top_k)
        np.testing.assert_allclose([s for _, s in got], [s for _, s in want])
