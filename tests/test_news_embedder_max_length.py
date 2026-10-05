"""NewsEmbedder의 생성자 옵션 max_length·use_fp16.

절단 길이는 생성자에서 정한다(None이면 Settings.EMBEDDING_MAX_LENGTH). 가짜 모델은 tokenizer와
encode를 둘 다 가져서, 고정 크기 배치 구현과 토큰 길이 기반 배치(plan_batches) 구현 어느 쪽에서도
같은 계약(encode에 넘어가는 max_length, 입력 순서 보존, fp16 기본값)을 검사한다.
"""
import importlib
import os
import sys
import types

import numpy as np

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

# 실제 파이프라인 기본값(8192)과 다른 값을 넣어, 기본 절단 길이를 설정에서 읽는지 구분한다.
SETTINGS_MAX_LENGTH = 4096


class _FakeTokenizer:
    def __call__(self, texts, truncation=True, max_length=None, **kwargs):
        cap = max_length if (truncation and max_length) else 10 ** 9
        return {"input_ids": [list(range(min(len(t), cap))) for t in texts]}


class _RecordingBGEM3:
    calls = []
    init_kwargs = []

    def __init__(self, *args, **kwargs):
        _RecordingBGEM3.init_kwargs.append(kwargs)
        self.tokenizer = _FakeTokenizer()

    def encode(self, texts, **kwargs):
        _RecordingBGEM3.calls.append({"n": len(texts), **kwargs})
        # 첫 성분에 텍스트 길이를 담아 출력 순서가 입력 순서와 같은지 확인한다.
        return {"dense_vecs": np.array([[float(len(t)), 1.0, 0.0, 0.0] for t in texts], dtype=np.float32)}


def _import_embedder_with_fakes(monkeypatch):
    # CI venv에는 torch/FlagEmbedding이 없고, config.settings는 .env를 읽으므로 가짜 모듈로 대체한다.
    torch_stub = types.ModuleType("torch")
    torch_stub.Tensor = type("Tensor", (), {})
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    flag_stub = types.ModuleType("FlagEmbedding")
    flag_stub.BGEM3FlagModel = _RecordingBGEM3
    monkeypatch.setitem(sys.modules, "FlagEmbedding", flag_stub)
    settings_stub = types.ModuleType("config.settings")
    settings_stub.Settings = types.SimpleNamespace(
        EMBEDDING_MAX_LENGTH=SETTINGS_MAX_LENGTH, EMBEDDING_ATTENTION_BUDGET=8 * 1024 ** 2)
    monkeypatch.setitem(sys.modules, "config.settings", settings_stub)
    monkeypatch.delitem(sys.modules, "core.embedder", raising=False)
    _RecordingBGEM3.calls.clear()
    _RecordingBGEM3.init_kwargs.clear()
    return importlib.import_module("core.embedder")


def test_default_max_length_comes_from_settings(monkeypatch):
    mod = _import_embedder_with_fakes(monkeypatch)
    emb = mod.NewsEmbedder(force_cpu=True, verbose=False, l2_normalize=False)
    vecs, _ = emb.generate_embeddings_batch(["a", "bb"], batch_size=2)
    assert emb.max_length == SETTINGS_MAX_LENGTH
    assert len(vecs) == 2
    assert {c["max_length"] for c in _RecordingBGEM3.calls} == {SETTINGS_MAX_LENGTH}


def test_constructor_max_length_is_passed_to_every_encode_call(monkeypatch):
    mod = _import_embedder_with_fakes(monkeypatch)
    emb = mod.NewsEmbedder(force_cpu=True, verbose=False, l2_normalize=False, max_length=512)
    texts = ["a" * 7, "b" * 3, "c" * 5]
    vecs, _ = emb.generate_embeddings_batch(texts, batch_size=2)
    assert [v[0] for v in vecs] == [7.0, 3.0, 5.0]
    assert sum(c["n"] for c in _RecordingBGEM3.calls) == len(texts)
    assert {c["max_length"] for c in _RecordingBGEM3.calls} == {512}
    assert all(c["n"] <= 2 for c in _RecordingBGEM3.calls)


def test_max_length_attribute_change_applies_to_next_call(monkeypatch):
    """EB-NeRD 절단 영향 점검은 같은 모델로 잠깐 더 긴 max_length를 쓴다(모델 재로딩 없이)."""
    mod = _import_embedder_with_fakes(monkeypatch)
    emb = mod.NewsEmbedder(force_cpu=True, verbose=False, l2_normalize=False, max_length=512)
    emb.max_length = 2048
    emb.generate_embeddings_batch(["a", "b"], batch_size=4)
    assert {c["max_length"] for c in _RecordingBGEM3.calls} == {2048}


def test_fp16_defaults_to_cuda_only(monkeypatch):
    mod = _import_embedder_with_fakes(monkeypatch)
    mod.NewsEmbedder(force_cpu=True, verbose=False)
    assert _RecordingBGEM3.init_kwargs[-1]["use_fp16"] is False


def test_fp16_can_be_forced_for_non_cuda_devices(monkeypatch):
    mod = _import_embedder_with_fakes(monkeypatch)
    mod.NewsEmbedder(force_cpu=True, verbose=False, use_fp16=True)
    assert _RecordingBGEM3.init_kwargs[-1]["use_fp16"] is True
