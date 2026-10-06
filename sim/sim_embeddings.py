"""Stand-in newsletter embeddings for the synthetic catalog (ADR 0025 A1.1).

The synthetic catalog has no embeddings and the request-time recommender needs one per
newsletter. This is NOT BGE-M3: every token gets a fixed 64-d standard-normal vector seeded
by the sha256 of its string, and a newsletter's embedding is

    normalize( sum of its keyword vectors + its category vector )

So two newsletters are close when they share keywords or a category. Those are the very
fields the click model reads, which means an embedding-based ranker is structurally favored
in this world (the content-based bias ADR 0019 spells out). The experiments built on this
ask whether an off-policy estimate matches the measured value in the same world; they make
no claim about how good the ranker is.
"""

import hashlib
from typing import Dict, Iterable, Optional

import numpy as np

from sim.catalog import Catalog, Item

DIM = 64
NAMESPACE = "sim-embed-v1"


def token_vector(token: str, dim: int = DIM) -> np.ndarray:
    digest = hashlib.sha256(f"{NAMESPACE}:{token}".encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(digest[:8], "big"))
    return rng.standard_normal(dim)


def item_embedding(keywords: Iterable[str], category_id: Optional[int], dim: int = DIM) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float64)
    for kw in dict.fromkeys(keywords):  # a repeated keyword counts once
        v += token_vector(f"kw:{kw}", dim)
    v += token_vector(f"cat:{category_id}", dim)
    return (v / np.linalg.norm(v)).astype(np.float32)


def catalog_embeddings(catalog: Catalog) -> Dict[int, np.ndarray]:
    return {it.news_letter_id: item_embedding(it.keywords, it.category_id) for it in catalog.items}


def embed(item: Item) -> np.ndarray:
    return item_embedding(item.keywords, item.category_id)
