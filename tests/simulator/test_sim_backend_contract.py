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
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

import app.security as backend_security  # noqa: E402
from app.database import get_session  # noqa: E402
from app.main import app as backend_app  # noqa: E402
from app.models.batch import NewsLettersCategory, NewsLetterTodayBatch  # noqa: E402
from app.models.log import UserNewsLetterCTRLog  # noqa: E402
from app.models.news import Category, NewsLetter, NewsLetterCategories  # noqa: E402
from app.models.user import User, UserPreferredCategories  # noqa: E402

from sim.catalog import CATEGORIES, synthetic_catalog  # noqa: E402
from sim.click_model import ClickModel, preset  # noqa: E402
from sim.driver import ApiClient, SimulationConfig, VirtualClock, run_simulation  # noqa: E402
from sim.metrics import compute_metrics  # noqa: E402
from sim.personas import PopulationConfig, generate_population  # noqa: E402

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
        for i, (code, name) in enumerate(CATEGORIES.items(), start=1):
            s.add(Category(category_id=i, category_name=name, category_code=code))
        cat_pk = {code: i for i, code in enumerate(CATEGORIES, start=1)}
        for it in catalog.items:
            s.add(NewsLetter(news_letter_id=it.news_letter_id, news_letter_title=it.title,
                             news_letter_sentence=it.sentence, news_letter_content=it.sentence,
                             news_letter_created_at=it.created_at, news_letter_keywords=list(it.keywords),
                             raw_news_count=it.raw_news_count))
            s.add(NewsLetterCategories(news_letter_id=it.news_letter_id, category_id=cat_pk[it.category_id]))
        s.add(NewsLettersCategory(news_letter_ids=[it.news_letter_id for it in reversed(catalog.items)]))
        s.commit()

    def session_override():
        with Session(engine) as s:
            yield s

    backend_app.dependency_overrides[get_session] = session_override
    yield engine, catalog
    backend_app.dependency_overrides.clear()


def nightly_batch_stand_in(engine, catalog, now):
    """Writes one news_letter_today_batch row per user from their preferred categories."""
    code_of = {}
    with Session(engine) as s:
        for c in s.exec(select(Category)).all():
            code_of[c.category_id] = c.category_code
        fresh = [it for it in catalog.candidates(now)]
        fresh.sort(key=lambda it: it.created_at, reverse=True)
        for u in s.exec(select(User)).all():
            prefs = {code_of[p.category_id] for p in
                     s.exec(select(UserPreferredCategories).where(UserPreferredCategories.user_id == u.user_id))}
            ids = [it.news_letter_id for it in fresh if it.category_id in prefs][:20]
            if ids:
                s.add(NewsLetterTodayBatch(user_id=u.user_id, news_letter_ids=ids))
        s.commit()


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
            on_day_end=lambda day: nightly_batch_stand_in(engine, catalog, clock.now()),
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
