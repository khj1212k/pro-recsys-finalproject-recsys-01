"""The simulator driver against the *real* backend routers (not the fake).

Guards against the fake app drifting from backend/app/api/*. The backend runs
in-process on SQLite (only the tables the API touches; news_raw's TSVECTOR has
no SQLite rendering), with a stand-in for the nightly batch job that writes
news_letter_today_batch rows. Nothing here needs Postgres.

/newsletters/today exists in two generations, and the driver has to speak both:

  batch-only     reads the user's news_letter_today_batch row; no X-Rec-Source header
  request-time   (backend/app/recsys, branch feat/realtime-recommendation) answers through
                 RecommendationService - RECSYS_MODE=realtime or batch - and labels every
                 answer with X-Rec-Source

Which one the checked-out backend has is read off the package; the tests for the other
generation are skipped.
"""

import importlib.util
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Unit-test job only: the integration job installs neither the API stack nor passlib,
# and pytest collects every module even under -m integration.
pytest.importorskip("fastapi")
pytest.importorskip("passlib")

os.environ.setdefault("SECRET_KEY", "sim-contract-test")
os.environ.setdefault("ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from fastapi import Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402
from sqlalchemy import func, text  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

import app.security as backend_security  # noqa: E402
from app.database import get_session  # noqa: E402
from app.main import app as backend_app  # noqa: E402
from app.models.batch import NewsLettersCategory, NewsLetterTodayBatch  # noqa: E402
from app.models.log import UserNewsLetterCTRLog  # noqa: E402
from app.models.news import Category, NewsLetter, NewsLetterCategories  # noqa: E402
from app.models.user import User, UserPreferredCategories  # noqa: E402

from sim.catalog import synthetic_catalog  # noqa: E402
from sim.click_model import ClickModel, preset  # noqa: E402
from sim.driver import ApiClient, SimulationConfig, VirtualClock, run_simulation  # noqa: E402
from sim.metrics import compute_metrics  # noqa: E402
from sim.personas import PopulationConfig, generate_population  # noqa: E402
from sim.seed import NotDisposableError, seed_catalog, write_today_batches  # noqa: E402

# find_spec, not try/except ImportError: if the package is there, a broken import inside it must
# fail loudly instead of quietly selecting the batch-only tests.
REQUEST_TIME_API = importlib.util.find_spec("app.recsys") is not None
if REQUEST_TIME_API:
    from app.api.newsletter import get_request_repo  # noqa: E402
    from app.recsys.config import RecsysConfig  # noqa: E402
    from app.recsys.runtime import get_recommendation_service  # noqa: E402
    from app.recsys.service import build_service  # noqa: E402
    from app.recsys.types import NewsletterMeta  # noqa: E402

batch_only_api = pytest.mark.skipif(REQUEST_TIME_API, reason="this backend has the request-time /today (app.recsys)")
request_time_api = pytest.mark.skipif(not REQUEST_TIME_API, reason="this backend has the batch-only /today")

START = datetime(2026, 1, 5, tzinfo=timezone.utc)
N_DAYS = 3
API_TABLES = ("user", "category", "news_letter", "news_letter_categories", "news_letters_category",
              "news_letter_today_batch", "user_preferred_categories", "user_preferred_newsletter",
              "user_newsletter_ctr_log")
ALL_ENDPOINTS = {"signup", "login", "onboarding_news", "put_newsletters", "put_categories", "today", "click",
                 "detail"}


@pytest.fixture
def real_backend(monkeypatch):
    # bcrypt at the default cost dominates runtime; the contract does not depend on it
    monkeypatch.setattr(backend_security, "pwd_context", CryptContext(schemes=["bcrypt"], bcrypt__rounds=4))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[t] for t in API_TABLES])
    catalog = synthetic_catalog(n_days=N_DAYS, items_per_day=25, seed=1, start=START)
    with Session(engine) as s:
        assert seed_catalog(s, catalog)["newsletters"] == len(catalog.items)

    def session_override():
        with Session(engine) as s:
            yield s

    backend_app.dependency_overrides[get_session] = session_override
    yield engine, catalog
    backend_app.dependency_overrides.clear()


def nightly_batch_stand_in(engine, now):
    """sim.seed's batch writer: one news_letter_today_batch row per user from their onboarding categories."""
    with Session(engine) as s:
        write_today_batches(s, now)


def simulate(engine, clock, late_join_frac=0.25):
    users = generate_population(PopulationConfig(n_users=12, seed=0, n_days=N_DAYS, late_join_frac=late_join_frac,
                                                 drift_frac=0.0))
    # No `with`: the request-time app's lifespan would assemble its default service on the
    # Postgres DATABASE_URL. These tests install their own through dependency_overrides.
    client = TestClient(backend_app)
    log = run_simulation(
        users,
        make_api=lambda u, calls: ApiClient(client, calls=calls, user_index=u.index),
        model=ClickModel(preset("default")),
        cfg=SimulationConfig(n_days=N_DAYS, start=START),
        clock=clock,
        on_day_end=lambda day: nightly_batch_stand_in(engine, clock.now()),
    )
    return users, log, compute_metrics(log)


def assert_contract_and_click_log(engine, users, log, m):
    """Every route answers, every user is in, and each click the simulator posted is one log row."""
    assert {c.endpoint for c in log.calls} >= ALL_ENDPOINTS
    assert m["errors"]["error_rate"] == 0.0
    assert m["onboarding"]["n_users_onboarded"] == len(users) and m["onboarding"]["n_onboarding_failures"] == 0
    with Session(engine) as s:
        emails = [u.user_email for u in s.exec(select(User)).all()]
        n_logged = len(s.exec(select(UserNewsLetterCTRLog)).all())
    assert sorted(emails) == sorted(u.email for u in users)
    assert m["engagement"]["clicks_attempted"] > 0
    assert n_logged == m["engagement"]["clicks_acked"] == m["engagement"]["clicks_attempted"]
    assert m["cold_start"]["n_late_joiners_with_views"] > 0


def after_click_pairs(log):
    """(view with a click, next view of the same session)."""
    by_session = {}
    for v in log.views:
        by_session.setdefault((v.user, v.day, v.session), []).append(v)
    return [(a, b) for vs in by_session.values() for a, b in zip(vs, vs[1:]) if a.clicked_ids and a.ok and b.ok]


@batch_only_api
def test_driver_speaks_the_batch_only_api_and_measures_its_behavior(real_backend):
    engine, _ = real_backend
    users, log, m = simulate(engine, VirtualClock(START))

    assert_contract_and_click_log(engine, users, log, m)
    # Behavior of the batch-only design, measured through the public API only:
    # /today reads the last nightly batch, so a user who signs up during the day
    # sees an empty feed, and clicks do not change the feed until the next batch.
    assert m["cold_start"]["first_view_coverage"] == 0.0
    assert m["reactivity"]["after_click_jaccard_mean"] == 1.0
    assert m["serving"]["fallback_rate"] is None  # this API sends no source header


# --- request-time /today ------------------------------------------------------


def _utc(ts):
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


class SqliteRecsysRepo:
    """The request-time API's RecsysRepository on the SQLite test database.

    The production SqlRecsysRepository is Postgres + pgvector SQL (vector_send, ::timestamptz,
    = ANY) and is covered by that branch's Postgres integration tests. This stand-in answers the
    same questions from the same tables, so that the routers, the RecommendationService and the
    X-Rec-Source wiring under test are the real ones.

    Two things follow from the test setup, not from the API: the synthetic seed has no embeddings,
    so every vector lookup is None and the service takes its no-personal-signal path; and the
    whole virtual week is seeded up front, so newsletters "published" after the virtual now are
    hidden, as they would not exist yet.
    """

    def __init__(self, session, now_fn):
        self.session, self.now_fn = session, now_fn

    def rollback(self):
        self.session.rollback()

    def _displayable(self):
        rows = self.session.exec(
            select(NewsLetter.news_letter_id, NewsLetter.news_letter_created_at, NewsLetter.raw_news_count)
            .join(NewsLetterCategories, NewsLetter.news_letter_id == NewsLetterCategories.news_letter_id)).all()
        now = self.now_fn()
        return [(nid, _utc(ts), n) for nid, ts, n in rows if _utc(ts) <= now]

    def last_click_id(self, user_id):
        return self.session.exec(select(func.max(UserNewsLetterCTRLog.log_id))
                                 .where(UserNewsLetterCTRLog.user_id == user_id)).one()

    def long_term_and_categories(self, user_id):
        cats = self.session.exec(select(UserPreferredCategories.category_id)
                                 .where(UserPreferredCategories.user_id == user_id)).all()
        return None, sorted(cats)

    def short_term_vector(self, user_id, since, limit):
        return None

    def onboarding_vector(self, user_id):
        return None

    def category_centroid(self, category_ids, since):
        return None

    def window_meta(self, since):
        return [NewsletterMeta(nid, ts, n) for nid, ts, n in self._displayable() if ts >= since]

    def recent_ids(self, n):
        return [nid for nid, _, _ in sorted(self._displayable(), key=lambda r: (r[1], r[0]), reverse=True)[:n]]

    def clicked_among(self, user_id, news_letter_ids):
        clicked = self.session.exec(select(UserNewsLetterCTRLog.news_letter_id)
                                    .where(UserNewsLetterCTRLog.user_id == user_id)).all()
        return set(clicked) & set(news_letter_ids)

    def fatigued_among(self, user_id, news_letter_ids, since, min_impressions):
        return set()  # no impression log in this SQLite stand-in (the writer is not installed here)

    def displayable_among(self, news_letter_ids):
        return {nid for nid, _, _ in self._displayable()} & set(news_letter_ids)

    def latest_batch(self, user_id):
        row = self.session.exec(
            select(NewsLetterTodayBatch).where(NewsLetterTodayBatch.user_id == user_id)
            .order_by(NewsLetterTodayBatch.created_at.desc(), NewsLetterTodayBatch.news_letter_batch_id.desc())
            .limit(1)).first()
        return None if row is None else (_utc(row.created_at), [int(i) for i in row.news_letter_ids or []])


@contextmanager
def request_time_service(engine, clock, mode):
    """Installs a RecommendationService in `mode` that reads the SQLite test DB on the virtual clock.

    Without this the app would build its default service on DATABASE_URL and try to reach Postgres."""

    @contextmanager
    def repo_scope():
        with Session(engine) as s:
            yield SqliteRecsysRepo(s, clock.now)

    def request_repo(session: Session = Depends(get_session)):
        return SqliteRecsysRepo(session, clock.now)

    service = build_service(
        # the budget is a wall-clock limit; generous, so a slow test machine cannot turn answers into fallbacks
        RecsysConfig(mode=mode, time_budget_ms=30_000),
        repo_factory=repo_scope,
        now_fn=clock.now,  # batch freshness and the popularity window follow the virtual clock
        clock=lambda: clock.now().timestamp(),  # ...and so does the answer cache's TTL
    )
    backend_app.dependency_overrides[get_recommendation_service] = lambda: service
    backend_app.dependency_overrides[get_request_repo] = request_repo
    try:
        yield service
    finally:
        service.shutdown()


@request_time_api
def test_driver_reads_the_source_header_of_the_request_time_api_in_batch_mode(real_backend):
    engine, _ = real_backend
    clock = VirtualClock(START)
    with request_time_service(engine, clock, "batch"):
        users, log, m = simulate(engine, clock)

    assert_contract_and_click_log(engine, users, log, m)
    # RECSYS_MODE=batch: the nightly batch row is the answer, and a user without one (signed up
    # today) gets the popular list instead of the batch-only API's empty feed.
    sources = m["serving"]["source_counts"]
    assert set(sources) == {"batch", "popular"}
    assert m["cold_start"]["first_view_coverage"] == 1.0
    assert m["serving"]["empty_rate"] == 0.0
    assert m["serving"]["fallback_rate"] == pytest.approx(sources["popular"] / m["serving"]["n_views"])
    first_views = [v for v in log.views if v.is_first_view and users[v.user].is_late_joiner]
    assert first_views and {v.source for v in first_views} == {"popular"}
    # a batch row does not react to clicks
    batch_pairs = [(a, b) for a, b in after_click_pairs(log) if a.source == b.source == "batch"]
    assert batch_pairs and all(a.item_ids == b.item_ids for a, b in batch_pairs)


@request_time_api
def test_driver_reads_the_source_header_of_the_request_time_api_in_realtime_mode(real_backend):
    engine, _ = real_backend
    clock = VirtualClock(START)
    with request_time_service(engine, clock, "realtime"):
        users, log, m = simulate(engine, clock)

    assert_contract_and_click_log(engine, users, log, m)
    # RECSYS_MODE=realtime on a seed without embeddings: nobody has a personal signal, so every
    # answer is the request-time cold-start list. That is an answer by design, not a fallback.
    assert set(m["serving"]["source_counts"]) == {"cold_start_popular"}
    assert m["serving"]["fallback_rate"] == 0.0 and m["serving"]["empty_rate"] == 0.0
    assert m["cold_start"]["first_view_coverage"] == 1.0
    # ...and it reacts within the session: what was just clicked is gone from the next answer
    pairs = after_click_pairs(log)
    assert pairs and all(set(a.clicked_ids).isdisjoint(b.item_ids) for a, b in pairs)
    assert m["reactivity"]["after_click_jaccard_mean"] < 1.0


# --- sim.seed: the load-test seed goes through the same backend models --------

NOW = START + timedelta(hours=12)


def _user(s, email, codes=()):
    u = User(user_email=email, user_password_hash="x", user_nickname="n")
    s.add(u)
    s.flush()
    pk = {c.category_code: c.category_id for c in s.exec(select(Category)).all()}
    for code in codes:
        s.add(UserPreferredCategories(user_id=u.user_id, category_id=pk[code]))
    s.commit()
    return u.user_id


def test_seed_catalog_writes_what_the_api_reads_and_is_idempotent(real_backend):
    engine, catalog = real_backend
    with Session(engine) as s:
        assert len(s.exec(select(NewsLetter)).all()) == len(catalog.items)
        assert len(s.exec(select(NewsLetterCategories)).all()) == len(catalog.items)
        ranking = s.exec(select(NewsLettersCategory)).one().news_letter_ids
        assert ranking[0] == max(catalog.items, key=lambda it: it.created_at).news_letter_id
        again = seed_catalog(s, catalog)
    assert again["newsletters"] == 0 and again["skipped_existing"] == len(catalog.items)


def test_seed_batches_follow_onboarding_categories_within_the_freshness_window(real_backend):
    engine, catalog = real_backend
    with Session(engine) as s:
        sports = _user(s, "a@sim.invalid", codes=(600,))
        nothing = _user(s, "b@sim.invalid")
        assert write_today_batches(s, NOW) == 2
        rows = {r.user_id: r.news_letter_ids for r in s.exec(select(NewsLetterTodayBatch)).all()}
    by_id = catalog.by_id
    assert rows[sports] and all(by_id[i].category_id == 600 for i in rows[sports])
    assert all(NOW - timedelta(days=3) <= by_id[i].created_at <= NOW for i in rows[sports] + rows[nothing])
    fresh = sorted(catalog.candidates(NOW), key=lambda it: it.created_at, reverse=True)
    assert rows[nothing] == [it.news_letter_id for it in fresh[:20]]  # no onboarding -> newest


def test_seed_refuses_a_database_with_real_users_or_collected_articles(real_backend):
    engine, catalog = real_backend
    with Session(engine) as s:
        _user(s, "someone@example.com")
        with pytest.raises(NotDisposableError, match="non-synthetic"):
            write_today_batches(s, NOW)
        with pytest.raises(NotDisposableError, match="non-synthetic"):
            seed_catalog(s, catalog)

    fresh_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(fresh_engine, tables=[SQLModel.metadata.tables[t] for t in API_TABLES])
    with Session(fresh_engine) as s:
        s.execute(text("CREATE TABLE news_raw (raw_news_id INTEGER PRIMARY KEY)"))
        s.execute(text("INSERT INTO news_raw (raw_news_id) VALUES (1)"))
        s.commit()
        with pytest.raises(NotDisposableError, match="collection DB"):
            seed_catalog(s, catalog)
