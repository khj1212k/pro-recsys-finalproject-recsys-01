import importlib
import os
import sys
import types

import numpy as np

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)


class _RecordingBGEM3:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def encode(self, texts, **kwargs):
        _RecordingBGEM3.calls.append(kwargs)
        return {"dense_vecs": np.ones((len(texts), 4), dtype=np.float32)}


def _import_embedder_with_fakes(monkeypatch):
    # CI venv에는 torch/FlagEmbedding이 없으므로 가짜 모듈로 임포트만 통과시킨다.
    torch_stub = types.ModuleType("torch")
    torch_stub.Tensor = type("Tensor", (), {})
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    flag_stub = types.ModuleType("FlagEmbedding")
    flag_stub.BGEM3FlagModel = _RecordingBGEM3
    monkeypatch.setitem(sys.modules, "FlagEmbedding", flag_stub)
    monkeypatch.delitem(sys.modules, "core.embedder", raising=False)
    return importlib.import_module("core.embedder")


def test_default_max_length_is_unchanged(monkeypatch):
    mod = _import_embedder_with_fakes(monkeypatch)
    _RecordingBGEM3.calls.clear()
    emb = mod.NewsEmbedder(force_cpu=True, verbose=False, l2_normalize=False)
    vecs, _ = emb.generate_embeddings_batch(["a", "b"], batch_size=2)
    assert len(vecs) == 2
    assert _RecordingBGEM3.calls[0]["max_length"] == 8192


def test_max_length_is_passed_to_encoder(monkeypatch):
    mod = _import_embedder_with_fakes(monkeypatch)
    _RecordingBGEM3.calls.clear()
    emb = mod.NewsEmbedder(force_cpu=True, verbose=False, l2_normalize=False)
    emb.generate_embeddings_batch(["a", "b", "c"], batch_size=2, max_length=512)
    assert [c["max_length"] for c in _RecordingBGEM3.calls] == [512, 512]


class _RecordingInit(_RecordingBGEM3):
    init_kwargs = []

    def __init__(self, *args, **kwargs):
        _RecordingInit.init_kwargs.append(kwargs)


def _import_with_init_recorder(monkeypatch):
    mod = _import_embedder_with_fakes(monkeypatch)
    monkeypatch.setattr(sys.modules["FlagEmbedding"], "BGEM3FlagModel", _RecordingInit)
    _RecordingInit.init_kwargs.clear()
    return mod


def test_fp16_defaults_to_cuda_only(monkeypatch):
    mod = _import_with_init_recorder(monkeypatch)
    mod.NewsEmbedder(force_cpu=True, verbose=False)
    assert _RecordingInit.init_kwargs[-1]["use_fp16"] is False


def test_fp16_can_be_forced_for_non_cuda_devices(monkeypatch):
    mod = _import_with_init_recorder(monkeypatch)
    mod.NewsEmbedder(force_cpu=True, verbose=False, use_fp16=True)
    assert _RecordingInit.init_kwargs[-1]["use_fp16"] is True
