import logging

from sqlalchemy import event
from sqlmodel import SQLModel, create_engine, Session
from typing import Generator
import os
from dotenv import load_dotenv
from pgvector.psycopg2 import register_vector

load_dotenv()

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

def _register_pgvector(dbapi_connection, connection_record):
    # 요청 시점 추천(app.recsys)의 KNN 쿼리는 numpy 배열을 파라미터로 넘긴다(ADR 0008
    # 규칙). 등록이 실패해도 로그인 등 벡터와 무관한 엔드포인트까지 막지 않도록
    # 연결 자체는 살려 두고, 벡터 쿼리가 실패하면 추천은 폴백 체인으로 떨어진다.
    try:
        register_vector(dbapi_connection)
    except Exception:
        logger.exception("pgvector register_vector failed; vector queries will fall back")
    finally:
        dbapi_connection.rollback()


def register_pgvector_on_connect(target_engine) -> None:
    event.listen(target_engine, "connect", _register_pgvector)


engine = create_engine(
    DATABASE_URL,
    connect_args={"options": "-c client_encoding=utf8"}
)
register_pgvector_on_connect(engine)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
