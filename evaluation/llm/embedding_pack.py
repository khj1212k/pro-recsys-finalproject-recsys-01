"""임베딩 팩 - 원격 임베딩 잡이 돌려주는 산출물의 형식과, 동결 반출본에 대한 검증 (ADR 0036).

Tier 0 VM(1 GB)과 개발 Mac에서는 BGE-M3를 돌리지 않으므로 기사 임베딩은 밖(Colab T4)에서 계산해 파일로
받는다. pgvector는 거치지 않는다. 팩은 디렉터리 하나다.

    ids.npy                 int64 (N,)        raw_news_id, 오름차순
    emb.f16.npy | emb.f32.npy  float16|float32 (N, 1024)  L2 정규화된 dense 벡터, ids와 같은 순서
    content_sha256.txt      N줄               잡이 실제로 읽은 본문(UTF-8)의 sha256, ids와 같은 순서
    embeddings_manifest.json  형식, 행 수, 차원, dtype, 모델·파라미터, 파일별 sha256,
                              어느 반출본에서 만들었는지(export_identity_sha256), 잡이 남긴 수치(job)

`write_embedding_pack`은 numpy와 표준 라이브러리만 쓴다 - 원격 잡이 이 파일 하나를 가져가 쓸 수 있다.
`load_embedding_pack`은 팩을 반출본과 대조한다: 같은 반출본, 같은 id(빠짐·남음·순서), 같은 본문 해시,
1,024차원, NaN·Inf 없음, 단위 길이, 운영과 같은 모델·파라미터. 하나라도 어긋나면 사유 코드와 함께 거절한다.
원격에서 온 파일이므로 pickle은 읽지 않는다(`allow_pickle=False`).

    python scripts/import_embeddings.py --export-dir data/exports/<T0> --pack-dir data/exports/<T0>/embeddings
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import numpy as np

FORMAT = "news-embedding-pack/1"
MANIFEST_FILE = "embeddings_manifest.json"
IDS_FILE = "ids.npy"
HASHES_FILE = "content_sha256.txt"
VECTOR_FILES = {"float16": "emb.f16.npy", "float32": "emb.f32.npy"}
EMBEDDING_DIM = 1024
# 단위 길이에서 벗어나도 되는 폭. float16은 성분마다 상대 오차가 2^-11까지 생긴다.
NORM_TOLERANCE = {"float16": 1e-3, "float32": 1e-4}
# 운영 임베딩 경로(core.embedder.NewsEmbedder, pipeline.stages의 Stage3)와 같아야 하는 값.
# tests/evaluation/test_embedding_pack.py가 운영 코드의 상수와 맞는지 확인한다.
EXPECTED_MODEL: Dict[str, Any] = {
    "name": "BAAI/bge-m3",
    "dim": EMBEDDING_DIM,
    "max_length": 8192,
    "l2_normalize": True,
    "text_rule": 'f"{title} {content}"[:8000]',
}


class EmbeddingPackError(RuntimeError):
    """팩을 받지 않는다. `code`가 사유다."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm_stats(vectors: np.ndarray) -> Dict[str, float]:
    norms = np.linalg.norm(vectors.astype(np.float64), axis=1)
    if len(norms) == 0:
        return {"min": 1.0, "max": 1.0, "max_abs_deviation": 0.0}
    return {"min": float(norms.min()), "max": float(norms.max()),
            "max_abs_deviation": float(np.abs(norms - 1.0).max())}


def cosine_stats(reference: np.ndarray, other: np.ndarray) -> Dict[str, Any]:
    """같은 기사의 두 벡터 집합 사이 코사인의 분포(fp16 대 fp32 대조 등)."""
    a = np.asarray(reference, dtype=np.float64)
    b = np.asarray(other, dtype=np.float64)
    if a.shape != b.shape or a.ndim != 2:
        raise EmbeddingPackError("shape", f"비교할 두 배열의 모양이 다르다: {a.shape} / {b.shape}")
    cos = np.sum(a * b, axis=1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
    return {"n": int(len(cos)), "p10": float(np.percentile(cos, 10)), "min": float(cos.min()),
            "mean": float(cos.mean())}


# ---------------------------------------------------------------- 쓰기 (원격 잡)


def write_embedding_pack(directory: Path, *, ids: Sequence[int], vectors: np.ndarray, content_sha256: Sequence[str],
                         export_identity_sha256: str, model: Dict[str, Any], dtype: str = "float16",
                         job: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """팩을 쓴다. 여기서 막는 것은 자기 모순뿐이다(모양, 순서, NaN) - 반출본과의 대조는 읽는 쪽이 한다.

    `job`에는 수치와 식별자만 넣는다(장치, 정밀도, 연산 단위, fp16/fp32 코사인). 기사 텍스트를 넣지 않는다.
    """
    directory = Path(directory)
    if dtype not in VECTOR_FILES:
        raise EmbeddingPackError("dtype", f"dtype은 {sorted(VECTOR_FILES)} 중 하나다: {dtype!r}")
    if (directory / MANIFEST_FILE).exists():
        raise EmbeddingPackError("exists", f"{directory}: 이미 팩이 있다 - 덮어쓰지 않는다")
    id_array = np.asarray(list(ids), dtype=np.int64)
    matrix = np.asarray(vectors)
    hashes = [str(h) for h in content_sha256]
    if matrix.ndim != 2 or matrix.shape[0] != len(id_array) or len(hashes) != len(id_array):
        raise EmbeddingPackError(
            "shape", f"id {len(id_array)}개, 해시 {len(hashes)}개, 벡터 모양 {matrix.shape}가 서로 맞지 않는다")
    if len(id_array) > 1 and not np.all(np.diff(id_array) > 0):
        raise EmbeddingPackError("ids", "id는 중복 없이 오름차순이어야 한다")
    if not np.all(np.isfinite(matrix)):
        raise EmbeddingPackError("non_finite", "벡터에 NaN 또는 Inf가 있다")
    stored = matrix.astype(dtype)

    directory.mkdir(parents=True, exist_ok=True)
    vector_file = VECTOR_FILES[dtype]
    np.save(directory / vector_file, stored, allow_pickle=False)
    np.save(directory / IDS_FILE, id_array, allow_pickle=False)
    (directory / HASHES_FILE).write_text("".join(h + "\n" for h in hashes), encoding="ascii")
    manifest = {
        "format": FORMAT,
        "rows": int(len(id_array)),
        "dim": int(stored.shape[1]),
        "dtype": dtype,
        "model": dict(model),
        "export_identity_sha256": export_identity_sha256,
        "files": {
            name: {"sha256": sha256_file(directory / name), "bytes": (directory / name).stat().st_size}
            for name in (vector_file, IDS_FILE, HASHES_FILE)
        },
        "norm": _norm_stats(stored),
        "job": dict(job or {}),
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = directory / (MANIFEST_FILE + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, directory / MANIFEST_FILE)  # 매니페스트가 마지막이다 - 있으면 팩이 다 쓰인 것이다
    return manifest


# ---------------------------------------------------------------- 읽기와 검증 (Mac)


@dataclass
class EmbeddingPack:
    dir: Path
    manifest: Dict[str, Any]
    ids: np.ndarray       # int64 (N,)
    vectors: np.ndarray   # float32 (N, 1024) - 운영의 parse_embedding과 같은 dtype

    @property
    def vectors_sha256(self) -> str:
        return self.manifest["files"][VECTOR_FILES[self.manifest["dtype"]]]["sha256"]

    def summary(self) -> Dict[str, Any]:
        """화면과 사전 등록 기록에 옮겨 적는 값. 기사 텍스트는 없다."""
        return {
            "dir": str(self.dir),
            "rows": int(len(self.ids)),
            "dim": int(self.vectors.shape[1]),
            "dtype": self.manifest["dtype"],
            "vectors_sha256": self.vectors_sha256,
            "ids_sha256": self.manifest["files"][IDS_FILE]["sha256"],
            "export_identity_sha256": self.manifest["export_identity_sha256"],
            "model": self.manifest["model"],
            "norm": _norm_stats(self.vectors),
            "job": self.manifest.get("job", {}),
        }


def _some(values: Sequence[int], limit: int = 5) -> str:
    shown = ", ".join(str(v) for v in list(values)[:limit])
    return shown + (" …" if len(values) > limit else "")


def _load_array(path: Path, code: str) -> np.ndarray:
    try:
        return np.load(path, allow_pickle=False)
    except (ValueError, OSError) as e:
        raise EmbeddingPackError(code, f"{path.name}을 읽을 수 없다({type(e).__name__})") from None


def load_embedding_pack(directory: Path, export: Any, *, expected_model: Optional[Dict[str, Any]] = None) -> EmbeddingPack:
    """팩을 반출본(`identity_sha256`과 `rows`를 가진 객체, 보통 frozen_export.FrozenExport)과 대조해 읽는다."""
    directory = Path(directory)
    expected_model = EXPECTED_MODEL if expected_model is None else expected_model
    try:
        manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise EmbeddingPackError("manifest", f"{directory}: 매니페스트를 읽을 수 없다({type(e).__name__})") from None
    if manifest.get("format") != FORMAT:
        raise EmbeddingPackError("manifest", f"모르는 형식 {manifest.get('format')!r}")
    dtype = manifest.get("dtype")
    if dtype not in VECTOR_FILES:
        raise EmbeddingPackError("dtype", f"dtype은 {sorted(VECTOR_FILES)} 중 하나다: {dtype!r}")
    vector_file = VECTOR_FILES[dtype]

    for name in (vector_file, IDS_FILE, HASHES_FILE):
        path = directory / name
        if not path.is_file() or name not in manifest.get("files", {}):
            raise EmbeddingPackError("file_missing", f"{name}이 없다")
        if sha256_file(path) != manifest["files"][name].get("sha256"):
            raise EmbeddingPackError("file_sha256", f"{name}의 sha256이 매니페스트와 다르다")

    if manifest.get("export_identity_sha256") != export.identity_sha256:
        raise EmbeddingPackError("export_identity", "다른 반출본에서 만든 팩이다(export_identity_sha256 불일치)")
    model = manifest.get("model") or {}
    different = sorted(k for k in expected_model if model.get(k) != expected_model[k])
    if different:
        raise EmbeddingPackError("model", f"운영 임베딩과 다른 모델·파라미터다: {', '.join(different)}")

    ids = _load_array(directory / IDS_FILE, "ids")
    vectors = _load_array(directory / vector_file, "dtype")
    if ids.dtype != np.int64 or ids.ndim != 1:
        raise EmbeddingPackError("ids", f"ids는 1차원 int64여야 한다(dtype={ids.dtype}, ndim={ids.ndim})")
    if vectors.dtype != np.dtype(dtype):
        raise EmbeddingPackError("dtype", f"벡터 파일의 dtype {vectors.dtype}가 매니페스트의 {dtype}와 다르다")
    if vectors.ndim != 2 or vectors.shape[0] != len(ids):
        raise EmbeddingPackError("shape", f"벡터 모양 {vectors.shape}와 id {len(ids)}개가 맞지 않는다")
    if vectors.shape[1] != EMBEDDING_DIM:
        raise EmbeddingPackError("dim", f"차원이 {vectors.shape[1]}이다 - {EMBEDDING_DIM}이어야 한다")

    want = [int(r["id"]) for r in export.rows]
    have = ids.tolist()
    if have != want:
        missing = sorted(set(want) - set(have))
        extra = sorted(set(have) - set(want))
        detail = f"빠진 id {len(missing)}개({_some(missing)}), 남는 id {len(extra)}개({_some(extra)})"
        if not missing and not extra:
            detail = "id 집합은 같지만 순서가 다르거나 중복이 있다"
        raise EmbeddingPackError("ids", f"반출본의 id와 다르다: {detail}")

    hashes = (directory / HASHES_FILE).read_text(encoding="ascii").split()
    if len(hashes) != len(want):
        raise EmbeddingPackError("content_sha256", f"해시가 {len(hashes)}줄이다 - {len(want)}줄이어야 한다")
    differing = [row["id"] for row, digest in zip(export.rows, hashes) if row["content_sha256"] != digest]
    if differing:
        raise EmbeddingPackError(
            "content_sha256", f"잡이 읽은 본문이 반출본과 다른 행이 {len(differing)}건이다(id {_some(differing)})")

    if not np.all(np.isfinite(vectors)):
        raise EmbeddingPackError("non_finite", "벡터에 NaN 또는 Inf가 있다")
    deviation = np.abs(np.linalg.norm(vectors.astype(np.float64), axis=1) - 1.0)
    off = int(np.sum(deviation > NORM_TOLERANCE[dtype]))
    if off:
        raise EmbeddingPackError(
            "norm", f"단위 길이가 아닌 벡터가 {off}건이다(최대 편차 {float(deviation.max()):.4g}, "
                    f"허용 {NORM_TOLERANCE[dtype]:g})")

    return EmbeddingPack(directory, manifest, ids, np.ascontiguousarray(vectors, dtype=np.float32))


# ---------------------------------------------------------------- 실행


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="임베딩 팩을 동결 반출본과 대조한다")
    parser.add_argument("--export-dir", required=True)
    parser.add_argument("--pack-dir", required=True)
    args = parser.parse_args(argv)

    from evaluation.llm import frozen_export

    try:
        # 임베딩도 공개하지 않는다(ADR 0023 "남는 위험": 역변환). git이 추적할 수 있는 곳의 팩은 받지 않는다.
        frozen_export.ensure_private_destination(Path(args.pack_dir))
        export = frozen_export.load_export(Path(args.export_dir))  # 여는 김에 만료된 본문을 지운다
        pack = load_embedding_pack(Path(args.pack_dir), export)
    except (EmbeddingPackError, frozen_export.ExportError) as e:
        print(f"거절: {e}", file=sys.stderr)
        return 2
    print(json.dumps(pack.summary(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
