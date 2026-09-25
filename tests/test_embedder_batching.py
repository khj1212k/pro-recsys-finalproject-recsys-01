"""core/embedder.py: 토큰 길이 기반 배치 계획과 순서 보존.

FlagEmbedding 1.2.5의 BGEM3FlagModel.encode는 배치를 가장 긴 입력 길이로 패딩하고,
transformers 4.38.2의 XLM-R은 eager attention이라 (배치 x 헤드 x L x L) 점수 텐서를 만든다.
긴 기사 여러 건이 한 배치에 들어가면 프로세스가 수 GB~14GB까지 커졌다(2026-09-26, ADR 0006).
"""
import numpy as np
import pytest

from core.embedder import NewsEmbedder, plan_batches


def _cost(group, lengths, max_length):
    return len(group) * min(max(lengths[i] for i in group), max_length) ** 2


def test_plan_covers_every_index_once_and_respects_attention_budget():
    lengths = [30, 4000, 120, 900, 2100, 60, 1500, 10, 3000, 700]
    groups = plan_batches(lengths, max_batch=8, max_length=2048, attention_budget=8 * 1024 ** 2)

    flat = [i for g in groups for i in g]
    assert sorted(flat) == list(range(len(lengths)))
    for g in groups:
        assert len(g) <= 8
        assert len(g) == 1 or _cost(g, lengths, 2048) <= 8 * 1024 ** 2


def test_plan_groups_similar_lengths_so_short_texts_are_not_padded_to_long_ones():
    lengths = [5000, 20, 4800, 25, 30, 22]
    groups = plan_batches(lengths, max_batch=8, max_length=8192, attention_budget=8 * 1024 ** 2)

    short_group = next(g for g in groups if 1 in g)
    assert set(short_group) == {1, 3, 4, 5}
    # 긴 텍스트는 예산을 넘으므로 혼자 배치된다(최소 1건은 항상 진행).
    assert [0] in groups and [2] in groups


def test_plan_uses_truncated_length_for_cost():
    """max_length로 잘리는 입력은 잘린 길이로 비용을 계산한다."""
    lengths = [9000, 9000]
    groups = plan_batches(lengths, max_batch=8, max_length=1024, attention_budget=2 * 1024 ** 2)
    assert groups == [[0, 1]]


class _FakeTokenizer:
    def __call__(self, texts, truncation, max_length):
        ids = [list(range(min(len(t), max_length) if truncation else len(t))) for t in texts]
        return {"input_ids": ids}


class _FakeFlagModel:
    """텍스트 길이를 첫 성분에 담은 벡터를 돌려주고 encode 호출(배치 구성)을 기록한다."""

    def __init__(self):
        self.tokenizer = _FakeTokenizer()
        self.calls = []

    def encode(self, texts, batch_size, max_length, return_dense, return_sparse, return_colbert_vecs):
        self.calls.append({"n": len(texts), "batch_size": batch_size, "max_length": max_length})
        return {"dense_vecs": np.array([[float(len(t)), 1.0] for t in texts], dtype=np.float32)}


def _embedder(max_length=64, attention_budget=4 * 16 ** 2):
    emb = NewsEmbedder.__new__(NewsEmbedder)
    emb.verbose = False
    emb.l2_normalize = False
    emb.device = "cpu"
    emb.max_length = max_length
    emb.attention_budget = attention_budget
    emb.model = _FakeFlagModel()
    return emb


def test_generate_embeddings_batch_returns_vectors_in_input_order():
    emb = _embedder()
    texts = ["a" * 50, "b" * 3, "c" * 40, "d" * 5, "e" * 4]

    vectors, _ = emb.generate_embeddings_batch(texts, batch_size=8)

    assert [v[0] for v in vectors] == [50.0, 3.0, 40.0, 5.0, 4.0]
    calls = emb.model.calls
    assert sum(c["n"] for c in calls) == len(texts)
    assert all(c["max_length"] == 64 for c in calls)
    assert all(c["batch_size"] == c["n"] for c in calls)
    # 길이 3~5 텍스트 셋은 한 배치, 40/50은 예산(4*16^2) 때문에 따로 간다.
    assert sorted(c["n"] for c in calls) == [1, 1, 3]


def test_generate_embeddings_batch_l2_normalizes_when_enabled():
    emb = _embedder()
    emb.l2_normalize = True
    vectors, _ = emb.generate_embeddings_batch(["x" * 3, "y" * 4], batch_size=8)
    assert np.allclose(np.linalg.norm(np.array(vectors), axis=1), 1.0)


@pytest.mark.parametrize("texts", [[], None])
def test_generate_embeddings_batch_empty(texts):
    emb = _embedder()
    assert emb.generate_embeddings_batch(texts or [], batch_size=8) == ([], 0.0)
