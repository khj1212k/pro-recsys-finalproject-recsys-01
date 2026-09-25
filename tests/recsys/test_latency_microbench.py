"""요청 시점 추천의 프로세스 내 비용 마이크로 벤치마크 (DB 없음, CI 제외).

    .venv/bin/python -m pytest -q -s -m benchmark tests/recsys/test_latency_microbench.py

저장소는 미리 계산한 답을 O(1)로 돌려주는 StaticRepo라서, 측정값은 후보 합집합·아이템
캐시·스코어링·MMR·스레드 전환 같은 이 패키지 자체의 비용이다. 부하가 큰 머신에서
벽시계 시간이 흔들리므로 스레드 CPU 시간(time.thread_time)도 같이 낸다.
"""
import os
import time
from contextlib import contextmanager
from datetime import timedelta

import numpy as np
import pytest

from app.recsys.config import RecsysConfig
from app.recsys.pipeline import Deadline, RealtimeRecommender
from app.recsys.scoring import HeuristicScorer
from app.recsys.service import build_service
from app.recsys.types import Item, NewsletterMeta, UserState
from src.core.reranker import CategoryBasedMMRReranker
from tests.recsys.fakes import NOW

pytestmark = pytest.mark.benchmark

DIM, N_ITEMS, REPS = 1024, 300, 300


class StaticRepo:
    def __init__(self, rng):
        emb = rng.normal(size=(N_ITEMS, DIM)).astype(np.float32)
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        self.items_by_id = {
            i + 1: Item(i + 1, emb[i], NOW - timedelta(hours=i % 72), 1 + i % 7) for i in range(N_ITEMS)
        }
        ids = list(self.items_by_id)
        self.knn = ids[:100]
        self.recent = ids[:100]
        self.popular = ids[100:200]
        self.category = ids[200:250]
        self.meta = [NewsletterMeta(i, it.created_at, it.raw_news_count) for i, it in self.items_by_id.items()]
        self.long = emb[0]
        self.short = emb[1]

    def last_click_id(self, user_id):
        return None

    def long_term_and_categories(self, user_id):
        return self.long, [1, 2]

    def short_term_vector(self, user_id, since, limit):
        return self.short

    def onboarding_vector(self, user_id):
        return None

    def category_centroid(self, category_ids, since):
        return None

    def knn_ids(self, query, since, k):
        return self.knn[:k]

    def recent_ids(self, n):
        return self.recent[:n]

    def window_meta(self, since):
        return self.meta

    def category_recent_ids(self, category_ids, since, n):
        return self.category[:n]

    def clicked_among(self, user_id, ids):
        return set()

    def items(self, ids):
        return {i: self.items_by_id[i] for i in ids if i in self.items_by_id}

    def latest_batch(self, user_id):
        return None


def _pcts(samples_ms):
    s = sorted(samples_ms)
    return s[len(s) // 2], s[int(len(s) * 0.95) - 1]


def _measure(fn, reps=REPS):
    wall, cpu = [], []
    for _ in range(reps):
        w0, c0 = time.perf_counter(), time.thread_time()
        fn()
        cpu.append((time.thread_time() - c0) * 1000)
        wall.append((time.perf_counter() - w0) * 1000)
    return _pcts(wall), _pcts(cpu)


def _line(name, res):
    (w50, w95), (c50, c95) = res
    return f"{name:<34} wall p50={w50:7.2f}ms p95={w95:7.2f}ms | thread-cpu p50={c50:7.2f}ms p95={c95:7.2f}ms"


def test_in_process_latency_breakdown():
    rng = np.random.default_rng(0)
    repo = StaticRepo(rng)
    cfg = RecsysConfig(cache_ttl_s=0)
    recommender = RealtimeRecommender(cfg, scorer=HeuristicScorer())
    recommender.recommend(repo, 1, NOW, Deadline(10))  # 아이템 캐시 워밍

    state = UserState(user_id=1, long_term=repo.long, short_term=repo.short, profile=repo.long)
    items = list(repo.items_by_id.values())
    scorer = HeuristicScorer()
    scores = scorer.score(state, items, NOW).scores
    embs = np.stack([it.embedding for it in items])
    mmr = CategoryBasedMMRReranker()

    @contextmanager
    def factory():
        yield repo

    service = build_service(cfg, repo_factory=factory, now_fn=lambda: NOW)
    try:
        results = {
            "heuristic score (300 items)": _measure(lambda: scorer.score(state, items, NOW)),
            "MMR top20 from pool 80": _measure(lambda: mmr.rerank_for_user(scores, embs, 20, 2)),
            "recommender.recommend (warm)": _measure(lambda: recommender.recommend(repo, 1, NOW, Deadline(10))),
            "service.recommend (+thread hop)": _measure(lambda: service.recommend(1, fallback_repo=repo)),
        }
    finally:
        service.shutdown()

    load = os.getloadavg()[0]
    print(f"\n[recsys microbench] items={N_ITEMS} dim={DIM} reps={REPS} loadavg1={load:.2f}")
    for name, res in results.items():
        print(_line(name, res))
    # service.recommend의 thread-cpu는 호출 스레드 몫만 잡히므로(계산은 작업 스레드) 벽시계로 본다.
    (_, service_wall_p95), _ = results["service.recommend (+thread hop)"]
    assert service_wall_p95 < RecsysConfig().time_budget_ms
