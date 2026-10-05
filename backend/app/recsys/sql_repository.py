"""RecsysRepository의 PostgreSQL(+pgvector) 구현.

규칙 세 가지 (ADR 0008/0015):
- 벡터 쿼리 파라미터는 numpy 배열로 넘긴다. app.database가 커넥션마다
  register_vector를 등록하므로 ndarray는 vector 리터럴로 바인딩된다. 파이썬 list는
  numeric[]로 바인딩돼 `<=>`가 "operator does not exist"로 실패한다.
- 벡터를 읽을 때는 vector_send()로 바이너리를 받아 np.frombuffer로 푼다. 텍스트
  표현을 파이썬에서 파싱하면 300개 x 1024차원에 수십 ms가 든다: float4 최단 표기 기준
  pgvector Vector.from_text 32~33ms, float() 직접 파싱 41~50ms이고 바이너리는 0.3ms다
  (M2, loadavg 4~8, nice 19, p50; `pytest -s -m benchmark tests/recsys/test_latency_microbench.py`,
  ADR 0015 증거 5). 처음에 "수백 ms"라고 적었던 값은 loadavg 100 이상에서 잰 것이라 버렸다.
- `timestamp without time zone` 컬럼은 서버 기본 TimeZone 기준 벽시계 값으로
  저장돼 있다(NOW()/aware datetime 모두 세션 TimeZone으로 변환되어 저장됨).
  비교는 tz-aware 파라미터로, 읽기는 ::timestamptz로 해 같은 규칙으로 해석한다.

클릭 로그에는 이벤트 종류가 있다(ADR 0025). 추천 경로가 "클릭"으로 읽는 것은 event = 'click' 행뿐이다 -
같은 클릭의 체류 보고('detail_view')까지 세면 단기 벡터에서 그 뉴스레터가 두 번 평균된다.

장기 프로필은 user_profile_state(클릭마다 갱신되는 증분 상태, app/recsys/profile_store.py)에서 읽는다.
"user".user_embedding은 더 읽지 않는다(ADR 0033).
"""
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

import numpy as np
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.recsys import RecommendationImpressionLog, RecommendationRequestLog
from app.recsys.pgvector_io import vector_from_send
from app.recsys.profile_store import STATE_COLUMNS, state_from_row
from app.recsys.types import ClickEvent, Item, NewsletterMeta, ProfileState, WindowCounts


def _vec_param(v: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(v, dtype=np.float32)


# 화면 응답(app/api/newsletter.py의 hydrate_today_news)은 카테고리 매핑이 있는 뉴스레터만
# 내보낸다. 추천 ID를 만드는 쪽(아이템 조회, 인기/최신 목록, 배치 행)이 같은 조건을 걸어야
# "ID는 있는데 응답 본문이 비는" 일이 없다. KNN은 걸지 않는다 - 뒤의 items()가 거른다.
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

    def rollback(self) -> None:
        self.session.rollback()

    def last_click_id(self, user_id: int) -> Optional[int]:
        return self._scalar(
            "SELECT log_id FROM user_newsletter_ctr_log WHERE user_id = :uid AND event = 'click' "
            "ORDER BY created_at DESC, log_id DESC LIMIT 1",
            uid=user_id,
        )

    def profile_state(self, user_id: int) -> Tuple[ProfileState, List[int]]:
        columns = ", ".join(f"s.{c}" for c in STATE_COLUMNS.split(", "))
        rows = self._rows(
            f"SELECT {columns}, "
            "ARRAY(SELECT p.category_id FROM user_preferred_categories p "
            "      WHERE p.user_id = u.user_id ORDER BY p.category_id) "
            'FROM "user" u LEFT JOIN user_profile_state s ON s.user_id = u.user_id '
            "WHERE u.user_id = :uid",
            uid=user_id,
        )
        if not rows:
            return ProfileState(), []
        *state_row, cats = rows[0]
        return state_from_row(*state_row), list(cats or [])

    def recent_clicks(
        self, user_id: int, since: datetime, until: datetime, limit: int
    ) -> List[ClickEvent]:
        rows = self._rows(
            "SELECT l.created_at::timestamptz, vector_send(n.news_letter_embedding), l.news_letter_id"
            "  FROM user_newsletter_ctr_log l"
            "  JOIN news_letter n ON n.news_letter_id = l.news_letter_id"
            "  WHERE l.user_id = :uid AND l.created_at >= :since AND l.created_at < :until"
            "    AND l.event = 'click' AND n.news_letter_embedding IS NOT NULL"
            # (초, 뉴스레터 ID) 순으로 가장 뒤의 것들. 같은 초의 클릭을 마이크로초로 가르지 않는다(저장소 계약 참고).
            "  ORDER BY date_trunc('second', l.created_at) DESC, l.news_letter_id DESC, l.log_id DESC"
            "  LIMIT :lim",
            uid=user_id,
            since=since,
            until=until,
            lim=limit,
        )
        return [ClickEvent(r[0], vector_from_send(r[1]), int(r[2])) for r in rows]

    def item_window_counts(
        self,
        news_letter_ids: Sequence[int],
        click_starts: Sequence[datetime],
        inview_start: datetime,
        end: datetime,
    ) -> Dict[int, WindowCounts]:
        if not news_letter_ids:
            return {}
        ids = list(news_letter_ids)
        # 클릭: 가장 넓은 창만큼 한 번 읽고 창마다 FILTER로 센다. ix_user_newsletter_ctr_log_news_letter_id_created_at.
        filters = ", ".join(
            f"COUNT(*) FILTER (WHERE created_at >= :s{j})" for j in range(len(click_starts))
        )
        clicks = self._rows(
            f"SELECT news_letter_id, {filters} FROM user_newsletter_ctr_log "
            "WHERE event = 'click' AND news_letter_id = ANY(:ids) "
            "  AND created_at >= :widest AND created_at < :end GROUP BY news_letter_id",
            ids=ids,
            widest=min(click_starts),
            end=end,
            **{f"s{j}": start for j, start in enumerate(click_starts)},
        )
        # 노출: 화면에 나간 칸 하나가 한 건. ix_recommendation_impression_log_news_letter_id_created_at.
        inviews = dict(
            self._rows(
                "SELECT news_letter_id, COUNT(*) FROM recommendation_impression_log "
                "WHERE news_letter_id = ANY(:ids) AND created_at >= :start AND created_at < :end "
                "GROUP BY news_letter_id",
                ids=ids,
                start=inview_start,
                end=end,
            )
        )
        zero = (0,) * len(click_starts)
        by_id = {r[0]: tuple(int(c) for c in r[1:]) for r in clicks}
        return {
            int(nid): WindowCounts(by_id.get(nid, zero), int(inviews.get(nid, 0)))
            for nid in set(by_id) | set(inviews)
        }

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
            "SELECT n.news_letter_id FROM news_letter n WHERE " + _DISPLAYABLE + " "
            "ORDER BY n.news_letter_created_at DESC LIMIT :n",
            n=n,
        )
        return [r[0] for r in rows]

    def window_meta(self, since: datetime) -> List[NewsletterMeta]:
        rows = self._rows(
            "SELECT n.news_letter_id, n.news_letter_created_at::timestamptz, n.raw_news_count "
            "FROM news_letter n WHERE n.news_letter_created_at >= :since AND " + _DISPLAYABLE,
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
            "WHERE user_id = :uid AND event = 'click' AND news_letter_id = ANY(:ids)",
            uid=user_id,
            ids=list(news_letter_ids),
        )
        return {r[0] for r in rows}

    def fatigued_among(
        self, user_id: int, news_letter_ids: Sequence[int], since: datetime, min_impressions: int
    ) -> Set[int]:
        if not news_letter_ids:
            return set()
        # ix_recommendation_impression_log_user_id_created_at가 (user_id, created_at DESC)라
        # 한 사용자의 최근 구간만 읽는다.
        rows = self._rows(
            "SELECT news_letter_id FROM recommendation_impression_log "
            "WHERE user_id = :uid AND created_at >= :since AND news_letter_id = ANY(:ids) "
            "GROUP BY news_letter_id HAVING COUNT(*) >= :n",
            uid=user_id,
            since=since,
            ids=list(news_letter_ids),
            n=min_impressions,
        )
        return {r[0] for r in rows}

    def displayable_among(self, news_letter_ids: Sequence[int]) -> Set[int]:
        if not news_letter_ids:
            return set()
        rows = self._rows(
            "SELECT DISTINCT c.news_letter_id FROM news_letter_categories c "
            "WHERE c.news_letter_id = ANY(:ids)",
            ids=list(news_letter_ids),
        )
        return {r[0] for r in rows}

    def items(self, news_letter_ids: Sequence[int]) -> Dict[int, Item]:
        if not news_letter_ids:
            return {}
        rows = self._rows(
            "SELECT n.news_letter_id, vector_send(n.news_letter_embedding), "
            "       n.news_letter_created_at::timestamptz, n.raw_news_count, "
            "       (SELECT MIN(c.category_id) FROM news_letter_categories c "
            "         WHERE c.news_letter_id = n.news_letter_id) "
            "FROM news_letter n "
            "WHERE n.news_letter_id = ANY(:ids) AND n.news_letter_embedding IS NOT NULL "
            "  AND " + _DISPLAYABLE,
            ids=list(news_letter_ids),
        )
        return {r[0]: Item(r[0], vector_from_send(r[1]), r[2], r[3], r[4]) for r in rows}

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


# 접속 자체가 멈춘 경우(네트워크 단절 등) 작업 스레드가 pool_timeout보다 오래 붙잡히지 않게 한다.
# libpq는 2초 미만 값을 2초로 올려 쓴다.
CONNECT_TIMEOUT_S = 2


def create_recsys_engine(database_url: str, workers: int, time_budget_ms: int) -> Engine:
    """실시간 경로 전용 커넥션 풀(벌크헤드). API 요청은 인증 조회 때부터 앱 풀 커넥션을 쥔 채
    추천 결과를 기다리므로, 작업 스레드가 같은 풀에서 빌리면 동시 요청이 풀 크기에 닿을 때
    순환 대기가 생긴다. 작업 스레드 수만큼 따로 두면 작업 스레드는 풀을 기다리지 않고,
    앱 풀 사용량은 요청당 1개로 이전과 같다.

    pool_pre_ping: DB가 재시작되면 풀에 남은 커넥션이 전부 죽어 있다. 미리 확인하지 않으면
    죽은 커넥션 하나마다 요청 하나가 오류 폴백으로 끝난다(체크아웃마다 왕복 1번을 더 낸다)."""
    from app.database import register_pgvector_on_connect

    eng = create_engine(
        database_url,
        connect_args={"options": "-c client_encoding=utf8", "connect_timeout": CONNECT_TIMEOUT_S},
        pool_size=workers,
        max_overflow=0,
        pool_timeout=max(0.001, time_budget_ms / 1000.0),
        pool_pre_ping=True,
    )
    register_pgvector_on_connect(eng)
    return eng


def create_aux_engine(database_url: str) -> Engine:
    """요청 밖에서 도는 작은 작업(모델 레지스트리 확인, 노출 로그 쓰기) 전용 풀.

    이 둘이 앱 풀을 쓰면 요청 하나가 앱 풀 커넥션을 2개까지 필요로 하게 된다: 레지스트리
    확인은 요청 세션이 살아 있는 동안 돌고, 노출 로그는 응답 뒤 BackgroundTasks에서 쓰는데
    그때 요청 세션이 이미 반납됐는지는 FastAPI 버전의 의존성 정리 순서에 달려 있다. 따로 두면
    앱 풀 사용량은 FastAPI 버전과 무관하게 요청당 1개다. 쓰기는 건당 1ms 안팎이라 2개면
    충분하고, 못 빌리면 2초 뒤 포기한다(노출 로그는 impressions.failed, 레지스트리는
    scorer.reload_error로 센다)."""
    return create_engine(
        database_url,
        connect_args={
            "options": "-c client_encoding=utf8 -c statement_timeout=5000",
            "connect_timeout": CONNECT_TIMEOUT_S,
        },
        pool_size=1,
        max_overflow=1,
        pool_timeout=2,
        pool_pre_ping=True,
    )


def create_feature_engine(database_url: str, workers: int, budget_ms: int) -> Engine:
    """요청 경로 밖의 shadow·피처 작업 전용 풀(ADR 0033). 그 작업이 읽는 것은 후보의 인기도 창 집계다.

    실시간 풀이나 보조 풀을 같이 쓰지 않는 이유: 이 조회의 비용은 노출 로그의 크기를 따라 커진다. 느려졌을 때
    붙잡히는 것이 요청을 처리하는 커넥션이나 로그를 쓰는 커넥션이어서는 안 된다. 전용 스레드 수만큼만 두고,
    못 빌리면 작업의 시간 예산만큼 기다리다 포기한다(features.error로 센다)."""
    return create_engine(
        database_url,
        connect_args={"options": "-c client_encoding=utf8", "connect_timeout": CONNECT_TIMEOUT_S},
        pool_size=max(1, workers),
        max_overflow=0,
        pool_timeout=max(0.001, budget_ms / 1000.0),
        pool_pre_ping=True,
    )


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
    """칸 로그(와 요청 로그 한 행)를 한 트랜잭션으로 쓴다 - 둘 중 하나만 남는 일이 없다(ADR 0025).

    request_row 없이 칸만 넘기는 호출도 받는다(로그 v2 이전 형태). 빈 응답은 칸 없이 요청 행만 쓴다."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def __call__(self, rows: List[dict], request_row: Optional[dict] = None) -> None:
        payload = [dict(r, request_id=uuid.UUID(str(r["request_id"]))) for r in rows]
        with self.engine.begin() as conn:
            if request_row is not None:
                conn.execute(
                    insert(RecommendationRequestLog.__table__),
                    [dict(request_row, request_id=uuid.UUID(str(request_row["request_id"])))],
                )
            if payload:
                conn.execute(insert(RecommendationImpressionLog.__table__), payload)
