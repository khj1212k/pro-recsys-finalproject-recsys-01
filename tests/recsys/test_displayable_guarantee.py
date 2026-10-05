"""화면에 내보낼 수 없는(카테고리 매핑이 없는) 뉴스레터가 추천 ID에 섞이지 않는지 본다.

GET /newsletters/today의 표시 단계(hydrate_today_news)는 카테고리 매핑이 없는 뉴스레터를
뺀다. 추천 ID 목록에 그런 뉴스레터가 들어가면 "ID는 20개인데 응답 본문은 비어 있는" 상태가
된다 - 인기 상위가 전부 그런 뉴스레터일 때 신규 유저가 빈 화면을 받는다.
"""
from contextlib import contextmanager
from datetime import timedelta

from app.recsys.config import RecsysConfig
from app.recsys.service import build_service
from app.recsys.types import SOURCE_BATCH, SOURCE_COLD_POPULAR, SOURCE_POPULAR, SOURCE_RECENT
from tests.recsys.fakes import NOW, FakeNewsletter, FakeRepo, FakeUser, axis_vec, two_topic_corpus

DIM = 16
HIDDEN = list(range(1001, 1026))


def _repo(batches=None, users=None):
    # 가장 새롭고 기사 수도 가장 많아 인기·최신 양쪽에서 맨 앞에 오지만 카테고리가 없는 25개
    hidden = [
        FakeNewsletter(i, axis_vec(DIM, 0), NOW - timedelta(minutes=1), 500, category_ids=())
        for i in HIDDEN
    ]
    users = users if users is not None else [FakeUser(1, long_term=axis_vec(DIM, 0))]
    return FakeRepo(two_topic_corpus(dim=DIM, per_topic=15) + hidden, users, batches=batches)


def _service(repo, cfg=None):
    @contextmanager
    def factory():
        yield repo

    return build_service(cfg or RecsysConfig(), repo_factory=factory, now_fn=lambda: NOW)


def test_cold_start_popular_never_recommends_newsletters_the_screen_cannot_show():
    repo = _repo(users=[FakeUser(1)])
    service = _service(repo)

    rec = service.recommend(1, fallback_repo=repo)

    assert rec.source == SOURCE_COLD_POPULAR
    assert len(rec.news_letter_ids) == service.cfg.top_k
    assert not set(rec.news_letter_ids) & set(HIDDEN)


def test_popular_and_recent_fallbacks_only_return_displayable_newsletters():
    repo = _repo()
    repo.fail_on.add("knn_ids")
    service = _service(repo)

    popular = service.recommend(1, fallback_repo=repo)
    repo.fail_on.add("window_meta")
    recent = service.recommend(1, fallback_repo=repo)

    assert popular.source == SOURCE_POPULAR and popular.news_letter_ids
    assert recent.source == SOURCE_RECENT and recent.news_letter_ids
    assert not set(popular.news_letter_ids) & set(HIDDEN)
    assert not set(recent.news_letter_ids) & set(HIDDEN)


def test_batch_fallback_drops_ids_the_screen_cannot_show_and_keeps_the_batch_order():
    repo = _repo(batches={1: (NOW - timedelta(hours=1), [1001, 7, 1002, 3, 5])})
    service = _service(repo, RecsysConfig(mode="batch"))

    rec = service.recommend(1, fallback_repo=repo)

    assert rec.source == SOURCE_BATCH
    assert rec.news_letter_ids == [7, 3, 5]


def test_a_batch_row_with_nothing_displayable_moves_on_to_popular():
    repo = _repo(batches={1: (NOW - timedelta(hours=1), HIDDEN[:5])})
    service = _service(repo, RecsysConfig(mode="batch"))

    rec = service.recommend(1, fallback_repo=repo)

    assert rec.source == SOURCE_POPULAR
    assert rec.news_letter_ids and not set(rec.news_letter_ids) & set(HIDDEN)
    assert service.counters.get("fallback.batch_undisplayable") == 1
