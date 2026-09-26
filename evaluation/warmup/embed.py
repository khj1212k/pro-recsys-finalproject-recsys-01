"""워밍업 1단계: 본문이 있고 임베딩이 없는 기사를 Stage3(main 코드 경로)로 임베딩하고 처리량을 잰다.

    python -m evaluation.warmup.embed --out <json> [--bench 200]

- Stage3_NewsEmbedding.execute()를 그대로 부른다(WHERE embedding_result IS NULL - 멱등).
  NewsEmbedder 생성자와 generate_embeddings_batch만 감싸서 모델 로드 시간·인코딩 시간·입력 토큰
  길이를 기록한다(동작은 바꾸지 않는다).
- --bench N: 이미 임베딩된 기사 N건을 Stage3와 같은 입력 구성(`f"{제목} {본문}"[:8000]`,
  max_length=8192, 배치 8, id 순)으로 메모리에서만 다시 인코딩해 처리량을 재고, DB에 저장된
  벡터와의 코사인으로 저장 경로가 같은 임베딩을 만들었는지 확인한다(DB 쓰기 없음).
리포트에는 id 개수·분포만 남긴다(본문 없음).
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Any, Dict, List, Optional

import numpy as np

from evaluation.warmup.common import cosine_rows, distribution, environment, sha256_ids, write_json

logger = logging.getLogger(__name__)

# Stage3_NewsEmbedding(ai_workspace/pipeline/stages.py)이 쓰는 입력 구성 - 바뀌면 여기도 맞춘다
STAGE3_CHAR_LIMIT = 8000
STAGE3_MAX_LENGTH = 8192  # core/embedder.py generate_embeddings_batch의 max_length


def stage3_text(title: Optional[str], content: Optional[str]) -> str:
    return f"{title} {content}"[:STAGE3_CHAR_LIMIT]


def token_length_summary(lengths: List[int], full_char_lengths: List[int]) -> Dict[str, Any]:
    """토큰 길이 분포와 절단 건수. lengths는 truncation 없이 센 토큰 수(특수 토큰 포함)."""
    return {
        "tokens": distribution(lengths),
        "n_over_2048_tokens": int(sum(1 for n in lengths if n > 2048)),
        "n_over_8192_tokens": int(sum(1 for n in lengths if n > STAGE3_MAX_LENGTH)),
        "n_char_truncated_at_8000": int(sum(1 for n in full_char_lengths if n > STAGE3_CHAR_LIMIT)),
        "chars_before_cut": distribution(full_char_lengths),
    }


def db_counts(cur) -> Dict[str, int]:
    cur.execute(
        """
        SELECT COUNT(*),
               COUNT(*) FILTER (WHERE raw_news_content <> ''),
               COUNT(*) FILTER (WHERE embedding_result IS NOT NULL),
               COUNT(*) FILTER (WHERE raw_news_content <> '' AND embedding_result IS NULL)
        FROM news_raw
        """
    )
    total, with_body, embedded, pending = cur.fetchone()
    return {"total": total, "with_body": with_body, "embedded": embedded, "pending": pending}


def _tokenizer():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained("BAAI/bge-m3")


def run_stage3(settings, batch_size: int) -> Dict[str, Any]:
    import core.embedder as embedder_module
    from pipeline.stages import Stage3_NewsEmbedding

    rec: Dict[str, Any] = {"model_load_s": 0.0, "encode_s": 0.0, "texts": 0, "batches": 0, "device": None,
                           "token_lengths": [], "char_lengths": []}
    orig_init = embedder_module.NewsEmbedder.__init__
    orig_batch = embedder_module.NewsEmbedder.generate_embeddings_batch
    tok = _tokenizer()

    def timed_init(self, *a, **kw):
        t0 = time.perf_counter()
        orig_init(self, *a, **kw)
        rec["model_load_s"] += time.perf_counter() - t0
        rec["device"] = self.device

    def timed_batch(self, texts, batch_size=20):
        out, elapsed = orig_batch(self, texts, batch_size)
        rec["encode_s"] += elapsed
        rec["texts"] += len(texts)
        rec["batches"] += 1
        # 통계용 토큰화는 타이밍 밖에서(Stage3 입력은 이미 8000자로 잘린 텍스트)
        rec["token_lengths"].extend(len(ids) for ids in tok(list(texts), truncation=False)["input_ids"])
        rec["char_lengths"].extend(len(t) for t in texts)
        return out, elapsed

    embedder_module.NewsEmbedder.__init__ = timed_init
    embedder_module.NewsEmbedder.generate_embeddings_batch = timed_batch
    try:
        t0 = time.perf_counter()
        embedded = Stage3_NewsEmbedding(settings).execute(batch_size=batch_size)
        wall = time.perf_counter() - t0
    finally:
        embedder_module.NewsEmbedder.__init__ = orig_init
        embedder_module.NewsEmbedder.generate_embeddings_batch = orig_batch

    return {
        "embedded": embedded,
        "device": rec["device"],
        "batch_size": batch_size,
        "model_load_s": round(rec["model_load_s"], 3),
        "encode_s": round(rec["encode_s"], 3),
        "wall_s": round(wall, 3),
        "batches": rec["batches"],
        "articles_per_s_encode": round(rec["texts"] / rec["encode_s"], 3) if rec["encode_s"] else None,
        "articles_per_s_wall": round(embedded / wall, 3) if wall and embedded else None,
        "input_tokens": distribution(rec["token_lengths"]) if rec["token_lengths"] else None,
    }


def bench_reembed(n: int, batch_size: int) -> Dict[str, Any]:
    """이미 임베딩된 기사 n건을 Stage3와 같은 방식으로 다시 인코딩(메모리 전용)."""
    from core.embedder import NewsEmbedder
    from core.clustering.hdbscan_clusterer import parse_embedding
    from db.connection import get_connection, release_connection

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT raw_news_id, raw_news_title, raw_news_content, embedding_result
                FROM news_raw
                WHERE embedding_result IS NOT NULL AND raw_news_content <> ''
                ORDER BY raw_news_id
                LIMIT %s
                """,
                (n,),
            )
            rows = cur.fetchall()
    finally:
        release_connection(conn)

    full_texts = [f"{r[1]} {r[2]}" for r in rows]
    texts = [stage3_text(r[1], r[2]) for r in rows]
    stored = np.vstack([parse_embedding(r[3]) for r in rows]).astype(np.float64)

    tok = _tokenizer()
    lengths = [len(ids) for ids in tok(texts, truncation=False)["input_ids"]]

    t_load = time.perf_counter()
    embedder = NewsEmbedder(verbose=False)
    load_s = time.perf_counter() - t_load
    try:
        vecs: List[List[float]] = []
        encode_s = 0.0
        per_batch = []
        for i in range(0, len(texts), batch_size):
            out, elapsed = embedder.generate_embeddings_batch(texts[i:i + batch_size], batch_size)
            vecs.extend(out)
            encode_s += elapsed
            per_batch.append(elapsed)
        device = embedder.device
    finally:
        embedder.cleanup()

    cos = cosine_rows(np.asarray(vecs, dtype=np.float64), stored)
    stored_norms = np.linalg.norm(stored, axis=1)
    return {
        "n": len(rows),
        "ids_sha256": sha256_ids(r[0] for r in rows),
        "device": device,
        "batch_size": batch_size,
        "model_load_s": round(load_s, 3),
        "encode_s": round(encode_s, 3),
        "articles_per_s_encode": round(len(rows) / encode_s, 3) if encode_s else None,
        "batch_seconds": distribution(per_batch),
        "input": token_length_summary(lengths, [len(t) for t in full_texts]),
        "cosine_vs_stored": distribution(cos.tolist()),
        "n_cosine_below_0_999": int(np.sum(cos < 0.999)),
        "stored_l2_norm": distribution(stored_norms.tolist()),
    }


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m evaluation.warmup.embed")
    parser.add_argument("--out", required=True)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--bench", type=int, default=0, help="이미 임베딩된 기사 N건 재인코딩 벤치(DB 쓰기 없음)")
    parser.add_argument("--skip-stage3", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from config.settings import Settings
    from db.connection import get_connection, release_connection

    batch_size = args.batch_size or Settings.EMBEDDING_BATCH_SIZE
    report: Dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": environment(),
        "input_construction": {
            "text": "f'{raw_news_title} {raw_news_content}'[:8000] (Stage3_NewsEmbedding)",
            "model": "BAAI/bge-m3 dense, FlagEmbedding BGEM3FlagModel, fp32 on mps/cpu",
            "max_length_tokens": STAGE3_MAX_LENGTH,
            "l2_normalize": True,
            "batch_size": batch_size,
        },
    }

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            report["db_before"] = db_counts(cur)
    finally:
        release_connection(conn)

    if not args.skip_stage3:
        report["stage3"] = run_stage3(Settings, batch_size)
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                report["db_after"] = db_counts(cur)
        finally:
            release_connection(conn)

    if args.bench:
        report["bench"] = bench_reembed(args.bench, batch_size)

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json(args.out, report)
    logger.info("wrote %s", args.out)


if __name__ == "__main__":
    main()
