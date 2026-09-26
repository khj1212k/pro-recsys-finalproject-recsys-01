"""BGE-M3 임베딩 처리량을 장치별로 같은 입력에 대해 잰다 (ADR 0006의 컨테이너 CPU vs 호스트 MPS 비교).

입력은 한 줄에 하나의 텍스트(JSON 문자열)인 JSONL이다. 텍스트 구성은 수집 잡과 같다
(`f"{title} {content}"[:8000]`, pipeline/stages.py::embed_pending_articles). DB에 쓰지 않는다.

    python scripts/bench_embedding_throughput.py texts.jsonl --device cpu --batch-size 8
"""
import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(REPO_ROOT / "ai_workspace"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("texts", type=Path)
    parser.add_argument("--device", choices=("cpu", "mps", "auto"), default="auto")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=1, help="측정에서 빼는 첫 배치 수(커널 준비/캐시)")
    args = parser.parse_args()

    texts = [json.loads(line) for line in args.texts.read_text(encoding="utf-8").splitlines() if line.strip()]

    import torch
    from core.embedder import NewsEmbedder

    load_started = time.monotonic()
    embedder = NewsEmbedder(force_cpu=(args.device == "cpu"), verbose=False)
    if args.device == "mps" and embedder.device != "mps":
        raise SystemExit("MPS를 쓸 수 없는 환경입니다")
    load_s = time.monotonic() - load_started

    batches = [texts[i:i + args.batch_size] for i in range(0, len(texts), args.batch_size)]
    warm, timed = batches[:args.warmup], batches[args.warmup:]
    for batch in warm:
        embedder.generate_embeddings_batch(batch, args.batch_size)
    encode_s = 0.0
    n = 0
    for batch in timed:
        _, elapsed = embedder.generate_embeddings_batch(batch, args.batch_size)
        encode_s += elapsed
        n += len(batch)

    print(json.dumps({
        "device": embedder.device,
        "machine": platform.machine(),
        "torch": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "omp_num_threads": os.getenv("OMP_NUM_THREADS"),
        "batch_size": args.batch_size,
        "texts_timed": n,
        "mean_chars": round(sum(len(t) for b in timed for t in b) / max(n, 1), 1),
        "model_load_s": round(load_s, 2),
        "encode_s": round(encode_s, 2),
        "articles_per_s": round(n / encode_s, 3) if encode_s else None,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
