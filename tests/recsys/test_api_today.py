"""GET /newsletters/today 라우터 배선: 응답 본문 계약 유지, 헤더, 백그라운드 노출 로그."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.newsletter import TodayNewsResponse, get_request_repo, get_today_hydrator
from app.api.user_check import get_current_user
from app.main import app
from app.recsys.config import RecsysConfig
from app.recsys.runtime import get_recommendation_service
from app.recsys.service import build_service
from tests.recsys.fakes import NOW, FakeRepo, FakeUser, axis_vec, two_topic_corpus

DIM = 16
FRONTEND_TODAY_FIELDS = {
    "news_letter_id",
    "news_letter_title",
    "news_letter_sentence",
    "news_letter_keywords",
    "news_letter_created_at",
    "raw_news_count",
    "category_id",
    "category_name",
}


def _fake_hydrate(ids):
    return [
        TodayNewsResponse(
            news_letter_id=i,
            news_letter_title=f"t{i}",
            news_letter_sentence="s",
            news_letter_keywords=["k"],
            news_letter_created_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
            raw_news_count=1,
            category_id=100,
            category_name="정치",
        )
        for i in ids[:20]
    ]


@pytest.fixture
def wired():
    repo = FakeRepo(two_topic_corpus(dim=DIM, per_topic=15), [FakeUser(1, long_term=axis_vec(DIM, 0))])
    written = []

    @contextmanager
    def factory():
        yield repo

    service = build_service(
        RecsysConfig(), repo_factory=factory, impression_writer=written.extend, now_fn=lambda: NOW
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=1)
    app.dependency_overrides[get_recommendation_service] = lambda: service
    app.dependency_overrides[get_request_repo] = lambda: repo
    app.dependency_overrides[get_today_hydrator] = lambda: _fake_hydrate
    try:
        yield SimpleNamespace(client=TestClient(app), repo=repo, service=service, written=written)
    finally:
        app.dependency_overrides.clear()
        service.shutdown()


def test_today_keeps_body_contract_and_sets_recsys_headers(wired):
    resp = wired.client.get("/newsletters/today")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and 0 < len(body) <= 20
    assert set(body[0]) == FRONTEND_TODAY_FIELDS
    assert resp.headers["X-Rec-Source"] == "realtime"
    assert resp.headers["X-Model-Version"] == "heuristic-v1"
    assert resp.headers["X-Request-Id"]


def test_impressions_are_logged_in_background_in_response_order(wired):
    resp = wired.client.get("/newsletters/today")

    shown = [item["news_letter_id"] for item in resp.json()]
    assert [r["news_letter_id"] for r in wired.written] == shown
    assert [r["position"] for r in wired.written] == list(range(len(shown)))
    assert {r["request_id"] for r in wired.written} == {resp.headers["X-Request-Id"]}


def test_fallback_is_visible_in_headers(wired):
    wired.repo.fail_on.add("knn_ids")

    resp = wired.client.get("/newsletters/today")

    assert resp.status_code == 200
    assert resp.json()
    assert resp.headers["X-Rec-Source"] == "popular"
    assert resp.headers["X-Model-Version"] == "popularity-v1"


def test_stats_endpoint_reports_counters(wired):
    wired.client.get("/newsletters/today")
    wired.client.get("/newsletters/today")

    stats = wired.client.get("/recsys/stats").json()

    assert stats["mode"] == "realtime"
    assert stats["counters"]["requests"] == 2
    assert stats["counters"]["cache.hit"] == 1
