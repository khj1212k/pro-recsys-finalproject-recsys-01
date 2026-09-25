"""Re-embed the team's archived newsletters with BGE-M3 (the export stored truncated vectors)."""
import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from core.embedder import NewsEmbedder  # noqa: E402

DEFAULT_EXPORT = REPO_ROOT / "data/team_archive/synthetic_dataset/newsletters_export.csv"
DEFAULT_OUT = REPO_ROOT / "data/team_archive/embeddings"


def load_newsletters(path: Path) -> list[dict]:
    csv.field_size_limit(sys.maxsize)
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: int(r["news_letter_id"]))
    return rows


def embedding_text(row: dict) -> str:
    # Same template as workflow/nodes.py generate_newsletter_embedding: f"{title} {content}".
    # The archive only keeps the saved (tone-converted) content, not the formal draft the
    # pipeline originally embedded.
    return f"{row['news_letter_title']} {row['news_letter_content']}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    rows = load_newsletters(args.export)
    texts = [embedding_text(r) for r in rows]
    ids = [int(r["news_letter_id"]) for r in rows]

    with NewsEmbedder() as embedder:
        vectors, elapsed = embedder.generate_embeddings_batch(texts, batch_size=args.batch_size)
        device = embedder.device

    matrix = np.asarray(vectors, dtype=np.float32)
    args.out.mkdir(parents=True, exist_ok=True)
    np.save(args.out / "newsletters_bge_m3.npy", matrix)
    meta = {
        "model": "BAAI/bge-m3",
        "device": device,
        "text_template": "{news_letter_title} {news_letter_content} (saved content, not formal draft)",
        "l2_normalized": True,
        "n": len(ids),
        "dim": int(matrix.shape[1]),
        "ids": ids,
        "source_sha256": hashlib.sha256(args.export.read_bytes()).hexdigest(),
        "vectors_sha256": hashlib.sha256(matrix.tobytes()).hexdigest(),
        "elapsed_sec": round(float(elapsed), 2),
    }
    (args.out / "newsletters_bge_m3.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in meta.items() if k != "ids"}, ensure_ascii=False))


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
