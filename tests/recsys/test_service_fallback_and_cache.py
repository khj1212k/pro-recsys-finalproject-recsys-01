import time
from contextlib import contextmanager
from datetime import timedelta

import pytest

from app.recsys.config import RecsysConfig
from app.recsys.service import build_service
from app.recsys.types import (
    SOURCE_BATCH,
    SOURCE_EMPTY,
    SOURCE_POPULAR,
    SOURCE_REALTIME,
    SOURCE_RECENT,
)
from tests.recsys.fakes import NOW, FakeRepo, FakeUser, axis_vec, two_topic_corpus

DIM = 16


class SlowRepo(FakeRepo):
    def __init__(self, *a, delay_s=1.0, **kw):
        super().__init__(*a, **kw)
        self.delay_s = delay_s

    def last_click_id(self, user_id):
        time.sleep(self.delay_s)
        return super().last_click_id(user_id)


def _service(repo, cfg=None, clock=None, **kw):
    @contextmanager
    def factory():
        yield repo

    return build_service(
        cfg or RecsysConfig(),
        repo_factory=factory,
        now_fn=lambda: NOW,
        **({"clock": clock} if clock else {}),
        **kw,
    )


def _repo(batches=None, repo_cls=FakeRepo, **kw):
    return repo_cls(
        two_topic_corpus(dim=DIM, per_topic=15),
        [FakeUser(1, long_term=axis_vec(DIM, 0))],
        batches=batches,
        **kw,
    )


def test_realtime_timeout_falls_back_to_fresh_batch_within_budget():
    repo = _repo(batches={1: (NOW - timedelta(hours=5), [3, 2, 1])}, repo_cls=SlowRepo, delay_s=1.0)
    service = _service(repo, RecsysConfig(time_budget_ms=100))

    started = time.perf_counter()
    rec = service.recommend(1, fallback_repo=repo)
    elapsed = time.perf_counter() - started

    assert rec.source == SOURCE_BATCH
    assert rec.news_letter_ids == [3, 2, 1]
    assert rec.fallback_reason == "timeout"
    assert elapsed < 0.5
    assert service.counters.get("fallback.timeout") == 1


def test_realtime_exception_falls_back_and_is_counted():
    repo = _repo()
    repo.fail_on.add("knn_ids")
    service = _service(repo)

    rec = service.recommend(1, fallback_repo=repo)

    # 배치 행이 없으므로 체인의 다음 단계(인기)로 간다
    assert rec.source == SOURCE_POPULAR
    assert rec.fallback_reason == "error"
    # 표시 단계가 카테고리 없는 항목을 빼도 한 화면(20개)을 채울 수 있도록 넉넉히 준다
    assert len(rec.news_letter_ids) >= 20
    assert service.counters.get("fallback.error") == 1


def test_stale_batch_row_is_skipped_in_favour_of_popular():
    repo = _repo(batches={1: (NOW - timedelta(hours=37), [3, 2, 1])})
    repo.fail_on.add("knn_ids")
    service = _service(repo)

    rec = service.recommend(1, fallback_repo=repo)

    assert rec.source == SOURCE_POPULAR


def test_fallback_chain_reaches_recent_when_popular_fails_and_never_raises():
    repo = _repo()
    repo.fail_on.update({"knn_ids", "window_meta"})
    service = _service(repo)

    rec = service.recommend(1, fallback_repo=repo)
    assert rec.source == SOURCE_RECENT
    assert rec.news_letter_ids

    repo.fail_on.add("recent_ids")
    rec = service.recommend(1, fallback_repo=repo)
    assert rec.source == SOURCE_EMPTY
    assert rec.news_letter_ids == []
    assert service.counters.get("fallback.exhausted") == 1


def test_cache_hit_until_a_click_changes_the_key():
    repo = _repo()
    service = _service(repo)

    first = service.recommend(1, fallback_repo=repo)
    knn_calls_after_first = repo.calls.count("knn_ids")
    second = service.recommend(1, fallback_repo=repo)

    assert first.source == SOURCE_REALTIME
    assert second.cache_hit is True
    assert second.news_letter_ids == first.news_letter_ids
    assert second.request_id != first.request_id
    assert repo.calls.count("knn_ids") == knn_calls_after_first

    clicked = first.news_letter_ids[0]
    repo.click(1, clicked, NOW - timedelta(minutes=1))
    third = service.recommend(1, fallback_repo=repo)

    assert third.cache_hit is False
    assert clicked not in third.news_letter_ids
    assert service.counters.get("cache.hit") == 1
    assert service.counters.get("cache.miss") == 2


def test_cache_expires_after_ttl():
    repo = _repo()
    t = [0.0]
    service = _service(repo, RecsysConfig(cache_ttl_s=60), clock=lambda: t[0])

    service.recommend(1, fallback_repo=repo)
    t[0] = 61.0
    rec = service.recommend(1, fallback_repo=repo)

    assert rec.cache_hit is False


def test_fallback_results_are_not_cached():
    repo = _repo()
    repo.fail_on.add("knn_ids")
    service = _service(repo)
    service.recommend(1, fallback_repo=repo)

    repo.fail_on.clear()
    rec = service.recommend(1, fallback_repo=repo)

    assert rec.source == SOURCE_REALTIME


def test_batch_mode_serves_fallback_chain_without_realtime_work():
    repo = _repo(batches={1: (NOW - timedelta(hours=1), [5, 4])})
    service = _service(repo, RecsysConfig(mode="batch"))

    rec = service.recommend(1, fallback_repo=repo)

    assert rec.source == SOURCE_BATCH
    assert rec.fallback_reason is None
    assert "knn_ids" not in repo.calls
    assert "last_click_id" not in repo.calls


def test_invalid_mode_is_rejected():
    with pytest.raises(ValueError):
        RecsysConfig(mode="realtme")


def test_impressions_are_built_from_what_was_shown():
    repo = _repo()
    written = []
    service = _service(repo, impression_writer=written.extend)
    rec = service.recommend(1, fallback_repo=repo)
    shown = rec.news_letter_ids[:3]

    service.log_impressions(user_id=1, rec=rec, shown_ids=shown)

    assert [r["news_letter_id"] for r in written] == shown
    assert [r["position"] for r in written] == [0, 1, 2]
    assert {r["request_id"] for r in written} == {rec.request_id}
    assert all(r["source"] == SOURCE_REALTIME for r in written)
    assert all(r["model_version"] == rec.model_version for r in written)
    assert written[0]["score"] == pytest.approx(rec.scores[0])


def test_impression_write_failure_is_swallowed_and_counted():
    repo = _repo()

    def boom(rows):
        raise RuntimeError("db down")

    service = _service(repo, impression_writer=boom)
    rec = service.recommend(1, fallback_repo=repo)

    service.log_impressions(user_id=1, rec=rec, shown_ids=rec.news_letter_ids)

    assert service.counters.get("impressions.failed") == 1
