"""POST /logs/newsletter/click: 기존 프런트 계약({news_letter_id}만)을 지키면서 노출 연결키를 받는다 (ADR 0025).

실제 라우터를 SQLite 위에서 돌린다(클릭 로그 테이블의 컬럼 타입은 SQLite에서도 만들어진다).
PostgreSQL에서의 같은 계약은 tests/integration/test_logs_join.py가 본다.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.user_check import get_current_user
from app.database import get_session
from app.main import app
from app.models.log import UserNewsLetterCTRLog

TABLES = ("user", "news_letter", "user_newsletter_ctr_log")


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[SQLModel.metadata.tables[t] for t in TABLES])

    def session_override():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=7)

    def rows():
        with Session(engine) as s:
            return s.exec(select(UserNewsLetterCTRLog).order_by(UserNewsLetterCTRLog.log_id)).all()

    try:
        # with 없이 만든다: lifespan이 운영 추천 서비스를 조립하지 않게.
        yield SimpleNamespace(client=TestClient(app), rows=rows)
    finally:
        app.dependency_overrides.clear()


def test_the_existing_frontend_body_still_stores_a_click(api):
    resp = api.client.post("/logs/newsletter/click", json={"news_letter_id": 42})

    assert resp.status_code == 200
    assert resp.json() == {"status": "success", "log_id": 1}
    (row,) = api.rows()
    assert (row.user_id, row.news_letter_id, row.event) == (7, 42, "click")
    assert row.request_id is None and row.position is None and row.dwell_ms is None


def test_a_click_can_carry_the_request_id_and_position_of_the_impression(api):
    request_id = str(uuid.uuid4())

    resp = api.client.post(
        "/logs/newsletter/click",
        json={"news_letter_id": 42, "request_id": request_id, "position": 3},
    )

    assert resp.status_code == 200
    (row,) = api.rows()
    assert str(row.request_id) == request_id
    assert (row.position, row.event, row.dwell_ms) == (3, "click", None)


def test_a_detail_view_reports_dwell_time_as_its_own_event(api):
    request_id = str(uuid.uuid4())
    api.client.post("/logs/newsletter/click", json={"news_letter_id": 42, "request_id": request_id, "position": 0})

    resp = api.client.post(
        "/logs/newsletter/click",
        json={"news_letter_id": 42, "request_id": request_id, "position": 0,
              "event": "detail_view", "dwell_ms": 18_500},
    )

    assert resp.status_code == 200
    click, view = api.rows()
    assert (click.event, click.dwell_ms) == ("click", None)
    assert (view.event, view.dwell_ms) == ("detail_view", 18_500)
    assert view.request_id == click.request_id


@pytest.mark.parametrize(
    "extra",
    [
        {"event": "purchase"},
        {"position": -1},
        {"dwell_ms": -5},
        {"request_id": "not-a-uuid"},
        {"position": "third"},
    ],
)
def test_malformed_linkage_fields_are_rejected_and_nothing_is_stored(api, extra):
    resp = api.client.post("/logs/newsletter/click", json={"news_letter_id": 42, **extra})

    assert resp.status_code == 422
    assert api.rows() == []


def test_unknown_extra_fields_from_a_newer_client_do_not_break_the_click(api):
    resp = api.client.post("/logs/newsletter/click", json={"news_letter_id": 42, "surface": "home"})

    assert resp.status_code == 200
    assert len(api.rows()) == 1
