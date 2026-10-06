import uuid
from typing import Optional
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Index, Integer, SmallInteger, String, Uuid, text
from sqlmodel import Column, Field, SQLModel

CLICK_EVENTS = ("click", "detail_view")


# 8. 사용자-뉴스레터 로그 (User_NewsLetter_CTR_log)
#
# 로그 v2(ADR 0025)에서 추가한 컬럼. 넷 다 보내지 않아도 되는 값이라 기존 클라이언트
# ({news_letter_id}만 보냄)의 행은 request_id/position/dwell_ms가 NULL, event가 'click'이다.
# - request_id, position: 이 클릭이 나온 추천 응답(X-Request-Id)과 그 목록 안의 0부터 시작하는 순위.
#   recommendation_impression_log의 (request_id, news_letter_id)와 1:1로 조인된다.
#   추천 목록 밖(카테고리 화면 등)의 클릭은 NULL로 남는다.
# - event: 'click'(목록에서 누름) | 'detail_view'(상세 화면 체류 보고). 추천 경로가 "클릭"으로
#   읽는 것은 'click' 행뿐이다.
# - dwell_ms: 상세 화면에 머문 시간(밀리초). 보내는 쪽이 재서 준다.
class UserNewsLetterCTRLog(SQLModel, table=True):
    __tablename__ = "user_newsletter_ctr_log"
    __table_args__ = (
        Index(
            "ix_user_newsletter_ctr_log_request_id",
            "request_id",
            postgresql_where=text("request_id IS NOT NULL"),
        ),
        CheckConstraint("event IN ('click', 'detail_view')", name="ck_user_newsletter_ctr_log_event"),
        # 아이템별 최근 클릭 수(인기도 창 집계, ADR 0033). 그 집계가 읽는 것은 'click' 행의 (뉴스레터, 시각)뿐이라
        # 그 행만 담는 부분 인덱스로 둔다: 인덱스만 읽고 끝낼 수 있다(체류 보고 행을 걸러 내려고 테이블을 읽지 않는다).
        Index(
            "ix_user_newsletter_ctr_log_news_letter_id_created_at",
            "news_letter_id",
            "created_at",
            postgresql_where=text("event = 'click'"),
        ),
    )

    log_id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.user_id")
    news_letter_id: int = Field(foreign_key="news_letter.news_letter_id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    request_id: Optional[uuid.UUID] = Field(default=None, sa_column=Column(Uuid, nullable=True))
    position: Optional[int] = Field(default=None, sa_column=Column(SmallInteger, nullable=True))
    event: str = Field(
        default="click",
        sa_column=Column(String(16), nullable=False, server_default="click"),
    )
    dwell_ms: Optional[int] = Field(default=None, sa_column=Column(Integer, nullable=True))
