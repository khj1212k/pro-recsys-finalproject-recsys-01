"""기사 임베딩 잡 - 동결 반출본의 본문을 운영 임베딩 경로 그대로 임베딩해 임베딩 팩을 만든다 (ADR 0036, ADR 0009 A8.1).

원격 GPU 런타임(Colab T4)에서 `scripts/colab_embed_articles.py`(드라이버)가 서브프로세스로 부른다. 개발 Mac에서는
BGE-M3를 돌리지 않는다 - Mac의 테스트는 임베더 자리에 페이크를 넣어 이 모듈의 배치·체크포인트·팩 조립만 본다.

운영과 같은 것(바꾸지 않는다):
- 임베더: `core.embedder.NewsEmbedder(l2_normalize=True)` - 모델 `BAAI/bge-m3`, `Settings.EMBEDDING_MAX_LENGTH`(8192),
  dense CLS 풀링(FlagEmbedding 1.2.5 기본), L2 정규화, CUDA면 fp16(`use_fp16=None`의 운영 기본값).
- 텍스트 규칙: Stage3(`pipeline.stages.embed_pending_articles`)와 같은 `f"{title} {content}"[:8000]`.
- 배치: Stage3와 같은 `pipeline.stages._length_sorted_batches(rows, EMBEDDING_BATCH_SIZE, EMBEDDING_BATCH_SIZE * 8)`를
  id 순 행에 그대로 쓴다. 파트(체크포인트 단위)는 그 정렬 창(64행)의 배수인 1,024행이라 파트로 나눠도 운영이 같은
  행을 한 번에 돌렸을 때와 배치 구성이 같다.

운영과 다른 것(선언된 이탈 5): 장치와 정밀도(T4 fp16 - 운영 워커는 CPU fp32), torch 빌드. 그래서 fp32로 같은 기사
100건을 다시 임베딩해 코사인을 남긴다(A8.1).

기사 텍스트는 어디에도 쓰지 않는다: 로그·진행 기록·잡 기록에는 건수·길이·해시·시간만 있다. 오류도 예외의 종류만 적는다.

    python -m evaluation.llm.embed_job --input-dir /content/e0in --out-dir /content/e0job/out
    python -m evaluation.llm.embed_job --synthetic 300 --out-dir /content/e0job/out      # 합성 텍스트로 처리량·결정론만 잰다
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

# 사전 등록(e0-baseline-v1.yaml sample.embedding, ADR 0009 A8.1)과 테스트가 묶는 값.
PART_ROWS = 1024                   # 운영 정렬 창(배치 8 x 8 = 64행)의 배수
FP32_CHECK_ROWS = 100              # A8.1 "fp16/fp32 대조 100건"
FP32_CHECK_SEED = 20261006         # 대조할 100건을 고르는 시드(A8.1의 표본 시드와 같은 값)
DETERMINISM_ROWS = 64              # 같은 배치를 두 번 돌려 비트 단위로 같은지 보는 행 수(운영 정렬 창 하나)
MODEL_NAME = "BAAI/bge-m3"
# 2026-10-17에 Hugging Face API로 확인한 main의 커밋(lastModified 2024-07-03). 운영 코드는 리비전을 지정하지 않으므로
# 같은 main을 받는다. 잡은 실제로 읽은 스냅숏의 리비전을 적고 이 값과 다르면 멈춘다.
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
TEXT_CHAR_LIMIT = 8000
SYNTHETIC_ID_BASE = 9_000_000_000  # 합성 행 id - 실제 raw_news_id와 겹치지 않는 범위

EXIT_OK, EXIT_INPUT, EXIT_ENVIRONMENT, EXIT_BUDGET, EXIT_FAILED = 0, 2, 8, 10, 5
STOP_FILE = "STOP_BUDGET"          # 드라이버가 CU 상한에 닿으면 만드는 파일. 다음 파트를 시작하지 않는다
PROGRESS_FILE = "progress.json"
JOB_FILE = "job.json"
PARTS_DIR = "parts"
PACK_DIR = "pack"


class JobError(RuntimeError):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def embedding_text(title: str, body: str) -> str:
    """Stage3의 `f"{r[1]} {r[2]}"[:8000]`(r = (id, 제목, 본문)). 테스트가 운영 소스의 식과 같은지 본다."""
    return f"{title} {body}"[:TEXT_CHAR_LIMIT]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any = None) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


# ---------------------------------------------------------------- 입력


Row = Tuple[int, str, str]  # (id, 제목, 본문) - Stage3의 SELECT raw_news_id, raw_news_title, raw_news_content와 같은 순서


def rows_from_export(input_dir: Path, *, now: Optional[datetime] = None) -> Tuple[List[Row], Dict[str, Any]]:
    """업로드된 반출본(manifest.json + articles.jsonl)을 동결 반출 도구로 검증해 읽는다.

    `frozen_export.load_export`가 신원·행별 해시·파일 sha256을 대조하고 만료된 본문을 지운다. 본문이 지워진 행이
    하나라도 있으면 멈춘다 - 팩은 반출본의 모든 행을 덮어야 받아들여진다(embedding_pack.load_embedding_pack).
    """
    from evaluation.llm import frozen_export

    try:
        export = frozen_export.load_export(Path(input_dir), now=now)
    except frozen_export.ExportError as e:
        raise JobError(f"입력 반출본을 받지 않는다: {e}", EXIT_INPUT) from None
    missing = [r["id"] for r in export.rows if r["body"] is None]
    if missing:
        raise JobError(f"본문이 없는 행이 {len(missing)}건이다(만료·정리). 그 반출본으로는 팩을 만들 수 없다", EXIT_INPUT)
    rows = sorted(((int(r["id"]), r["title"], r["body"]) for r in export.rows), key=lambda r: r[0])
    info = {
        "export_identity_sha256": export.identity_sha256,
        "rows": len(rows),
        "first_body_expiry_utc": export.identity["retention"]["first_body_expiry_utc"],
        "articles_sha256": export.manifest["files"]["articles.jsonl"]["sha256"],
    }
    return rows, info


# 합성 텍스트: 지어낸 낱말로 만든 한국어 꼴 문장. 실제 기사와 무관하고, 길이 분포만 반출 기사(실행 계획 2.1:
# p10 602 / p50 1,178 / p90 3,633자)와 비슷하게 맞춰 처리량을 잰다.
_SYN_WORDS = (
    "가람시", "누리구", "한빛", "새솔", "다온", "미르", "아라", "온새미", "도담", "라온",
    "계획", "발표", "예산", "사업", "주민", "시설", "확대", "추진", "협의", "검토", "지원", "운영", "개선", "조사",
    "결과", "올해", "내년", "지난달", "이번", "관계자", "위원회", "기관", "지역", "단계", "방안", "제도", "참여",
    "공개", "설명", "논의", "일정", "규모", "비율", "증가", "감소", "유지", "변경", "시행", "마련", "점검",
)
_SYN_ENDINGS = ("밝혔다", "전했다", "설명했다", "덧붙였다", "예정이다", "계획이다", "것으로 나타났다")


def synthetic_rows(n: int, seed: int = 20261017) -> List[Row]:
    """처리량·결정론 측정용 합성 행 n개(id 오름차순). 기사 본문이 아니다."""
    rng = random.Random(seed)
    rows: List[Row] = []
    for i in range(n):
        chars = int(min(12_000, max(150, math.exp(rng.gauss(math.log(1178), 0.88)))))
        sentences: List[str] = []
        total = 0
        while total < chars:
            words = [rng.choice(_SYN_WORDS) for _ in range(rng.randint(4, 9))]
            if rng.random() < 0.4:
                words.insert(rng.randint(0, len(words)), f"{rng.randint(1, 999)}건")
            sentence = " ".join(words) + " " + rng.choice(_SYN_ENDINGS) + "."
            sentences.append(sentence)
            total += len(sentence) + 1
        body = " ".join(sentences)[:chars]
        title = " ".join(rng.choice(_SYN_WORDS) for _ in range(rng.randint(3, 6)))
        rows.append((SYNTHETIC_ID_BASE + i, title, body))
    return rows


# ---------------------------------------------------------------- 운영 임베딩 경로


def production_batches(rows: Sequence[Row], batch_size: int) -> Iterable[List[Row]]:
    """Stage3가 쓰는 배치 함수 그대로(운영 기본 정렬 창 = batch_size * 8)."""
    from pipeline.stages import _length_sorted_batches

    return _length_sorted_batches(list(rows), batch_size, batch_size * 8)


def production_batch_size() -> int:
    from config.settings import Settings

    return int(Settings.EMBEDDING_BATCH_SIZE)


def load_embedder(*, use_fp16: Optional[bool] = None):
    """운영 임베더. use_fp16=None은 운영 기본값(CUDA면 fp16)이고, fp32 대조에만 False를 준다."""
    from core.embedder import NewsEmbedder

    return NewsEmbedder(verbose=False, l2_normalize=True, use_fp16=use_fp16)


def embed_rows(embedder, rows: Sequence[Row], batch_size: int) -> Tuple[np.ndarray, Dict[str, float]]:
    """rows(id 순)를 운영 배치로 임베딩해 같은 순서의 float32 배열로 돌려준다."""
    position = {r[0]: i for i, r in enumerate(rows)}
    out = np.zeros((len(rows), 1024), dtype=np.float32)
    seen = 0
    encode_s = 0.0
    for batch in production_batches(rows, batch_size):
        texts = [embedding_text(r[1], r[2]) for r in batch]
        vectors, elapsed = embedder.generate_embeddings_batch(texts, batch_size)
        if len(vectors) != len(batch):
            raise JobError(f"임베더가 {len(batch)}건에 {len(vectors)}개를 돌려줬다", EXIT_FAILED)
        encode_s += float(elapsed)
        for row, vec in zip(batch, vectors):
            out[position[row[0]]] = np.asarray(vec, dtype=np.float32)
            seen += 1
    if seen != len(rows):
        raise JobError(f"배치가 {len(rows)}행 중 {seen}행만 덮었다", EXIT_FAILED)
    if not np.all(np.isfinite(out)):
        raise JobError("임베딩에 NaN 또는 Inf가 있다", EXIT_FAILED)
    return out, {"encode_s": round(encode_s, 3)}


def token_lengths(embedder, rows: Sequence[Row]) -> Optional[List[int]]:
    """잘리기 전 토큰 수(처리량 기록용). 토크나이저가 없으면 None."""
    tokenizer = getattr(getattr(embedder, "model", None), "tokenizer", None)
    if tokenizer is None:
        return None
    texts = [embedding_text(r[1], r[2]) for r in rows]
    return [len(ids) for ids in tokenizer(texts, truncation=False)["input_ids"]]


def model_revision(embedder) -> Optional[str]:
    """임베더가 실제로 읽은 스냅숏의 커밋(.../snapshots/<sha>). 알 수 없으면 None."""
    import re

    candidates: List[Any] = []
    obj = getattr(embedder, "model", None)
    for _ in range(4):
        if obj is None:
            break
        config = getattr(obj, "config", None)
        if config is not None:
            candidates.append(getattr(config, "_name_or_path", None))
        candidates.append(getattr(obj, "name_or_path", None))
        obj = getattr(obj, "model", None)
    for value in candidates:
        match = re.search(r"snapshots/([0-9a-f]{40})", str(value or ""))
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------- 파트와 재개


def plan_parts(n_rows: int, part_rows: int = PART_ROWS) -> List[Tuple[int, int]]:
    return [(start, min(start + part_rows, n_rows)) for start in range(0, n_rows, part_rows)]


def _ids_sha256(ids: Sequence[int]) -> str:
    return hashlib.sha256(",".join(str(int(i)) for i in ids).encode("ascii")).hexdigest()


def _part_paths(out_dir: Path, index: int) -> Tuple[Path, Path]:
    base = out_dir / PARTS_DIR / f"part_{index:05d}"
    return base.with_suffix(".ids.npy"), base.with_suffix(".emb.npy")


def load_progress(out_dir: Path, config_digest: str) -> Dict[str, Any]:
    progress = _read_json(out_dir / PROGRESS_FILE)
    if progress is None:
        return {"config_digest": config_digest, "parts": {}, "started_at": _now()}
    if progress.get("config_digest") != config_digest:
        raise JobError("이전 진행 기록과 입력·설정이 다르다 - 이어서 돌 수 없다(out을 비우고 처음부터)", EXIT_INPUT)
    return progress


def part_done(out_dir: Path, progress: Dict[str, Any], index: int, ids: Sequence[int]) -> bool:
    """체크포인트가 있고, 기록한 sha256과 같고, 같은 id를 덮는가."""
    entry = progress["parts"].get(str(index))
    ids_path, emb_path = _part_paths(out_dir, index)
    if not entry or not ids_path.exists() or not emb_path.exists():
        return False
    if sha256_file(emb_path) != entry["emb_sha256"] or sha256_file(ids_path) != entry["ids_npy_sha256"]:
        return False
    return entry["ids_sha256"] == _ids_sha256(ids)


def write_part(out_dir: Path, progress: Dict[str, Any], index: int, ids: Sequence[int], vectors: np.ndarray,
               stats: Dict[str, Any]) -> None:
    ids_path, emb_path = _part_paths(out_dir, index)
    ids_path.parent.mkdir(parents=True, exist_ok=True)
    for path, array in ((ids_path, np.asarray(ids, dtype=np.int64)), (emb_path, np.asarray(vectors, dtype=np.float32))):
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            np.save(f, array, allow_pickle=False)
        os.replace(tmp, path)
    progress["parts"][str(index)] = {
        "rows": len(ids), "ids_sha256": _ids_sha256(ids), "ids_npy_sha256": sha256_file(ids_path),
        "emb_sha256": sha256_file(emb_path), "done_at": _now(), **stats,
    }
    _write_json(out_dir / PROGRESS_FILE, progress)


def read_part(out_dir: Path, index: int) -> Tuple[np.ndarray, np.ndarray]:
    ids_path, emb_path = _part_paths(out_dir, index)
    return np.load(ids_path, allow_pickle=False), np.load(emb_path, allow_pickle=False)


# ---------------------------------------------------------------- 잡


def check_rows(rows: Sequence[Row]) -> None:
    ids = [r[0] for r in rows]
    if not rows:
        raise JobError("입력 행이 없다", EXIT_INPUT)
    if ids != sorted(set(ids)):
        raise JobError("입력 id는 중복 없이 오름차순이어야 한다", EXIT_INPUT)
    if any(not isinstance(r[1], str) or not isinstance(r[2], str) or not r[2] for r in rows):
        raise JobError("제목·본문은 문자열이고 본문이 비어 있지 않아야 한다", EXIT_INPUT)


def fp32_check_indices(n_rows: int, k: int = FP32_CHECK_ROWS, seed: int = FP32_CHECK_SEED) -> List[int]:
    return sorted(random.Random(seed).sample(range(n_rows), min(k, n_rows)))


def run_job(rows: Sequence[Row], out_dir: Path, *, input_info: Dict[str, Any],
            embedder_factory: Callable[..., Any] = load_embedder, batch_size: Optional[int] = None,
            part_rows: int = PART_ROWS, expected_revision: Optional[str] = MODEL_REVISION,
            log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
            write_pack: bool = True, fp32_check: bool = True) -> Dict[str, Any]:
    """파트마다 임베딩하고 체크포인트를 남긴 뒤, 모든 파트가 있으면 fp32 대조·결정론 확인을 하고 팩을 쓴다.

    돌려주는 것: 잡 기록(job.json과 같은 내용 - 수치·해시만). 드라이버가 STOP_BUDGET 파일을 만들면 다음 파트를
    시작하지 않고 JobError(EXIT_BUDGET)를 낸다(이미 쓴 파트는 남는다).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    check_rows(rows)
    batch_size = int(batch_size or production_batch_size())
    ids = [r[0] for r in rows]
    digest = hashlib.sha256(json.dumps({
        "input": input_info.get("export_identity_sha256") or input_info.get("synthetic"),
        "ids": _ids_sha256(ids), "part_rows": part_rows, "batch_size": batch_size,
    }, sort_keys=True).encode()).hexdigest()
    progress = load_progress(out_dir, digest)
    parts = plan_parts(len(rows), part_rows)
    log(f"job: 행 {len(rows)}, 파트 {len(parts)}(파트당 {part_rows}행), 배치 {batch_size}")

    t_load = clock()
    embedder = embedder_factory(use_fp16=None)
    load_s = round(clock() - t_load, 2)
    revision = model_revision(embedder)
    device = str(getattr(embedder, "device", "unknown"))
    if expected_revision and revision and revision != expected_revision:
        raise JobError(f"모델 스냅숏 리비전 {revision}이 고정값 {expected_revision}과 다르다", EXIT_ENVIRONMENT)
    log(f"model: 로드 {load_s}s, 장치 {device}, 리비전 {revision or '확인 못 함'}")

    encode_total = 0.0
    for index, (start, end) in enumerate(parts):
        part_ids = ids[start:end]
        if part_done(out_dir, progress, index, part_ids):
            log(f"part {index}: 이미 있음, 건너뜀")
            continue
        if (out_dir / STOP_FILE).exists():
            raise JobError(f"CU 상한으로 멈춤 요청 - 파트 {index}부터 시작하지 않는다", EXIT_BUDGET)
        vectors, stats = embed_rows(embedder, rows[start:end], batch_size)
        lengths = token_lengths(embedder, rows[start:end])
        if lengths is not None:
            stats.update({"tokens": int(sum(lengths)), "tokens_truncated": int(sum(max(0, n - 8192) for n in lengths))})
        stats["chars"] = int(sum(len(embedding_text(r[1], r[2])) for r in rows[start:end]))
        write_part(out_dir, progress, index, part_ids, vectors, stats)
        encode_total += stats["encode_s"]
        log(f"part {index}: {end - start}행, encode {stats['encode_s']}s")

    vectors = np.concatenate([read_part(out_dir, i)[1] for i in range(len(parts))], axis=0)
    got_ids = np.concatenate([read_part(out_dir, i)[0] for i in range(len(parts))]).tolist()
    if got_ids != ids:
        raise JobError("파트를 이어 붙인 id가 입력과 다르다", EXIT_FAILED)

    # 결정론: 첫 정렬 창(64행)을 같은 배치로 한 번 더 돌린다.
    window = list(rows[:DETERMINISM_ROWS])
    again, _ = embed_rows(embedder, window, batch_size)
    first = vectors[:len(window)]
    determinism = {"rows": len(window), "bitwise_equal": bool(np.array_equal(again, first)),
                   "max_abs_diff": float(np.max(np.abs(again - first))) if len(window) else 0.0}
    log(f"determinism: {determinism}")

    peak_mem = None
    try:
        import torch  # noqa: WPS433 - 원격에서만

        if torch.cuda.is_available():
            peak_mem = round(torch.cuda.max_memory_allocated() / 1024 ** 3, 2)
    except Exception:  # noqa: BLE001 - 기록용
        pass
    if hasattr(embedder, "cleanup"):
        embedder.cleanup()

    from evaluation.llm import embedding_pack

    fp32 = None
    if fp32_check:
        picks = fp32_check_indices(len(rows))
        reference = embedder_factory(use_fp16=False)
        ref_vectors, _ = embed_rows(reference, [rows[i] for i in picks], batch_size)
        if hasattr(reference, "cleanup"):
            reference.cleanup()
        model_out = vectors[picks]
        stored = model_out.astype(np.float16).astype(np.float32)
        fp32 = {"n": len(picks), "seed": FP32_CHECK_SEED,
                "model_fp16_vs_fp32": embedding_pack.cosine_stats(ref_vectors, model_out),
                "stored_f16_vs_fp32": embedding_pack.cosine_stats(ref_vectors, stored)}
        log(f"fp16/fp32: p10 {fp32['stored_f16_vs_fp32']['p10']:.6f}, min {fp32['stored_f16_vs_fp32']['min']:.6f}")

    done = [progress["parts"][str(i)] for i in range(len(parts))]
    encode_s = round(sum(p["encode_s"] for p in done), 3)
    tokens = sum(p.get("tokens", 0) for p in done) if all("tokens" in p for p in done) else None
    job = {
        "format": "embed-job/1",
        "input": input_info,
        "rows": len(rows),
        "parts": len(parts),
        "part_rows": part_rows,
        "batch_size": batch_size,
        "sort_window": batch_size * 8,
        "device": device,
        "precision": "fp16" if device == "cuda" else "fp32",
        "model_revision": revision,
        "model_revision_expected": expected_revision,
        "model_load_s": load_s,
        "encode_s": encode_s,
        "encode_s_this_invocation": round(encode_total, 3),
        "articles_per_s": round(len(rows) / encode_s, 3) if encode_s else None,
        "tokens": tokens,
        "tokens_per_s": round(tokens / encode_s, 1) if tokens and encode_s else None,
        "tokens_truncated": sum(p.get("tokens_truncated", 0) for p in done) if tokens is not None else None,
        "chars": sum(p["chars"] for p in done),
        "gpu_peak_mem_gb": peak_mem,
        "determinism": determinism,
        "fp16_vs_fp32": fp32,
        "versions": package_versions(),
        "finished_at": _now(),
    }
    if write_pack:
        model = {**embedding_pack.EXPECTED_MODEL, "revision": revision, "precision": job["precision"], "device": device}
        content = [sha256_text(r[2]) for r in rows]
        titles = [sha256_text(r[1]) for r in rows]
        pack_dir = out_dir / PACK_DIR
        if (pack_dir / embedding_pack.MANIFEST_FILE).exists():
            job["pack"] = _read_json(pack_dir / embedding_pack.MANIFEST_FILE)["files"]
        else:
            manifest = embedding_pack.write_embedding_pack(
                pack_dir, ids=ids, vectors=vectors, content_sha256=content, title_sha256=titles,
                export_identity_sha256=str(input_info.get("export_identity_sha256") or "synthetic"),
                model=model, dtype="float16", job={k: v for k, v in job.items() if k != "input"})
            job["pack"] = manifest["files"]
    _write_json(out_dir / JOB_FILE, job)
    return job


def package_versions() -> Dict[str, Optional[str]]:
    import importlib.metadata as md

    out: Dict[str, Optional[str]] = {"python": sys.version.split()[0]}
    for name in ("torch", "FlagEmbedding", "transformers", "tokenizers", "sentence-transformers", "huggingface-hub",
                 "safetensors", "accelerate", "numpy"):
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            out[name] = None
    try:
        import torch

        out["cuda"] = torch.version.cuda
        out["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except Exception:  # noqa: BLE001
        out["cuda"] = out["gpu"] = None
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="동결 반출본의 기사 임베딩(운영 임베딩 경로)")
    parser.add_argument("--input-dir", default=None, help="업로드한 반출본(manifest.json + articles.jsonl)")
    parser.add_argument("--synthetic", type=int, default=None, help="합성 텍스트 n건(드라이 런)")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--no-pack", action="store_true", help="팩을 쓰지 않는다(드라이 런에서 처리량만)")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)

    def log(message: str) -> None:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} embed_job {message}", flush=True)

    try:
        if (args.input_dir is None) == (args.synthetic is None):
            raise JobError("--input-dir와 --synthetic 중 하나만 준다", EXIT_INPUT)
        if args.synthetic is not None:
            if not 1 <= args.synthetic <= 5000:
                raise JobError("--synthetic은 1~5000건", EXIT_INPUT)
            rows = synthetic_rows(args.synthetic)
            info: Dict[str, Any] = {"synthetic": f"synthetic_rows(n={args.synthetic}, seed=20261017)"}
        else:
            rows, info = rows_from_export(Path(args.input_dir))
        job = run_job(rows, out_dir, input_info=info, log=log, write_pack=not args.no_pack)
    except JobError as e:
        log(f"중단: {e}")
        return e.code
    except Exception as e:  # noqa: BLE001 - 메시지에 입력 텍스트가 섞일 수 있어 종류만 적는다
        log(f"중단: 예상하지 못한 오류 {type(e).__name__}")
        return EXIT_FAILED
    log(f"끝: {json.dumps({k: job[k] for k in ('rows', 'encode_s', 'articles_per_s', 'tokens_per_s')})}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
