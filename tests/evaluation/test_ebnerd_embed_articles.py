"""embed_articles가 NewsEmbedder의 생성자 max_length 계약을 따르는지(모델·데이터 없이)."""
import numpy as np

from evaluation.recsys.ebnerd.embed_articles import embed_sorted, max_length_override


class _FakeEmbedder:
    """호출 시점의 max_length를 기록하고, 첫 성분에 텍스트 길이를 담은 1024차원 벡터를 돌려준다."""

    def __init__(self, max_length):
        self.max_length = max_length
        self.calls = []

    def generate_embeddings_batch(self, texts, batch_size=20):
        self.calls.append({"n": len(texts), "max_length": self.max_length, "batch_size": batch_size})
        out = np.zeros((len(texts), 1024), dtype=np.float32)
        out[:, 0] = [len(t) for t in texts]
        return out.tolist(), 0.01


def test_embed_sorted_restores_input_order_and_uses_embedder_max_length(tmp_path):
    emb = _FakeEmbedder(max_length=4)
    texts = ["aaaaaa", "b", "ccc", "dd", "eeeee"]
    lengths = np.array([len(t) for t in texts])

    out, timings = embed_sorted(emb, texts, lengths, batch_size=2, partial_dir=tmp_path / "p", chunk=2)

    assert out[:, 0].tolist() == [6.0, 1.0, 3.0, 2.0, 5.0]
    assert {c["max_length"] for c in emb.calls} == {4}
    assert [t["n"] for t in timings] == [2, 2, 1]
    # 길이순 청크: (1, 2), (3, 5), (6) -> 절단 길이 4로 자른 평균 토큰
    assert [t["mean_tokens"] for t in timings] == [1.5, 3.5, 4.0]


def test_embed_sorted_resumes_from_saved_chunks(tmp_path):
    texts = ["aa", "b", "ccc"]
    lengths = np.array([2, 1, 3])
    first = _FakeEmbedder(max_length=8)
    out1, _ = embed_sorted(first, texts, lengths, batch_size=4, partial_dir=tmp_path / "p", chunk=2)

    second = _FakeEmbedder(max_length=8)
    out2, timings = embed_sorted(second, texts, lengths, batch_size=4, partial_dir=tmp_path / "p", chunk=2)

    assert second.calls == [] and timings == []
    assert np.array_equal(out1, out2)


def test_max_length_override_is_scoped_and_restored_on_error():
    emb = _FakeEmbedder(max_length=512)
    with max_length_override(emb, 2048):
        emb.generate_embeddings_batch(["x"], batch_size=4)
    assert emb.calls[-1]["max_length"] == 2048
    assert emb.max_length == 512

    try:
        with max_length_override(emb, 1024):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert emb.max_length == 512
