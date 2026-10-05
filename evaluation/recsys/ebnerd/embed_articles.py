"""EB-NeRD 기사 전체를 BGE-M3(ai_workspace/core/embedder.py의 NewsEmbedder)로 임베딩한다.

torch/FlagEmbedding이 있는 별도 venv(.venv-embed)에서 실행한다:

    /path/to/.venv-embed/bin/python -m evaluation.recsys.ebnerd.embed_articles \
        --dataset-dir data/benchmarks/ebnerd/ebnerd_small \
        --out-dir data/benchmarks/ebnerd/derived/ebnerd_small

라이선스(EB-NeRD: 연구/비상업, 자체 IT 환경 한정) 때문에 입력/출력은 전부 이 Mac의
gitignore된 data/ 아래에 둔다. 기사 본문은 어떤 로그/리포트에도 출력하지 않는다
(토큰 길이 같은 통계만 기록).

입력 텍스트는 "제목\\n부제\\n본문"이고 토큰 단위로 max_length(기본 512)에서 절단한다.
절단 길이는 NewsEmbedder 생성자에서 정한다(max_length). 절단의 영향은 --truncation-check로
일부 긴 기사를 같은 모델에서 더 긴 max_length로도 임베딩해 두 벡터의 코사인을 meta.json에 남긴다.

기록된 v1 임베딩(sha256 2f096ef8…ebeb)은 고정 크기 배치(배치 32)의 NewsEmbedder로 계산했다.
배치 구성이 다른 구현(토큰 길이·attention 예산 기반 배치)으로 다시 돌리면 패딩이 달라져 fp16 수치 잡음
수준의 차이가 생길 수 있으므로 sha256이 같다고 가정하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

TEXT_TEMPLATE = "{title}\n{subtitle}\n{body}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_texts(articles: pd.DataFrame) -> list[str]:
    return [
        TEXT_TEMPLATE.format(title=t or "", subtitle=s or "", body=b or "").strip()
        for t, s, b in zip(articles["title"], articles["subtitle"], articles["body"])
    ]


@contextmanager
def max_length_override(embedder, max_length: int):
    """같은 모델로 잠깐 다른 절단 길이를 쓴다(모델 재로딩 없이). 끝나면 원래 값으로 되돌린다."""
    prev = embedder.max_length
    embedder.max_length = max_length
    try:
        yield embedder
    finally:
        embedder.max_length = prev


def embed_sorted(embedder, texts: list[str], lengths: np.ndarray, batch_size: int,
                 partial_dir: Path, chunk: int = 512) -> tuple[np.ndarray, list[dict]]:
    # 길이순 정렬로 배치 내 padding 낭비를 줄이고, 끝나면 원래 순서로 되돌린다.
    # 수 시간짜리 작업이라 청크마다 partial_dir에 저장해 중단 시 이어서 돌 수 있게 한다
    # (정렬이 stable이라 같은 입력이면 청크 구성이 동일하다).
    order = np.argsort(lengths, kind="stable")
    out = np.zeros((len(texts), 1024), dtype=np.float32)
    timings = []
    partial_dir.mkdir(parents=True, exist_ok=True)
    for start in range(0, len(order), chunk):
        idx = order[start:start + chunk]
        part = partial_dir / f"chunk_{start:07d}_{len(idx)}.npy"
        if part.exists():
            out[idx] = np.load(part).astype(np.float32)
            continue
        vecs, elapsed = embedder.generate_embeddings_batch([texts[i] for i in idx], batch_size=batch_size)
        out[idx] = np.asarray(vecs, dtype=np.float32)
        np.save(part, out[idx])
        timings.append({"n": int(len(idx)), "seconds": round(float(elapsed), 3),
                        "mean_tokens": float(np.minimum(lengths[idx], embedder.max_length).mean())})
        done = min(start + chunk, len(order))
        print(f"[embed] {done}/{len(order)} ({len(idx) / max(elapsed, 1e-9):.1f} art/s)", flush=True)
    return out, timings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--limit", type=int, default=None, help="스모크 테스트용 앞부분 N개만")
    ap.add_argument("--fp32", action="store_true", help="MPS에서도 fp32로 (기본은 fp16)")
    ap.add_argument("--truncation-check", type=int, default=100,
                    help="max_length보다 긴 기사 N개를 --check-max-length로 다시 임베딩해 코사인 비교")
    ap.add_argument("--check-max-length", type=int, default=2048)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    from core.embedder import NewsEmbedder

    dataset_dir = Path(args.dataset_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    articles_path = dataset_dir / "articles.parquet"
    articles = pd.read_parquet(articles_path, columns=["article_id", "title", "subtitle", "body"])
    if args.limit:
        articles = articles.head(args.limit)
    texts = build_texts(articles)

    t_load = time.time()
    embedder = NewsEmbedder(force_cpu=False, verbose=False, l2_normalize=True, max_length=args.max_length,
                            use_fp16=not args.fp32)
    load_seconds = time.time() - t_load
    tokenizer = embedder.model.tokenizer

    t0 = time.time()
    lengths = np.array([len(x) for x in tokenizer(texts, add_special_tokens=True, truncation=False)["input_ids"]])
    tokenize_seconds = time.time() - t0

    t0 = time.time()
    partial_dir = out_dir / f"partial_tsb{args.max_length}_n{len(texts)}"
    emb, timings = embed_sorted(embedder, texts, lengths, args.batch_size, partial_dir)
    embed_seconds = time.time() - t0

    check = None
    long_idx = np.flatnonzero(lengths > args.max_length)
    if args.truncation_check and len(long_idx):
        rng = np.random.default_rng(args.seed)
        pick = rng.choice(long_idx, size=min(args.truncation_check, len(long_idx)), replace=False)
        t0 = time.time()
        with max_length_override(embedder, args.check_max_length):
            long_vecs, _ = embedder.generate_embeddings_batch([texts[i] for i in pick], batch_size=4)
        long_vecs = np.asarray(long_vecs, dtype=np.float32)
        cos = (long_vecs * emb[pick]).sum(axis=1)
        check = {
            "n": int(len(pick)), "seed": args.seed,
            "compare_max_length": args.check_max_length,
            "population": f"tokens > {args.max_length}",
            "cosine_mean": float(cos.mean()), "cosine_p05": float(np.quantile(cos, 0.05)),
            "cosine_min": float(cos.min()), "seconds": round(time.time() - t0, 2),
        }
    embedder.cleanup()

    emb_path = out_dir / f"bge_m3_tsb{args.max_length}.f16.npy"
    ids_path = out_dir / "article_ids.npy"
    np.save(emb_path, emb.astype(np.float16))
    np.save(ids_path, articles["article_id"].to_numpy().astype(np.int64))

    import torch
    import FlagEmbedding
    meta = {
        "dataset_dir": str(dataset_dir.name),
        "articles_parquet_sha256": sha256_file(articles_path),
        "n_articles": int(len(articles)),
        "model": "BAAI/bge-m3",
        "model_snapshot": _hf_snapshot(),
        "text_template": TEXT_TEMPLATE,
        "max_length_tokens": args.max_length,
        "l2_normalized": True,
        "dtype_on_disk": "float16",
        "compute_fp16": not args.fp32,
        "device": embedder.device,
        "batch_size": args.batch_size,
        "torch": torch.__version__,
        "flagembedding": getattr(FlagEmbedding, "__version__", "1.2.5"),
        "machine": f"{platform.machine()} {platform.system()} {platform.mac_ver()[0]}",
        "token_length": {
            "median": float(np.median(lengths)), "p90": float(np.quantile(lengths, 0.9)),
            "max": int(lengths.max()),
            "frac_over_max_length": float((lengths > args.max_length).mean()),
        },
        "timing": {
            "model_load_seconds": round(load_seconds, 2),
            "tokenize_seconds": round(tokenize_seconds, 2),
            "embed_seconds": round(embed_seconds, 2),
            "articles_per_second": round(sum(c["n"] for c in timings) / max(sum(c["seconds"] for c in timings), 1e-9), 2),
            "note": "청크 합산 기준(재개 시 이미 저장된 청크 제외). 동시에 다른 작업이 돌던 공유 머신에서 측정",
            "chunks": timings,
        },
        "truncation_check": check,
        "embeddings_file": emb_path.name,
        "embeddings_sha256": sha256_file(emb_path),
        "article_ids_file": ids_path.name,
        "article_ids_sha256": sha256_file(ids_path),
    }
    (out_dir / f"bge_m3_tsb{args.max_length}.meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in meta.items() if k != "timing"}, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in meta["timing"].items() if k != "chunks"}))
    return 0


def _hf_snapshot() -> str | None:
    snap = Path(os.path.expanduser("~/.cache/huggingface/hub/models--BAAI--bge-m3/snapshots"))
    if snap.exists():
        names = sorted(p.name for p in snap.iterdir())
        return names[-1] if names else None
    return None


if __name__ == "__main__":
    raise SystemExit(main())
