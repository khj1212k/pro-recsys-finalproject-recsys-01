"""evaluation/llm/embedding_pack.py — 원격 임베딩 잡이 돌려주는 팩의 형식과, 반출본에 대한 검증.

벡터는 난수로 만든 단위 벡터이고 기사 문장은 tests/frozen_news_fakes.py에서 지어낸 것이다.
"""
import json
from datetime import datetime, timezone

import numpy as np
import pytest

from evaluation.llm import embedding_pack as ep
from evaluation.llm import frozen_export as fx
from tests.frozen_news_fakes import CODE, server_row, stream

WINDOW = fx.Window.from_iso("2026-10-02T21:00:00", "2026-10-05T21:00:00")
NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)
N = 6


def _unit(n=N, dim=ep.EMBEDDING_DIM, seed=3):
    rng = np.random.default_rng(seed)
    vec = rng.normal(size=(n, dim)).astype(np.float32)
    return vec / np.linalg.norm(vec, axis=1, keepdims=True)


@pytest.fixture
def export(tmp_path):
    dest = tmp_path / "data" / "exports" / "t0"
    rows = [server_row(k) for k in range(1, N + 1)]
    fx.write_export(dest, fx.build_export(fx.parse_copy_stream(stream(rows)), WINDOW, code=CODE))
    return fx.load_export(dest, now=NOW)


def _write(tmp_path, export, *, name="pack", **over):
    args = dict(
        ids=export.ids, vectors=_unit(), content_sha256=[r["content_sha256"] for r in export.rows],
        export_identity_sha256=export.identity_sha256, model=dict(ep.EXPECTED_MODEL), dtype="float16",
    )
    args.update(over)
    directory = tmp_path / name
    ep.write_embedding_pack(directory, **args)
    return directory


def _code(excinfo):
    return excinfo.value.code


# ---------------------------------------------------------------- 정상 경로


@pytest.mark.parametrize("dtype, file_name, atol", [("float16", "emb.f16.npy", 1e-3), ("float32", "emb.f32.npy", 0)])
def test_pack_round_trip_returns_float32_vectors_in_export_order(tmp_path, export, dtype, file_name, atol):
    directory = _write(tmp_path, export, dtype=dtype)
    manifest = json.loads((directory / ep.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["dtype"] == dtype and manifest["rows"] == N and manifest["dim"] == 1024
    assert set(manifest["files"]) == {file_name, ep.IDS_FILE, ep.HASHES_FILE}
    assert manifest["export_identity_sha256"] == export.identity_sha256

    pack = ep.load_embedding_pack(directory, export)

    assert pack.vectors.dtype == np.float32 and pack.vectors.shape == (N, 1024)
    assert pack.ids.dtype == np.int64 and pack.ids.tolist() == export.ids
    np.testing.assert_allclose(pack.vectors, _unit(), atol=atol)
    assert pack.vectors_sha256 == manifest["files"][file_name]["sha256"]
    assert pack.summary()["norm"]["max_abs_deviation"] < ep.NORM_TOLERANCE[dtype]


def test_pack_manifest_carries_model_parameters_and_job_notes_but_no_text(tmp_path, export):
    directory = _write(tmp_path, export, job={"device": "cuda", "precision": "fp16", "colab_cu": 0.5,
                                              "fp16_vs_fp32_cos": {"n": 100, "p10": 0.9999, "min": 0.9997}})
    manifest = json.loads((directory / ep.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["model"] == ep.EXPECTED_MODEL
    assert manifest["job"]["fp16_vs_fp32_cos"]["n"] == 100
    everything = b"".join(p.read_bytes() for p in directory.iterdir())
    for row in export.rows:
        assert row["title"].encode("utf-8") not in everything and row["body"][:20].encode("utf-8") not in everything


def test_pack_still_validates_after_the_export_bodies_were_purged(tmp_path, export):
    directory = _write(tmp_path, export)
    fx.purge_export(export.dir, now=NOW, everything=True)
    purged = fx.load_export(export.dir, now=NOW)
    assert all(r["body"] is None for r in purged.rows)
    assert ep.load_embedding_pack(directory, purged).ids.tolist() == export.ids


def test_storing_float16_keeps_cosine_to_the_float32_vectors_above_the_registered_floor():
    reference = _unit(n=500, seed=11)
    stats = ep.cosine_stats(reference, reference.astype(np.float16).astype(np.float32))
    assert stats["n"] == 500 and stats["min"] > 0.999999  # 저장 정밀도만의 영향. 모델을 fp16으로 돌린 영향은 아니다


# ---------------------------------------------------------------- 쓰는 쪽이 막는 것


def test_writer_refuses_inconsistent_or_unusable_input(tmp_path, export):
    with pytest.raises(ep.EmbeddingPackError) as err:
        _write(tmp_path, export, vectors=_unit(n=N - 1))
    assert _code(err) == "shape"
    bad = _unit()
    bad[2, 5] = np.nan
    with pytest.raises(ep.EmbeddingPackError) as err:
        _write(tmp_path, export, name="nan", vectors=bad)
    assert _code(err) == "non_finite"
    with pytest.raises(ep.EmbeddingPackError) as err:
        _write(tmp_path, export, name="dtype", dtype="float64")
    assert _code(err) == "dtype"
    with pytest.raises(ep.EmbeddingPackError) as err:
        _write(tmp_path, export, name="order", ids=list(reversed(export.ids)))
    assert _code(err) == "ids"
    _write(tmp_path, export, name="twice")
    with pytest.raises(ep.EmbeddingPackError) as err:
        _write(tmp_path, export, name="twice")
    assert _code(err) == "exists"


# ---------------------------------------------------------------- 읽는 쪽이 거절하는 것


def test_load_refuses_a_pack_made_from_another_export(tmp_path, export):
    directory = _write(tmp_path, export, export_identity_sha256="e" * 64)
    with pytest.raises(ep.EmbeddingPackError) as err:
        ep.load_embedding_pack(directory, export)
    assert _code(err) == "export_identity"


def test_load_refuses_missing_and_extra_ids(tmp_path, export):
    hashes = [r["content_sha256"] for r in export.rows]
    fewer = _write(tmp_path, export, name="fewer", ids=export.ids[:-1], vectors=_unit(n=N - 1),
                   content_sha256=hashes[:-1])
    with pytest.raises(ep.EmbeddingPackError, match="빠진 id 1") as err:
        ep.load_embedding_pack(fewer, export)
    assert _code(err) == "ids"
    more = _write(tmp_path, export, name="more", ids=export.ids + [9999], vectors=_unit(n=N + 1),
                  content_sha256=hashes + ["a" * 64])
    with pytest.raises(ep.EmbeddingPackError, match="남는 id 1") as err:
        ep.load_embedding_pack(more, export)
    assert _code(err) == "ids"


def test_load_refuses_vectors_computed_from_different_text(tmp_path, export):
    hashes = [r["content_sha256"] for r in export.rows]
    hashes[3] = "b" * 64
    directory = _write(tmp_path, export, content_sha256=hashes)
    with pytest.raises(ep.EmbeddingPackError, match=str(export.ids[3])) as err:
        ep.load_embedding_pack(directory, export)
    assert _code(err) == "content_sha256"


def test_load_refuses_wrong_dimension(tmp_path, export):
    directory = _write(tmp_path, export, vectors=_unit(dim=768))
    with pytest.raises(ep.EmbeddingPackError) as err:
        ep.load_embedding_pack(directory, export)
    assert _code(err) == "dim"


def test_load_refuses_vectors_that_are_not_unit_length(tmp_path, export):
    scaled = _unit()
    scaled[1] *= 1.05
    directory = _write(tmp_path, export, vectors=scaled, model={**ep.EXPECTED_MODEL})
    with pytest.raises(ep.EmbeddingPackError, match="1건") as err:
        ep.load_embedding_pack(directory, export)
    assert _code(err) == "norm"


def test_load_refuses_a_model_or_parameters_other_than_production(tmp_path, export):
    directory = _write(tmp_path, export, model={**ep.EXPECTED_MODEL, "max_length": 512})
    with pytest.raises(ep.EmbeddingPackError, match="max_length") as err:
        ep.load_embedding_pack(directory, export)
    assert _code(err) == "model"


def test_load_refuses_files_changed_after_the_manifest_was_written(tmp_path, export):
    directory = _write(tmp_path, export)
    vectors = np.load(directory / "emb.f16.npy")
    vectors[0, 0] += np.float16(0.25)
    np.save(directory / "emb.f16.npy", vectors)
    with pytest.raises(ep.EmbeddingPackError) as err:
        ep.load_embedding_pack(directory, export)
    assert _code(err) == "file_sha256"


def test_load_refuses_missing_files_and_unknown_format(tmp_path, export):
    directory = _write(tmp_path, export)
    (directory / ep.IDS_FILE).unlink()
    with pytest.raises(ep.EmbeddingPackError) as err:
        ep.load_embedding_pack(directory, export)
    assert _code(err) == "file_missing"
    with pytest.raises(ep.EmbeddingPackError) as err:
        ep.load_embedding_pack(tmp_path / "nothing-here", export)
    assert _code(err) == "manifest"


def test_load_never_unpickles_what_a_remote_job_sent(tmp_path, export):
    """manifest의 sha256까지 맞춰 둔 object 배열(pickle)도 읽지 않는다."""
    directory = _write(tmp_path, export)
    np.save(directory / ep.IDS_FILE, np.array([{"x": 1}] * N, dtype=object), allow_pickle=True)
    manifest_path = directory / ep.MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][ep.IDS_FILE]["sha256"] = ep.sha256_file(directory / ep.IDS_FILE)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ep.EmbeddingPackError) as err:
        ep.load_embedding_pack(directory, export)
    assert _code(err) == "ids"


# ---------------------------------------------------------------- CLI


def test_cli_prints_a_summary_without_text_and_fails_with_the_reason(tmp_path, export, capsys):
    directory = _write(tmp_path, export)
    assert ep.main(["--export-dir", str(export.dir), "--pack-dir", str(directory)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"] == N and summary["dtype"] == "float16" and summary["dim"] == 1024
    assert summary["export_identity_sha256"] == export.identity_sha256
    assert len(summary["vectors_sha256"]) == 64

    other = _write(tmp_path, export, name="other", export_identity_sha256="e" * 64)
    assert ep.main(["--export-dir", str(export.dir), "--pack-dir", str(other)]) == 2
    assert "export_identity" in capsys.readouterr().err


# ---------------------------------------------------------------- 운영 코드와의 계약


def test_expected_model_matches_the_production_embedding_path():
    """팩이 요구하는 모델·파라미터가 운영 임베딩 경로의 값과 같다. 운영이 바뀌면 여기서 깨진다.

    core.embedder는 torch·FlagEmbedding을 임포트하므로(이 환경에 없다) 소스를 읽어 확인한다.
    """
    from pathlib import Path

    from config.settings import BaseSettings

    workspace = Path(__file__).resolve().parents[2] / "ai_workspace"
    embedder_source = (workspace / "core" / "embedder.py").read_text(encoding="utf-8")
    stage_source = (workspace / "pipeline" / "stages.py").read_text(encoding="utf-8")

    assert f"'{ep.EXPECTED_MODEL['name']}'" in embedder_source
    assert "l2_normalize: bool = True" in embedder_source
    assert BaseSettings.EMBEDDING_MAX_LENGTH == ep.EXPECTED_MODEL["max_length"]
    assert BaseSettings.EMBEDDING_DIM == ep.EXPECTED_MODEL["dim"] == ep.EMBEDDING_DIM
    # Stage3이 임베딩에 넣는 텍스트: 제목 + 공백 + 본문, 앞 8,000자
    assert 'texts = [f"{r[1]} {r[2]}"[:8000] for r in batch]' in stage_source
    assert ep.EXPECTED_MODEL["text_rule"] == 'f"{title} {content}"[:8000]'
