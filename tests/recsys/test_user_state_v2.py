"""요청 시점 사용자 상태 v2(ADR 0033): 장기 벡터는 클릭마다 갱신하는 증분 상태에서, 단기 벡터와 어댑터 입력은
한 번 읽은 최근 클릭에서 나온다. 후보 생성기 구성은 recsys_core의 한곳에서 온다."""
from datetime import timedelta

import numpy as np
import pytest

from app.recsys.config import RecsysConfig
from app.recsys.pipeline import build_user_state, generate_candidates, load_popularity
from recsys_core import SERVING_CANDIDATE_SPEC, CandidateSpec
from recsys_core.profile import rebuild, unit_rows
from recsys_core.serving import SHORT_MAX_EVENTS, WindowCounts, epoch_seconds
from tests.recsys.fakes import NOW, FakeNewsletter, FakeRepo, FakeUser, axis_vec, two_topic_corpus

DIM = 16


def _cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


def test_long_term_vector_is_the_decayed_click_history_and_needs_no_profile_job():
    corpus = two_topic_corpus(dim=DIM, per_topic=10)
    by_id = {n.id: n for n in corpus}
    repo = FakeRepo(corpus, [FakeUser(1)])
    clicks = [(3, NOW - timedelta(days=20)), (14, NOW - timedelta(days=2)), (15, NOW - timedelta(hours=30))]
    for nid, at in clicks:
        repo.click(1, nid, at)

    state = build_user_state(repo, 1, NOW, RecsysConfig())

    want = rebuild((epoch_seconds(at), by_id[nid].embedding, 0) for nid, at in clicks)
    assert state.profile_source == "long_term" and state.profile is state.long_term
    assert _cos(state.long_term, want.hist_sum) == pytest.approx(1.0, abs=1e-6)
    assert state.hist.hist_len == 3 and state.hist_last_event_at == clicks[-1][1]
    # 20일 전 클릭(주제 A)보다 최근 클릭(주제 B)의 비중이 크다: 반감기 7일
    assert _cos(state.long_term, axis_vec(DIM, 1)) > _cos(state.long_term, axis_vec(DIM, 0))
    assert state.short_term is None and state.recent_clicks == []  # 24시간 안의 클릭은 없다


def test_a_user_without_clicks_has_no_long_term_vector_and_falls_to_the_cold_chain():
    corpus = two_topic_corpus(dim=DIM, per_topic=5)
    repo = FakeRepo(corpus, [FakeUser(1, onboarding_ids=[1, 2]), FakeUser(2)])

    onboarded = build_user_state(repo, 1, NOW, RecsysConfig())
    nobody = build_user_state(repo, 2, NOW, RecsysConfig())

    assert onboarded.long_term is None and onboarded.profile_source == "onboarding"
    assert onboarded.hist.empty and onboarded.hist_last_event_at is None
    assert nobody.profile is None and nobody.profile_source == "none" and not nobody.has_personal_signal


def test_short_term_vector_is_the_unit_sum_of_the_last_20_clicks_strictly_before_the_request():
    rng = np.random.default_rng(0)
    corpus = [FakeNewsletter(i, rng.standard_normal(DIM).astype(np.float32) * (1 + i % 3), NOW - timedelta(hours=1))
              for i in range(1, 41)]
    by_id = {n.id: n for n in corpus}
    repo = FakeRepo(corpus, [FakeUser(1)])
    for k in range(30):  # 최근 5시간 안에 30번
        repo.click(1, k + 1, NOW - timedelta(minutes=10 * (30 - k)))
    repo.click(1, 35, NOW - timedelta(hours=25))  # 24시간 밖
    repo.click(1, 36, NOW)                        # 요청과 같은 시각: 아직 이 요청의 것이 아니다
    repo.click(1, 37, NOW + timedelta(seconds=1))

    state = build_user_state(repo, 1, NOW, RecsysConfig())

    last20 = [by_id[k + 1].embedding for k in range(10, 30)]
    assert len(state.recent_clicks) == SHORT_MAX_EVENTS == 20
    assert [c.at for c in state.recent_clicks] == sorted(c.at for c in state.recent_clicks)
    assert all(c.at < NOW for c in state.recent_clicks)
    assert np.allclose(state.short_term, unit_rows(np.stack(last20)).sum(axis=0), atol=1e-6)


def test_a_narrower_configured_short_window_only_changes_the_heuristic_vector():
    corpus = two_topic_corpus(dim=DIM, per_topic=10)
    repo = FakeRepo(corpus, [FakeUser(1)])
    repo.click(1, 1, NOW - timedelta(hours=10))   # 주제 A
    repo.click(1, 12, NOW - timedelta(hours=1))   # 주제 B

    wide = build_user_state(repo, 1, NOW, RecsysConfig())
    narrow = build_user_state(repo, 1, NOW, RecsysConfig(short_term_hours=2, short_term_max_clicks=5))

    assert len(wide.recent_clicks) == len(narrow.recent_clicks) == 2  # 어댑터 입력은 설정과 무관하다(24시간·20건)
    clicked_b = next(n for n in corpus if n.id == 12).embedding
    assert _cos(narrow.short_term, clicked_b) == pytest.approx(1.0, abs=1e-6)  # 2시간 창: 주제 B 클릭 하나뿐
    assert _cos(wide.short_term, clicked_b) < 0.9                              # 24시간 창: 두 클릭의 합


def test_popularity_is_read_for_the_adapter_windows_which_end_before_the_request():
    corpus = two_topic_corpus(dim=DIM, per_topic=3)
    repo = FakeRepo(corpus, [FakeUser(1), FakeUser(2)])
    now = NOW.replace(microsecond=400_000)
    repo.click(2, 1, now - timedelta(seconds=0.2))   # 창의 끝(요청 초 - 2초) 뒤: 세지 않는다
    repo.click(2, 1, now - timedelta(hours=3))
    repo.click(1, 1, now - timedelta(hours=30))
    repo.impress(2, [1, 2], now - timedelta(hours=5))
    repo.impress(2, [1], now - timedelta(seconds=1))
    repo.impress(2, [2], now - timedelta(hours=25))

    counts = load_popularity(repo, [1, 2, 3], now)

    assert counts == {1: WindowCounts((1, 1, 2), 1), 2: WindowCounts((0, 0, 0), 1)}


def test_items_carry_their_primary_category():
    corpus = [FakeNewsletter(1, axis_vec(DIM, 0), NOW, category_ids=(5, 2)),
              FakeNewsletter(2, axis_vec(DIM, 1), NOW, category_ids=(3,))]
    repo = FakeRepo(corpus)

    items = repo.items([1, 2])

    assert (items[1].category_id, items[2].category_id) == (2, 3)


# ------------------------------------------------------------------ 후보 생성기 구성
def test_default_candidate_spec_is_the_one_place_shared_with_the_harness():
    assert RecsysConfig().candidate_spec() == SERVING_CANDIDATE_SPEC


def test_environment_overrides_change_values_but_not_the_sources_or_their_order():
    cfg = RecsysConfig.from_env({
        "RECSYS_KNN_K": "40", "RECSYS_RECENT_N": "30", "RECSYS_POPULAR_N": "20", "RECSYS_CATEGORY_N": "10",
        "RECSYS_CANDIDATE_CAP": "120", "RECSYS_FRESHNESS_HOURS": "48",
    })

    assert cfg.candidate_spec() == CandidateSpec(
        window_h=48.0,
        sources=(("knn_profile", 40), ("knn_short", 40), ("recent", 30), ("popular", 20), ("category", 10)),
        cap=120,
    )


def test_generate_candidates_follows_the_spec_order_ks_and_cap():
    corpus = two_topic_corpus(dim=DIM, per_topic=40)
    repo = FakeRepo(corpus, [FakeUser(1, long_term=axis_vec(DIM, 0), category_ids=[2])])
    repo.click(1, 45, NOW - timedelta(minutes=5))
    cfg = RecsysConfig(knn_k=7, recent_n=6, popular_n=5, category_n=4, candidate_cap=300)

    cands = generate_candidates(repo, build_user_state(repo, 1, NOW, cfg), cfg, NOW)

    assert list(cands.by_source) == [name for name, _ in cfg.candidate_spec().sources]
    assert {name: len(ids) for name, ids in cands.by_source.items()} == dict(cfg.candidate_spec().sources)
    # 라운드로빈: 앞 다섯 개는 출처마다 하나씩이다
    firsts = [cands.by_source[name][0] for name in cands.by_source]
    assert cands.ids[: len(set(firsts))] == list(dict.fromkeys(firsts))


def test_sources_without_a_signal_are_absent_not_empty():
    repo = FakeRepo(two_topic_corpus(dim=DIM, per_topic=5), [FakeUser(1, onboarding_ids=[1])])
    cfg = RecsysConfig()

    cands = generate_candidates(repo, build_user_state(repo, 1, NOW, cfg), cfg, NOW)

    assert list(cands.by_source) == ["knn_profile", "recent", "popular"]  # 최근 클릭도 선호 카테고리도 없다
