from datetime import timedelta

import numpy as np
import pytest

from app.recsys.config import RecsysConfig
from app.recsys.pipeline import (
    Deadline,
    RealtimeRecommender,
    build_user_state,
    generate_candidates,
)
from app.recsys.scoring import HeuristicScorer
from app.recsys.types import (
    SOURCE_COLD_CATEGORY,
    SOURCE_COLD_ONBOARDING,
    SOURCE_COLD_POPULAR,
    SOURCE_REALTIME,
)
from tests.recsys.fakes import NOW, FakeNewsletter, FakeRepo, FakeUser, axis_vec, two_topic_corpus

DIM = 16


def _recommender(cfg=None):
    cfg = cfg or RecsysConfig()
    return RealtimeRecommender(cfg, scorer=HeuristicScorer())


def _deadline():
    return Deadline(10.0)


def test_candidate_union_dedups_across_sources_and_respects_cap():
    corpus = two_topic_corpus(dim=DIM, per_topic=40)
    repo = FakeRepo(corpus, [FakeUser(1, long_term=axis_vec(DIM, 0), category_ids=[2])])
    cfg = RecsysConfig(knn_k=30, recent_n=30, popular_n=30, category_n=30, candidate_cap=50)
    state = build_user_state(repo, 1, NOW, cfg)
    state.short_term = axis_vec(DIM, 1)

    cands = generate_candidates(repo, state, cfg, NOW)

    assert len(cands.ids) == len(set(cands.ids))
    assert len(cands.ids) == 50
    # 캡에 걸려도 모든 생성기가 기여해야 한다(라운드 로빈 병합)
    assert set(cands.by_source) == {"knn_profile", "knn_short", "recent", "popular", "category"}
    for source, contributed in cands.contributed.items():
        assert contributed > 0, source


def test_candidates_are_limited_to_the_freshness_window_except_recent():
    stale = FakeNewsletter(999, axis_vec(DIM, 0), NOW - timedelta(days=30))
    corpus = two_topic_corpus(dim=DIM, per_topic=5) + [stale]
    repo = FakeRepo(corpus, [FakeUser(1, long_term=axis_vec(DIM, 0))])
    cfg = RecsysConfig(freshness_hours=72, recent_n=5)

    cands = generate_candidates(repo, build_user_state(repo, 1, NOW, cfg), cfg, NOW)

    assert 999 not in cands.by_source["knn_profile"]
    assert 999 not in cands.by_source["popular"]


def test_personal_user_gets_realtime_list_without_already_clicked_items():
    corpus = two_topic_corpus(dim=DIM, per_topic=15)
    repo = FakeRepo(corpus, [FakeUser(1, long_term=axis_vec(DIM, 0))])
    repo.click(1, 1, NOW - timedelta(days=3))  # 오래된 클릭: 단기 벡터에는 안 들어가지만 제외 대상

    rec = _recommender().recommend(repo, 1, NOW, _deadline())

    assert rec.source == SOURCE_REALTIME
    assert 1 not in rec.news_letter_ids
    assert 0 < len(rec.news_letter_ids) <= 20
    assert len(rec.scores) == len(rec.news_letter_ids)


def test_brand_new_user_with_onboarding_newsletters_uses_their_mean():
    corpus = two_topic_corpus(dim=DIM, per_topic=15)
    topic_b_ids = [n.id for n in corpus if n.category_ids == (2,)]
    repo = FakeRepo(corpus, [FakeUser(7, onboarding_ids=topic_b_ids[:2])])

    rec = _recommender(RecsysConfig(top_k=5)).recommend(repo, 7, NOW, _deadline())

    assert rec.source == SOURCE_COLD_ONBOARDING
    assert len(rec.news_letter_ids) == 5
    # 온보딩에서 고른 topic B 쪽이 상위를 차지해야 한다
    assert sum(i in topic_b_ids for i in rec.news_letter_ids) >= 4


def test_new_user_with_only_categories_uses_category_centroid():
    corpus = two_topic_corpus(dim=DIM, per_topic=15)
    topic_a_ids = {n.id for n in corpus if n.category_ids == (1,)}
    repo = FakeRepo(corpus, [FakeUser(8, category_ids=[1])])

    rec = _recommender(RecsysConfig(top_k=5)).recommend(repo, 8, NOW, _deadline())

    assert rec.source == SOURCE_COLD_CATEGORY
    assert sum(i in topic_a_ids for i in rec.news_letter_ids) >= 4


@pytest.mark.parametrize("user_exists", [True, False])
def test_user_without_any_signal_gets_non_empty_popular_list(user_exists):
    corpus = two_topic_corpus(dim=DIM, per_topic=15)
    users = [FakeUser(9)] if user_exists else []
    repo = FakeRepo(corpus, users)

    rec = _recommender().recommend(repo, 9, NOW, _deadline())

    assert rec.source == SOURCE_COLD_POPULAR
    assert len(rec.news_letter_ids) == 20
    assert "knn_ids" not in repo.calls


def test_onboarding_is_skipped_when_long_term_vector_exists():
    corpus = two_topic_corpus(dim=DIM, per_topic=5)
    repo = FakeRepo(corpus, [FakeUser(1, long_term=axis_vec(DIM, 0), onboarding_ids=[6])])

    build_user_state(repo, 1, NOW, RecsysConfig())

    assert "onboarding_vector" not in repo.calls
    assert "category_centroid" not in repo.calls


def test_item_embeddings_are_cached_across_requests():
    corpus = two_topic_corpus(dim=DIM, per_topic=10)
    repo = FakeRepo(corpus, [FakeUser(1, long_term=axis_vec(DIM, 0)), FakeUser(2, long_term=axis_vec(DIM, 1))])
    recommender = _recommender()

    recommender.recommend(repo, 1, NOW, _deadline())
    first_fetch = sum(len(f) for f in repo.item_fetches)
    recommender.recommend(repo, 2, NOW, _deadline())
    second_fetch = sum(len(f) for f in repo.item_fetches) - first_fetch

    assert first_fetch > 0
    assert second_fetch == 0


def test_expired_deadline_aborts_before_scoring():
    corpus = two_topic_corpus(dim=DIM, per_topic=5)
    repo = FakeRepo(corpus, [FakeUser(1, long_term=axis_vec(DIM, 0))])

    from app.recsys.pipeline import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        _recommender().recommend(repo, 1, NOW, Deadline(0.0))
    assert "items" not in repo.calls
