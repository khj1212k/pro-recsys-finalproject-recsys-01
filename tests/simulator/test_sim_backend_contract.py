"""The simulator driver against the *real* backend routers (not the fake).

Guards against the fake app drifting from backend/app/api/*. The backend runs
in-process on SQLite (only the tables the API touches; news_raw's TSVECTOR has
no SQLite rendering), with a stand-in for the nightly batch job that writes
news_letter_today_batch rows. Nothing here needs Postgres.
"""

import os
import sys
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

from fastapi.testclient import TestClient  # noqa: E402
from passlib.context import CryptContext  # noqa: E402
from sqlalchemy import text  # noqa: E402
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

START = datetime(2026, 1, 5, tzinfo=timezone.utc)
N_DAYS = 3
API_TABLES = ("user", "category", "news_letter", "news_letter_categories", "news_letters_category",
              "news_letter_today_batch", "user_preferred_categories", "user_preferred_newsletter",
              "user_newsletter_ctr_log")


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


def test_driver_speaks_the_real_api_contract_and_measures_its_behavior(real_backend):
    engine, catalog = real_backend
    clock = VirtualClock(START)
    users = generate_population(PopulationConfig(n_users=12, seed=0, n_days=N_DAYS, late_join_frac=0.25,
                                                 drift_frac=0.0))
    with TestClient(backend_app) as client:
        log = run_simulation(
            users,
            make_api=lambda u, calls: ApiClient(client, calls=calls, user_index=u.index),
            model=ClickModel(preset("default")),
            cfg=SimulationConfig(n_days=N_DAYS, start=START),
            clock=clock,
            on_day_end=lambda day: nightly_batch_stand_in(engine, clock.now()),
        )
    m = compute_metrics(log)

    assert {c.endpoint for c in log.calls} >= {"signup", "login", "onboarding_news", "put_newsletters",
                                                "put_categories", "today", "click", "detail"}
    assert m["errors"]["error_rate"] == 0.0

    with Session(engine) as s:
        emails = [u.user_email for u in s.exec(select(User)).all()]
        n_logged = len(s.exec(select(UserNewsLetterCTRLog)).all())
    assert sorted(emails) == sorted(u.email for u in users)
    assert m["engagement"]["clicks_attempted"] > 0
    assert n_logged == m["engagement"]["clicks_acked"] == m["engagement"]["clicks_attempted"]

    # Behavior of the current design, measured through the public API only:
    # /today reads the last nightly batch, so a user who signs up during the day
    # sees an empty feed, and clicks do not change the feed until the next batch.
    assert m["cold_start"]["n_late_joiners_with_views"] > 0
    assert m["cold_start"]["first_view_coverage"] == 0.0
    assert m["reactivity"]["after_click_jaccard_mean"] == 1.0
    assert m["serving"]["fallback_rate"] is None  # no source header in the real API


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
