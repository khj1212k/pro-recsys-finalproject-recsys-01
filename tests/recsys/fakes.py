"""app.recsys.repository.RecsysRepository의 메모리 구현 (단위 테스트/마이크로 벤치마크용)."""
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

from app.recsys.types import Item, NewsletterMeta

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)


def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def axis_vec(dim: int, i: int, noise: float = 0.0, rng=None) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    if noise and rng is not None:
        v = v + rng.normal(0, noise, dim).astype(np.float32)
    return unit(v)


@dataclass
class FakeNewsletter:
    id: int
    embedding: np.ndarray
    created_at: datetime
    raw_news_count: int = 1
    category_ids: Tuple[int, ...] = (1,)


@dataclass
class FakeUser:
    id: int
    long_term: Optional[np.ndarray] = None
    category_ids: List[int] = field(default_factory=list)
    onboarding_ids: List[int] = field(default_factory=list)


class FakeRepo:
    def __init__(self, newsletters=(), users=(), batches=None):
        self.newsletters: Dict[int, FakeNewsletter] = {n.id: n for n in newsletters}
        self.users: Dict[int, FakeUser] = {u.id: u for u in users}
        self.clicks: List[Tuple[int, int, int, datetime]] = []  # (log_id, user_id, nl_id, at)
        self.batches: Dict[int, Tuple[datetime, List[int]]] = dict(batches or {})
        self.calls: List[str] = []
        self.item_fetches: List[List[int]] = []
        self._lock = threading.Lock()
        self.fail_on: Set[str] = set()

    def _call(self, name):
        with self._lock:
            self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError(f"injected failure in {name}")

    # --- helpers for tests ---
    def click(self, user_id: int, nl_id: int, at: datetime) -> int:
        log_id = len(self.clicks) + 1
        self.clicks.append((log_id, user_id, nl_id, at))
        return log_id

    # --- RecsysRepository ---
    def last_click_id(self, user_id):
        self._call("last_click_id")
        ids = [c[0] for c in self.clicks if c[1] == user_id]
        return max(ids) if ids else None

    def long_term_and_categories(self, user_id):
        self._call("long_term_and_categories")
        u = self.users.get(user_id)
        if u is None:
            return None, []
        return u.long_term, list(u.category_ids)

    def short_term_vector(self, user_id, since, limit):
        self._call("short_term_vector")
        recent = sorted(
            (c for c in self.clicks if c[1] == user_id and c[3] >= since),
            key=lambda c: c[3],
            reverse=True,
        )[:limit]
        vecs = [self.newsletters[c[2]].embedding for c in recent if c[2] in self.newsletters]
        return np.mean(vecs, axis=0) if vecs else None

    def onboarding_vector(self, user_id):
        self._call("onboarding_vector")
        u = self.users.get(user_id)
        if u is None or not u.onboarding_ids:
            return None
        return np.mean([self.newsletters[i].embedding for i in u.onboarding_ids], axis=0)

    def category_centroid(self, category_ids, since):
        self._call("category_centroid")
        vecs = [
            n.embedding
            for n in self.newsletters.values()
            if n.created_at >= since and set(n.category_ids) & set(category_ids)
        ]
        return np.mean(vecs, axis=0) if vecs else None

    def knn_ids(self, query, since, k):
        self._call("knn_ids")
        q = unit(query)
        pool = [n for n in self.newsletters.values() if n.created_at >= since]
        pool.sort(key=lambda n: -float(unit(n.embedding) @ q))
        return [n.id for n in pool[:k]]

    def recent_ids(self, n):
        self._call("recent_ids")
        ordered = sorted(self.newsletters.values(), key=lambda x: x.created_at, reverse=True)
        return [x.id for x in ordered[:n]]

    def window_meta(self, since):
        self._call("window_meta")
        return [
            NewsletterMeta(x.id, x.created_at, x.raw_news_count)
            for x in self.newsletters.values()
            if x.created_at >= since
        ]

    def category_recent_ids(self, category_ids, since, n):
        self._call("category_recent_ids")
        pool = [
            x
            for x in self.newsletters.values()
            if x.created_at >= since and set(x.category_ids) & set(category_ids)
        ]
        pool.sort(key=lambda x: x.created_at, reverse=True)
        return [x.id for x in pool[:n]]

    def clicked_among(self, user_id, news_letter_ids):
        self._call("clicked_among")
        wanted = set(news_letter_ids)
        return {c[2] for c in self.clicks if c[1] == user_id and c[2] in wanted}

    def items(self, news_letter_ids):
        self._call("items")
        with self._lock:
            self.item_fetches.append(list(news_letter_ids))
        out = {}
        for i in news_letter_ids:
            n = self.newsletters.get(i)
            if n is not None:
                out[i] = Item(i, n.embedding, n.created_at, n.raw_news_count)
        return out

    def latest_batch(self, user_id):
        self._call("latest_batch")
        return self.batches.get(user_id)


def two_topic_corpus(dim=16, per_topic=15, now=NOW, seed=0):
    """topic A(축 0 근처)와 topic B(축 1 근처) 뉴스레터를 같은 시각대에 만든다."""
    rng = np.random.default_rng(seed)
    newsletters = []
    nid = 1
    for topic_axis, cat in ((0, 1), (1, 2)):
        for j in range(per_topic):
            newsletters.append(
                FakeNewsletter(
                    id=nid,
                    embedding=axis_vec(dim, topic_axis, noise=0.15, rng=rng),
                    created_at=now - timedelta(hours=1 + j),
                    raw_news_count=1 + (j % 5),
                    category_ids=(cat,),
                )
            )
            nid += 1
    return newsletters
