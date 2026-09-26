"""sim.seed (load-test seed) on the Alembic schema in Postgres, not only on SQLite.

The unit tests in tests/simulator run the seed through the backend's SQLModel
tables on SQLite; here the same functions write to the migrated pgvector DB
(JSON columns, timestamp without time zone, serial ids). Everything happens in
one outer transaction that is rolled back, so the shared test DB is untouched.
"""
from datetime import datetime, timedelta, timezone


def test_seed_catalog_and_batches_on_the_migrated_schema(database_url):
    from sqlalchemy import create_engine
    from sqlmodel import Session, select

    from sim.catalog import synthetic_catalog
    from sim.seed import _models, seed_catalog, write_today_batches

    m = _models()
    now = datetime.now(timezone.utc)
    catalog = synthetic_catalog(n_days=2, items_per_day=15, seed=0, start=now - timedelta(days=2), warmup_days=0,
                                first_id=900_000)
    engine = create_engine(database_url)
    with engine.connect() as conn:
        outer = conn.begin()
        try:
            with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
                out = seed_catalog(s, catalog)
                assert out["newsletters"] == len(catalog.items)

                user = m["User"](user_email="seed-it@sim.invalid", user_password_hash="x", user_nickname="sim")
                s.add(user)
                s.flush()
                sports = s.exec(select(m["Category"]).where(m["Category"].category_code == 600)).one()
                s.add(m["UserPreferredCategories"](user_id=user.user_id, category_id=sports.category_id))
                s.commit()

                assert write_today_batches(s, now) == 1
                ids = s.exec(select(m["NewsLetterTodayBatch"]).where(
                    m["NewsLetterTodayBatch"].user_id == user.user_id)).one().news_letter_ids
                assert ids and all(catalog.by_id[i].category_id == 600 for i in ids)

                stored = s.exec(select(m["NewsLetter"]).where(
                    m["NewsLetter"].news_letter_id == ids[0])).one()
                assert stored.news_letter_keywords == list(catalog.by_id[ids[0]].keywords)
        finally:
            outer.rollback()
    engine.dispose()
