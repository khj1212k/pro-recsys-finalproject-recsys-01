"""요청 시점 추천의 프로세스 내 비용 마이크로 벤치마크 (DB 없음, CI 제외).

    .venv/bin/python -m pytest -q -s -m benchmark tests/recsys/test_latency_microbench.py

저장소는 미리 계산한 답을 O(1)로 돌려주는 StaticRepo라서, 측정값은 후보 합집합·아이템
캐시·스코어링·MMR·스레드 전환 같은 이 패키지 자체의 비용이다. 부하가 큰 머신에서
벽시계 시간이 흔들리므로 스레드 CPU 시간(time.thread_time)도 같이 낸다.
두 번째 테스트는 임베딩을 텍스트로 받아 파싱하는 비용과 바이너리로 받는 비용을 비교한다.
"""
import os
import time
from contextlib import contextmanager
from datetime import timedelta

import numpy as np
import pytest

from app.recsys.config import RecsysConfig
from app.recsys.pipeline import Deadline, RealtimeRecommender, build_recommendation
from app.recsys.scoring import HeuristicScorer
from app.recsys.service import build_service, rng_for_request
from app.recsys.types import ClickEvent, Item, NewsletterMeta, ProfileState, UserState
from recsys_core.profile import HistState
from recsys_core.serving import epoch_seconds
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

    def profile_state(self, user_id):
        hist = HistState(hist_sum=self.long.astype(np.float64), anchor_s=epoch_seconds(NOW) - 3600, hist_len=1,
                         cat_counts={0: 1})
        return ProfileState(hist, NOW - timedelta(hours=1)), [1, 2]

    def recent_clicks(self, user_id, since, until, limit):
        return [ClickEvent(NOW - timedelta(hours=1), self.short)]

    def item_window_counts(self, news_letter_ids, click_starts, inview_start, end):
        return {}

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

    def fatigued_among(self, user_id, ids, since, min_impressions):
        return set()

    def displayable_among(self, ids):
        return set(ids)

    def items(self, ids):
        return {i: self.items_by_id[i] for i in ids if i in self.items_by_id}

    def latest_batch(self, user_id):
        return None

    def rollback(self):
        pass


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
    # 탐색 칸을 넣는 단계만 따로(ADR 0025): 캐시가 적중한 요청이 추가로 치르는 비용이다.
    det = recommender.rank(repo, 1, NOW, Deadline(10))
    request_id = "0d7e0b0e-0000-4000-8000-000000000001"
    try:
        results = {
            "heuristic score (300 items)": _measure(lambda: scorer.score(state, items, NOW)),
            "MMR top20 from pool 80": _measure(lambda: mmr.rerank_for_user(scores, embs, 20, 2)),
            "exploration draw + slate (2 of 20)": _measure(
                lambda: build_recommendation(det, cfg, request_id, rng_for_request(request_id))
            ),
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


def test_vector_read_cost_text_vs_binary():
    """뉴스레터 임베딩 300개 x 1024차원을 DB에서 받아 numpy로 만드는 비용. pgvector의 텍스트
    표현("[0.1,0.2,...]")을 파이썬에서 파싱하는 경로와, vector_send() 바이너리를 np.frombuffer로
    푸는 경로(sql_repository.vector_from_send)를 같은 값으로 비교한다. DB 왕복은 포함하지 않는다."""
    import struct

    from pgvector import Vector

    from app.recsys.sql_repository import vector_from_send

    rng = np.random.default_rng(0)
    emb = rng.normal(size=(N_ITEMS, DIM)).astype(np.float32)
    # PostgreSQL의 vector 출력처럼 float4를 왕복 가능한 가장 짧은 십진수로 적는다
    texts = ["[" + ",".join(str(x) for x in row) + "]" for row in emb]
    binaries = [struct.pack(">HH", DIM, 0) + row.astype(">f4").tobytes() for row in emb]
    np.testing.assert_allclose(vector_from_send(binaries[0]), emb[0])

    reps = 30
    results = {
        "text: pgvector Vector.from_text": _measure(lambda: [Vector.from_text(t).to_numpy() for t in texts], reps),
        "text: float() per element": _measure(
            lambda: [np.asarray([float(x) for x in t[1:-1].split(",")], dtype=np.float32) for t in texts], reps
        ),
        "binary: np.frombuffer": _measure(lambda: [vector_from_send(b) for b in binaries], reps),
    }

    load = os.getloadavg()[0]
    print(f"\n[recsys vector read] vectors={N_ITEMS} dim={DIM} reps={reps} loadavg1={load:.2f}")
    for name, res in results.items():
        print(_line(name, res))
    (text_p50, _), _ = results["text: pgvector Vector.from_text"]
    (binary_p50, _), _ = results["binary: np.frombuffer"]
    assert binary_p50 < text_p50
