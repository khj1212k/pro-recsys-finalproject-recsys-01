"""RecsysRepository의 PostgreSQL(+pgvector) 구현.

규칙 두 가지 (ADR 0008/0015):
- 벡터 쿼리 파라미터는 numpy 배열로 넘긴다. app.database가 커넥션마다
  register_vector를 등록하므로 ndarray는 vector 리터럴로 바인딩된다. 파이썬 list는
  numeric[]로 바인딩돼 `<=>`가 "operator does not exist"로 실패한다.
- 벡터를 읽을 때는 vector_send()로 바이너리를 받아 np.frombuffer로 푼다. 텍스트
  표현을 파이썬에서 float로 파싱하면 300개 x 1024차원에서 수백 ms가 걸린다
  (ADR 0015 측정).
- `timestamp without time zone` 컬럼은 서버 기본 TimeZone 기준 벽시계 값으로
  저장돼 있다(NOW()/aware datetime 모두 세션 TimeZone으로 변환되어 저장됨).
  비교는 tz-aware 파라미터로, 읽기는 ::timestamptz로 해 같은 규칙으로 해석한다.
"""
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

import numpy as np
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.recsys import RecommendationImpressionLog
from app.recsys.types import Item, NewsletterMeta


def vector_from_send(buf) -> Optional[np.ndarray]:
    """pgvector vector_send() 바이너리: uint16 dim, uint16 unused, float32[dim] (big-endian)."""
    if buf is None:
        return None
    return np.frombuffer(bytes(buf), dtype=">f4", offset=4).astype(np.float32)


def _vec_param(v: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(v, dtype=np.float32)


_DISPLAYABLE = (
    "EXISTS (SELECT 1 FROM news_letter_categories c WHERE c.news_letter_id = n.news_letter_id)"
)
_IN_CATEGORIES = (
    "EXISTS (SELECT 1 FROM news_letter_categories c "
    "WHERE c.news_letter_id = n.news_letter_id AND c.category_id = ANY(:cats))"
)


class SqlRecsysRepository:
    def __init__(self, session: Session):
        self.session = session

    def _rows(self, sql: str, **params):
        return self.session.execute(text(sql), params).fetchall()

    def _scalar(self, sql: str, **params):
        return self.session.execute(text(sql), params).scalar()

    def last_click_id(self, user_id: int) -> Optional[int]:
        return self._scalar(
            "SELECT log_id FROM user_newsletter_ctr_log WHERE user_id = :uid "
            "ORDER BY created_at DESC, log_id DESC LIMIT 1",
            uid=user_id,
        )

    def long_term_and_categories(self, user_id: int) -> Tuple[Optional[np.ndarray], List[int]]:
        rows = self._rows(
            'SELECT vector_send(u.user_embedding), '
            "ARRAY(SELECT p.category_id FROM user_preferred_categories p "
            "      WHERE p.user_id = u.user_id ORDER BY p.category_id) "
            'FROM "user" u WHERE u.user_id = :uid',
            uid=user_id,
        )
        if not rows:
            return None, []
        vec, cats = rows[0]
        return vector_from_send(vec), list(cats or [])

    def short_term_vector(self, user_id: int, since: datetime, limit: int) -> Optional[np.ndarray]:
        return vector_from_send(
            self._scalar(
                "SELECT vector_send(AVG(t.e)) FROM ("
                "  SELECT n.news_letter_embedding AS e"
                "  FROM user_newsletter_ctr_log l"
                "  JOIN news_letter n ON n.news_letter_id = l.news_letter_id"
                "  WHERE l.user_id = :uid AND l.created_at >= :since"
                "    AND n.news_letter_embedding IS NOT NULL"
                "  ORDER BY l.created_at DESC LIMIT :lim) t",
                uid=user_id,
                since=since,
                lim=limit,
            )
        )

    def onboarding_vector(self, user_id: int) -> Optional[np.ndarray]:
        return vector_from_send(
            self._scalar(
                "SELECT vector_send(AVG(n.news_letter_embedding)) "
                "FROM user_preferred_newsletter p "
                "JOIN news_letter n ON n.news_letter_id = p.news_letter_id "
                "WHERE p.user_id = :uid",
                uid=user_id,
            )
        )

    def category_centroid(self, category_ids: Sequence[int], since: datetime) -> Optional[np.ndarray]:
        return vector_from_send(
            self._scalar(
                "SELECT vector_send(AVG(n.news_letter_embedding)) FROM news_letter n "
                "WHERE n.news_letter_created_at >= :since "
                "  AND n.news_letter_embedding IS NOT NULL AND " + _IN_CATEGORIES,
                since=since,
                cats=list(category_ids),
            )
        )

    def knn_ids(self, query: np.ndarray, since: datetime, k: int) -> List[int]:
        rows = self._rows(
            "SELECT n.news_letter_id FROM news_letter n "
            "WHERE n.news_letter_created_at >= :since AND n.news_letter_embedding IS NOT NULL "
            "ORDER BY n.news_letter_embedding <=> :q LIMIT :k",
            since=since,
            q=_vec_param(query),
            k=k,
        )
        return [r[0] for r in rows]

    def recent_ids(self, n: int) -> List[int]:
        rows = self._rows(
            "SELECT news_letter_id FROM news_letter "
            "ORDER BY news_letter_created_at DESC LIMIT :n",
            n=n,
        )
        return [r[0] for r in rows]

    def window_meta(self, since: datetime) -> List[NewsletterMeta]:
        rows = self._rows(
            "SELECT news_letter_id, news_letter_created_at::timestamptz, raw_news_count "
            "FROM news_letter WHERE news_letter_created_at >= :since",
            since=since,
        )
        return [NewsletterMeta(r[0], r[1], r[2]) for r in rows]

    def category_recent_ids(self, category_ids: Sequence[int], since: datetime, n: int) -> List[int]:
        rows = self._rows(
            "SELECT n.news_letter_id FROM news_letter n "
            "WHERE n.news_letter_created_at >= :since AND " + _IN_CATEGORIES + " "
            "ORDER BY n.news_letter_created_at DESC LIMIT :n",
            since=since,
            cats=list(category_ids),
            n=n,
        )
        return [r[0] for r in rows]

    def clicked_among(self, user_id: int, news_letter_ids: Sequence[int]) -> Set[int]:
        if not news_letter_ids:
            return set()
        rows = self._rows(
            "SELECT DISTINCT news_letter_id FROM user_newsletter_ctr_log "
            "WHERE user_id = :uid AND news_letter_id = ANY(:ids)",
            uid=user_id,
            ids=list(news_letter_ids),
        )
        return {r[0] for r in rows}

    def items(self, news_letter_ids: Sequence[int]) -> Dict[int, Item]:
        if not news_letter_ids:
            return {}
        rows = self._rows(
            "SELECT n.news_letter_id, vector_send(n.news_letter_embedding), "
            "       n.news_letter_created_at::timestamptz, n.raw_news_count "
            "FROM news_letter n "
            "WHERE n.news_letter_id = ANY(:ids) AND n.news_letter_embedding IS NOT NULL "
            "  AND " + _DISPLAYABLE,
            ids=list(news_letter_ids),
        )
        return {r[0]: Item(r[0], vector_from_send(r[1]), r[2], r[3]) for r in rows}

    def latest_batch(self, user_id: int) -> Optional[Tuple[datetime, List[int]]]:
        rows = self._rows(
            "SELECT created_at::timestamptz, news_letter_ids FROM news_letter_today_batch "
            "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 1",
            uid=user_id,
        )
        if not rows:
            return None
        created_at, ids = rows[0]
        return created_at, [int(i) for i in (ids or [])]


def create_recsys_engine(database_url: str, workers: int, time_budget_ms: int) -> Engine:
    """실시간 경로 전용 커넥션 풀(벌크헤드). API 요청은 인증 조회 때부터 앱 풀 커넥션을 쥔 채
    추천 결과를 기다리므로, 작업 스레드가 같은 풀에서 빌리면 동시 요청이 풀 크기에 닿을 때
    순환 대기가 생긴다. 작업 스레드 수만큼 따로 두면 작업 스레드는 풀을 기다리지 않고,
    앱 풀 사용량은 요청당 1개로 이전과 같다."""
    from app.database import register_pgvector_on_connect

    eng = create_engine(
        database_url,
        connect_args={"options": "-c client_encoding=utf8"},
        pool_size=workers,
        max_overflow=0,
        pool_timeout=max(0.001, time_budget_ms / 1000.0),
    )
    register_pgvector_on_connect(eng)
    return eng


@contextmanager
def sql_repo_scope(engine: Engine, statement_timeout_ms: Optional[int]) -> Iterator[SqlRecsysRepository]:
    """실시간 경로 전용 세션. statement_timeout을 트랜잭션 로컬로 걸어, 요청이 이미
    폴백으로 응답한 뒤에도 남은 작업 스레드가 오래 커넥션을 붙잡지 않게 한다."""
    with Session(engine) as session:
        if statement_timeout_ms:
            session.execute(
                text("SELECT set_config('statement_timeout', :v, true)"),
                {"v": f"{int(statement_timeout_ms)}ms"},
            )
        yield SqlRecsysRepository(session)


class SqlImpressionWriter:
    def __init__(self, engine: Engine):
        self.engine = engine

    def __call__(self, rows: List[dict]) -> None:
        payload = [dict(r, request_id=uuid.UUID(str(r["request_id"]))) for r in rows]
        with self.engine.begin() as conn:
            conn.execute(insert(RecommendationImpressionLog.__table__), payload)
