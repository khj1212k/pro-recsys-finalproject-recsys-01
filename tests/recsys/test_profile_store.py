"""장기 프로필 상태의 저장 형식과 클릭 API의 갱신 호출(ADR 0033). SQL 자체는 PostgreSQL 통합 테스트가 본다
(tests/integration/test_profile_state.py)."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.log import get_profile_updater
from app.api.user_check import get_current_user
from app.database import get_session
from app.main import app
from app.models.log import UserNewsLetterCTRLog
from app.recsys import profile_store
from recsys_core.profile import HistState, apply_event
from recsys_core.serving import epoch_seconds

AT = datetime(2026, 10, 6, 3, 4, 5, 678901, tzinfo=timezone.utc)


def test_hist_sum_round_trips_as_float64_bytes_without_losing_a_bit():
    rng = np.random.default_rng(0)
    vec = rng.standard_normal(1024) * 1e-3 + np.pi

    blob = profile_store.encode_hist_sum(vec)

    assert len(blob) == 1024 * 8
    assert np.array_equal(profile_store.decode_hist_sum(memoryview(blob)), vec)
    assert profile_store.encode_hist_sum(None) is None and profile_store.decode_hist_sum(None) is None


def test_category_counts_round_trip_through_json_with_integer_keys():
    text = profile_store.encode_cat_counts({3: 2, 0: 1})

    assert json.loads(text) == {"0": 1, "3": 2}
    assert profile_store.decode_cat_counts(text) == profile_store.decode_cat_counts(json.loads(text)) == {0: 1, 3: 2}
    assert profile_store.decode_cat_counts(None) == {}


def test_a_stored_row_reads_back_as_the_state_that_was_written():
    e = np.eye(8, dtype=np.float32)
    hist = apply_event(apply_event(HistState(), epoch_seconds(AT) - 86400, e[0], 3), epoch_seconds(AT), e[1], 0)

    state = profile_store.state_from_row(
        profile_store.encode_hist_sum(hist.hist_sum), AT, hist.hist_len, profile_store.encode_cat_counts(hist.cat_counts)
    )

    assert np.array_equal(state.hist.hist_sum, hist.hist_sum)
    assert (state.hist.anchor_s, state.hist.hist_len, state.hist.cat_counts) == (epoch_seconds(AT), 2, {3: 1, 0: 1})
    assert state.last_event_at == AT  # 마이크로초까지 남는다


@pytest.mark.parametrize("row", [(None, None, 0, None), (None, AT, 3, None), (b"\x00" * 64, None, 3, None),
                                 (b"\x00" * 64, AT, 0, None)])
def test_a_row_without_a_complete_state_reads_as_empty(row):
    state = profile_store.state_from_row(*row)

    assert state.hist.empty and state.last_event_at is None


def test_apply_click_does_nothing_on_a_non_postgres_connection():
    engine = create_engine("sqlite://")
    with engine.connect() as conn:
        assert profile_store.apply_click(conn, 1, 2, AT) is False


# ------------------------------------------------------------------ 클릭 API
@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(
        engine, tables=[SQLModel.metadata.tables[t] for t in ("user", "news_letter", "user_newsletter_ctr_log")]
    )
    calls = []
    behavior = SimpleNamespace(fail=False)

    def updater(conn, user_id, news_letter_id, clicked_at):
        # 같은 트랜잭션 안인지: 이 커넥션에서 방금 쓴(아직 커밋되지 않은) 클릭 행이 보인다
        visible = conn.exec_driver_sql("SELECT count(*) FROM user_newsletter_ctr_log").scalar()
        calls.append((user_id, news_letter_id, clicked_at, visible))
        if behavior.fail:
            conn.exec_driver_sql("INSERT INTO user_newsletter_ctr_log (user_id) VALUES (NULL)")  # NOT NULL 위반
        return True

    def session_override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=7)
    app.dependency_overrides[get_profile_updater] = lambda: updater

    def rows():
        with Session(engine) as s:
            return s.exec(select(UserNewsLetterCTRLog).order_by(UserNewsLetterCTRLog.log_id)).all()

    try:
        yield SimpleNamespace(client=TestClient(app), rows=rows, calls=calls, behavior=behavior)
    finally:
        app.dependency_overrides.clear()


def test_a_click_updates_the_profile_once_in_the_same_transaction_with_the_stored_click_time(api):
    resp = api.client.post("/logs/newsletter/click", json={"news_letter_id": 42})

    assert resp.status_code == 200
    (row,) = api.rows()
    ((user_id, nid, clicked_at, visible_rows),) = api.calls
    assert (user_id, nid, visible_rows) == (7, 42, 1)
    assert clicked_at.replace(tzinfo=None) == row.created_at.replace(tzinfo=None)


def test_a_detail_view_does_not_touch_the_profile(api):
    api.client.post("/logs/newsletter/click", json={"news_letter_id": 42, "event": "detail_view", "dwell_ms": 900})

    assert api.calls == [] and len(api.rows()) == 1


def test_a_failing_profile_update_keeps_the_click(api):
    api.behavior.fail = True

    resp = api.client.post("/logs/newsletter/click", json={"news_letter_id": 42})
    api.behavior.fail = False
    again = api.client.post("/logs/newsletter/click", json={"news_letter_id": 43})

    assert resp.status_code == again.status_code == 200
    assert [r.news_letter_id for r in api.rows()] == [42, 43]  # 실패한 갱신이 쓴 것은 남지 않고 클릭만 남는다
    assert len(api.calls) == 2


def test_the_default_updater_is_the_sql_profile_store():
    assert get_profile_updater() is profile_store.apply_click
    assert (AT + timedelta(0)).tzinfo is not None
