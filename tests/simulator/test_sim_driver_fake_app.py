from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")  # not installed in the integration CI job

from fastapi.testclient import TestClient  # noqa: E402

from sim.catalog import Item, synthetic_catalog
from sim.click_model import ClickModel, preset
from sim.driver import ApiClient, SimAgent, SimulationConfig, VirtualClock, run_simulation
from sim.fake_app import EXPLORE_SLOTS, FakeBackend, create_fake_app
from sim.metrics import compute_metrics
from sim.personas import PopulationConfig, generate_population

START = datetime(2026, 1, 5, tzinfo=timezone.utc)
N_DAYS = 4
CATALOG = synthetic_catalog(n_days=N_DAYS, items_per_day=30, seed=0, start=START)
ALL_ENDPOINTS = {"signup", "login", "onboarding_news", "put_newsletters", "put_categories", "today", "click", "detail"}


def simulate(policy, seed=0, n_users=40, drift_frac=0.3, late_join_frac=0.2):
    clock = VirtualClock(START)
    backend = FakeBackend(CATALOG, policy=policy, clock=clock, seed=seed)
    users = generate_population(PopulationConfig(n_users=n_users, seed=seed, n_days=N_DAYS, drift_day=2,
                                                 drift_frac=drift_frac, late_join_frac=late_join_frac))
    with TestClient(create_fake_app(backend)) as client:
        log = run_simulation(
            users,
            make_api=lambda u, calls: ApiClient(client, calls=calls, user_index=u.index),
            model=ClickModel(preset("default")),
            cfg=SimulationConfig(n_days=N_DAYS, start=START, seed=seed),
            clock=clock,
            on_day_end=lambda day: backend.rebuild_batches(),
        )
    return backend, log, compute_metrics(log)


@pytest.fixture(scope="module")
def reactive():
    return simulate("reactive")


@pytest.fixture(scope="module")
def static():
    return simulate("static_batch")


def test_run_exercises_whole_contract_and_data_flows_into_click_log(reactive):
    backend, log, m = reactive
    assert {c.endpoint for c in log.calls} == ALL_ENDPOINTS
    assert m["errors"]["error_rate"] == 0.0
    assert len(backend.users) == len(log.users)
    assert all(email.endswith("@sim.invalid") for email in backend.users)
    # every click the simulator posted is in the server-side log, and nothing else is
    assert m["engagement"]["clicks_attempted"] > 0
    assert m["engagement"]["click_ack_rate"] == 1.0
    assert len(backend.clicks) == m["engagement"]["clicks_attempted"]
    for u in log.users:
        stored = backend.users[u.email]
        assert stored.categories == list(log.onboarding[u.index].categories)
        assert stored.onboarding_ids == list(log.onboarding[u.index].newsletter_ids)


def test_static_batch_leaves_new_users_empty_and_ignores_clicks(static):
    _, _, m = static
    assert m["cold_start"]["n_late_joiners_with_views"] > 0
    assert m["cold_start"]["first_view_coverage"] == 0.0
    assert m["reactivity"]["n_after_click_pairs"] > 0
    assert m["reactivity"]["after_click_jaccard_mean"] == 1.0
    assert m["serving"]["empty_rate"] > 0
    # an empty answer is labelled like the request-time API's end of chain ("empty")
    assert m["serving"]["fallback_rate"] == m["serving"]["empty_rate"]


def test_reactive_policy_covers_new_users_reacts_and_follows_drift(reactive, static):
    _, _, r = reactive
    _, _, s = static
    assert r["cold_start"]["first_view_coverage"] == 1.0
    assert r["reactivity"]["after_click_jaccard_mean"] < 0.9
    assert r["reactivity"]["similar_share_lift"] > 0
    assert r["drift"]["pre_drift_new_core_share"] < 0.5
    assert r["drift"]["adapted_rate"] > s["drift"]["adapted_rate"] or (
        r["drift"]["requests_to_adapt_median"] < s["drift"]["requests_to_adapt_median"]
    )


def test_fallback_policy_is_counted_as_fallback():
    _, _, m = simulate("static_batch_fallback", n_users=30, drift_frac=0.0)
    assert m["cold_start"]["first_view_coverage"] == 1.0
    assert m["serving"]["fallback_rate"] > 0
    assert m["serving"]["empty_rate"] == 0.0


def test_same_seed_replays_identically_and_other_seed_differs():
    _, a, ma = simulate("reactive", seed=5, n_users=25)
    _, b, mb = simulate("reactive", seed=5, n_users=25)
    _, c, _ = simulate("reactive", seed=6, n_users=25)

    def trace(log):
        return [(v.user, v.day, v.t, v.item_ids, v.clicked_ids) for v in log.views]

    assert trace(a) == trace(b)
    for key in ("cold_start", "reactivity", "drift", "serving", "engagement"):
        assert ma[key] == mb[key]
    assert trace(a) != trace(c)


def _agent(backend, user_index=0):
    client = TestClient(create_fake_app(backend))
    user = generate_population(PopulationConfig(n_users=user_index + 1, seed=0))[user_index]
    api = ApiClient(client, user_index=user.index)
    return SimAgent(user, api, ClickModel(preset("default"))), api


def test_expired_token_triggers_one_relogin_and_the_request_succeeds():
    clock = VirtualClock(START + timedelta(hours=12))
    backend = FakeBackend(CATALOG, policy="reactive", clock=clock)
    agent, api = _agent(backend)
    agent.ensure_account()
    agent.onboard(0, clock.now())
    backend.expire_all_tokens()

    ev = agent.view(0, clock.now(), session=0, view_in_session=0, after_click=False)

    assert ev.ok and ev.item_ids
    tail = [(c.endpoint, c.status) for c in api.calls[-3:]]
    assert tail[0] == ("today", 401)
    assert tail[1] == ("login", 200)
    assert tail[2][0] == "today" and tail[2][1] == 200


def test_rerun_with_existing_account_logs_in_instead_of_failing():
    backend = FakeBackend(CATALOG, policy="reactive", clock=VirtualClock(START))
    agent, api = _agent(backend)
    agent.ensure_account()
    agent.ensure_account()
    assert [c.status for c in api.calls if c.endpoint == "signup"] == [201, 400]
    assert all(c.ok for c in api.calls)
    assert api.token


def test_item_payload_roundtrip_matches_today_contract():
    it = CATALOG.items[0]
    payload = it.to_api()
    assert set(payload) == {"news_letter_id", "news_letter_title", "news_letter_sentence", "news_letter_keywords",
                            "news_letter_created_at", "raw_news_count", "category_id", "category_name"}
    back = Item.from_api(payload)
    assert (back.news_letter_id, back.category_id, back.keywords, back.created_at, back.raw_news_count) == (
        it.news_letter_id, it.category_id, it.keywords, it.created_at, it.raw_news_count)
    assert back.press_names == ()


def test_reactive_explore_gives_fixed_slots_to_categories_outside_top_affinities():
    clock = VirtualClock(START + timedelta(days=1, hours=12))

    def top10_categories(policy):
        backend = FakeBackend(CATALOG, policy=policy, clock=clock, seed=0)
        api = ApiClient(TestClient(create_fake_app(backend)))
        user = generate_population(PopulationConfig(n_users=1, seed=0))[0]
        api.signup(user)
        api.login(user.email, user.password)
        api.put_categories([200])
        return [it.category_id for it in api.today().items[:10]]

    assert top10_categories("reactive") == [200] * 10
    explore = top10_categories("reactive_explore")
    assert [i for i, c in enumerate(explore) if c != 200] == list(EXPLORE_SLOTS)
