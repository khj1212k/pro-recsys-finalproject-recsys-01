from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def simulate(policy, seed=0, n_users=40, drift_frac=0.3, late_join_frac=0.2, session_for=None):
    """session_for(user, client) lets a test put a faulty transport in front of one user."""
    clock = VirtualClock(START)
    backend = FakeBackend(CATALOG, policy=policy, clock=clock, seed=seed)
    users = generate_population(PopulationConfig(n_users=n_users, seed=seed, n_days=N_DAYS, drift_day=2,
                                                 drift_frac=drift_frac, late_join_frac=late_join_frac))
    session_for = session_for or (lambda u, client: client)
    with TestClient(create_fake_app(backend)) as client:
        log = run_simulation(
            users,
            make_api=lambda u, calls: ApiClient(session_for(u, client), calls=calls, user_index=u.index),
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
    # the whole generated population entered the simulation
    assert m["onboarding"] == {"n_users_due": len(log.users), "n_users_onboarded": len(log.users),
                               "n_onboarding_failures": 0, "failures_by_endpoint": {}}
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


def test_reactive_policy_covers_new_users_and_reacts_to_clicks(reactive):
    _, _, r = reactive
    assert r["cold_start"]["first_view_coverage"] == 1.0
    assert r["reactivity"]["after_click_jaccard_mean"] < 0.9
    assert r["reactivity"]["similar_share_lift"] > 0


def test_drift_metrics_are_computed_for_the_drifting_users(reactive):
    # Only that the drift section is filled in. Which policy "adapts" better is not asserted:
    # the adaptation metrics failed their pre-registered direction check (ADR 0019, H5), and the
    # direction flips with the seed at this population size.
    _, log, r = reactive
    d = r["drift"]
    assert d["n_drifters"] == sum(u.is_drifter for u in log.users) > 0
    assert 0 < d["n_drifters_with_post_drift_views"] <= d["n_drifters"]
    assert d["pre_drift_new_core_share"] is not None and 0.0 <= d["adapted_rate"] <= 1.0
    assert d["censored"] == d["n_drifters_with_post_drift_views"] - round(
        d["adapted_rate"] * d["n_drifters_with_post_drift_views"])


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


class _FaultySession:
    """Passes requests through, except that one path answers with a canned response."""

    def __init__(self, inner, path, status, body):
        self.inner, self.path, self.status, self.body = inner, path, status, body

    def request(self, method, url, **kw):
        if url.endswith(self.path):
            return SimpleNamespace(status_code=self.status, headers={}, json=lambda: self.body)
        return self.inner.request(method, url, **kw)


def test_a_user_whose_onboarding_call_fails_is_counted_not_silently_dropped():
    def session_for(u, client):
        return _FaultySession(client, "/users/me/categories", 500, {"detail": "boom"}) if u.index == 3 else client

    _, log, m = simulate("reactive", n_users=12, drift_frac=0.0, late_join_frac=0.0, session_for=session_for)

    assert log.onboarding_failures == {3: "put_categories"}
    assert m["onboarding"] == {"n_users_due": 12, "n_users_onboarded": 11, "n_onboarding_failures": 1,
                               "failures_by_endpoint": {"put_categories": 1}}
    assert m["errors"]["by_endpoint"]["put_categories"]["errors"] == 1
    # logged in, so this user still reads - but as a user the server knows no categories for
    assert 3 not in log.onboarding and [v for v in log.views if v.user == 3]


def test_a_login_answer_without_a_token_is_an_onboarding_failure_and_the_user_has_no_views():
    # 200 with an unexpected body: no HTTP error to count, so without this accounting the
    # user would vanish from the population with error_rate still 0.
    def session_for(u, client):
        return _FaultySession(client, "/auth/login", 200, {"token": "wrong field"}) if u.index == 0 else client

    _, log, m = simulate("reactive", n_users=12, drift_frac=0.0, late_join_frac=0.0, session_for=session_for)

    assert log.onboarding_failures == {0: "login"}
    assert m["onboarding"]["n_users_onboarded"] == 11 and m["onboarding"]["n_onboarding_failures"] == 1
    assert m["errors"]["error_rate"] == 0.0
    assert not [v for v in log.views if v.user == 0]


def test_a_simulator_bug_during_onboarding_is_raised_not_recorded_as_a_failed_user(monkeypatch):
    def broken_onboard(self, day, now):
        raise RuntimeError("bug in the simulator, not an API failure")

    monkeypatch.setattr(SimAgent, "onboard", broken_onboard)
    with pytest.raises(RuntimeError, match="bug in the simulator"):
        simulate("reactive", n_users=5, drift_frac=0.0, late_join_frac=0.0)


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
    # the recovered 401 is flagged as a retry, not counted as an error
    assert [c.retried for c in api.calls[-3:]] == [True, False, False]
    assert all(c.ok for c in api.calls)


def test_a_second_401_after_relogin_is_an_error():
    clock = VirtualClock(START + timedelta(hours=12))
    backend = FakeBackend(CATALOG, policy="reactive", clock=clock)
    agent, api = _agent(backend)
    agent.ensure_account()
    api.session = _FaultySession(api.session, "/newsletters/today", 401, {"detail": "Could not validate credentials"})

    ev = agent.view(0, clock.now(), session=0, view_in_session=0, after_click=False)

    assert not ev.ok
    assert [(c.endpoint, c.status, c.ok, c.retried) for c in api.calls[-3:]] == [
        ("today", 401, True, True), ("login", 200, True, False), ("today", 401, False, False)]


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
