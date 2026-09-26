"""Seed a *disposable* database so the real API has something to serve under load.

    python -m sim.seed --database-url postgresql://USER:PW@127.0.0.1:5434/newsletter_load catalog --days 2
    python -m sim.seed --database-url postgresql://USER:PW@127.0.0.1:5434/newsletter_load batches

`catalog`   categories (missing codes only), synthetic newsletters published over
            the last `--days` days, their category links, and one ranking row in
            news_letters_category (what /onboarding/news reads). Skipped when the
            database already has newsletters.
`batches`   one news_letter_today_batch row per @sim.invalid user: the freshest
            newsletters in the user's onboarding categories (newest overall when
            the user has none). A stand-in for the nightly recommender, which needs
            embeddings and a trained model that this seed does not provide.

The synthetic newsletters have no embeddings, so request-time KNN (the realtime
branch) finds no candidates on this seed - see sim/README.md for what a seeded
load test does and does not exercise.

Refuses to write to a database that holds collected articles (any news_raw row)
or any non-synthetic user: fake newsletters must never reach the collection DB.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

from sim.catalog import CATEGORIES, Catalog, synthetic_catalog

SIM_EMAIL_DOMAIN = "@sim.invalid"
TODAY_SIZE = 20  # backend/app/api/newsletter.py returns the batch row's ids as-is; the app shows 20

_BACKEND = Path(__file__).resolve().parents[1] / "backend"


class NotDisposableError(RuntimeError):
    pass


def _models():
    """The backend's SQLModel tables (imported lazily: sim/ itself does not need the backend)."""
    if str(_BACKEND) not in sys.path:
        sys.path.insert(0, str(_BACKEND))
    from app.models.batch import NewsLettersCategory, NewsLetterTodayBatch
    from app.models.news import Category, NewsLetter, NewsLetterCategories
    from app.models.user import User, UserPreferredCategories

    return dict(Category=Category, NewsLetter=NewsLetter, NewsLetterCategories=NewsLetterCategories,
                NewsLettersCategory=NewsLettersCategory, NewsLetterTodayBatch=NewsLetterTodayBatch,
                User=User, UserPreferredCategories=UserPreferredCategories)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def check_disposable(session) -> None:
    from sqlalchemy import inspect, text
    from sqlmodel import select

    m = _models()
    tables = set(inspect(session.get_bind()).get_table_names())
    if "news_raw" in tables:
        n_raw = session.execute(text("SELECT COUNT(*) FROM news_raw")).scalar_one()
        if n_raw:
            raise NotDisposableError(f"news_raw has {n_raw} rows - this looks like the collection DB")
    User = m["User"]
    real = session.exec(select(User.user_email).where(User.user_email.not_like(f"%{SIM_EMAIL_DOMAIN}"))).all()
    if real:
        raise NotDisposableError(f"{len(real)} non-synthetic user(s) present - refusing to seed")


def seed_catalog(session, catalog: Catalog) -> dict:
    """Writes the catalog through the backend's own models; returns counts."""
    from sqlalchemy import func
    from sqlmodel import select

    m = _models()
    check_disposable(session)
    cat_pk: Dict[int, int] = {c.category_code: c.category_id for c in session.exec(select(m["Category"])).all()}
    added_categories = 0
    for code, name in CATEGORIES.items():
        if code not in cat_pk:
            row = m["Category"](category_name=name, category_code=code)
            session.add(row)
            session.flush()
            cat_pk[code] = row.category_id
            added_categories += 1
    existing = session.exec(select(func.count()).select_from(m["NewsLetter"])).one()
    if existing:
        session.commit()
        return {"newsletters": 0, "skipped_existing": existing, "categories_added": added_categories}
    for it in catalog.items:
        session.add(m["NewsLetter"](
            news_letter_id=it.news_letter_id, news_letter_title=it.title, news_letter_sentence=it.sentence,
            news_letter_content=it.sentence, news_letter_created_at=it.created_at,
            news_letter_keywords=list(it.keywords), raw_news_count=it.raw_news_count))
    session.flush()
    for it in catalog.items:
        session.add(m["NewsLetterCategories"](news_letter_id=it.news_letter_id, category_id=cat_pk[it.category_id]))
    newest_first = sorted(catalog.items, key=lambda it: (it.created_at, it.news_letter_id), reverse=True)
    session.add(m["NewsLettersCategory"](news_letter_ids=[it.news_letter_id for it in newest_first]))
    session.commit()
    return {"newsletters": len(catalog.items), "skipped_existing": 0, "categories_added": added_categories}


def _db_items(session) -> List[Tuple[int, int, datetime]]:
    """(news_letter_id, category_code, created_at) for every newsletter with a category."""
    from sqlmodel import select

    m = _models()
    NL, NLC, Cat = m["NewsLetter"], m["NewsLetterCategories"], m["Category"]
    rows = session.exec(select(NL.news_letter_id, Cat.category_code, NL.news_letter_created_at)
                        .join(NLC, NL.news_letter_id == NLC.news_letter_id)
                        .join(Cat, NLC.category_id == Cat.category_id)).all()
    return [(int(nid), int(code), _utc(ts)) for nid, code, ts in rows]


def write_today_batches(session, now: datetime, max_age: timedelta = timedelta(days=3), k: int = TODAY_SIZE) -> int:
    """One batch row per synthetic user; returns how many rows were written."""
    from sqlmodel import select

    m = _models()
    check_disposable(session)
    now = _utc(now)
    fresh = sorted(((nid, code, ts) for nid, code, ts in _db_items(session) if now - max_age <= ts <= now),
                   key=lambda r: (r[2], r[0]), reverse=True)
    code_of = {c.category_id: c.category_code for c in session.exec(select(m["Category"])).all()}
    written = 0
    User, UPC = m["User"], m["UserPreferredCategories"]
    for u in session.exec(select(User).where(User.user_email.like(f"%{SIM_EMAIL_DOMAIN}"))).all():
        prefs = {code_of[p.category_id] for p in session.exec(select(UPC).where(UPC.user_id == u.user_id)).all()}
        ids = [nid for nid, code, _ in fresh if code in prefs][:k] if prefs else []
        ids = ids or [nid for nid, _, _ in fresh][:k]
        if ids:
            session.add(m["NewsLetterTodayBatch"](user_id=u.user_id, news_letter_ids=ids))
            written += 1
    session.commit()
    return written


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--database-url", required=True,
                   help="disposable database only (deliberately not read from DATABASE_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("catalog")
    c.add_argument("--days", type=int, default=2)
    c.add_argument("--items-per-day", type=int, default=40)
    c.add_argument("--seed", type=int, default=0)
    sub.add_parser("batches")
    args = p.parse_args(argv)

    from sqlmodel import Session, create_engine

    engine = create_engine(args.database_url)
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        if args.cmd == "catalog":
            catalog = synthetic_catalog(n_days=args.days, items_per_day=args.items_per_day, seed=args.seed,
                                        start=now - timedelta(days=args.days), warmup_days=0)
            out = seed_catalog(session, catalog)
        else:
            out = {"batch_rows": write_today_batches(session, now)}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
