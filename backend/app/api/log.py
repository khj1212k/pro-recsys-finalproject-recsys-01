import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from sqlmodel import Field, Session, SQLModel
from app.database import get_session
from app.api.user_check import get_current_user
from app.models.user import User
from app.models.log import UserNewsLetterCTRLog

router = APIRouter(prefix="/logs", tags=["logs"])


class LogRequest(SQLModel):
    """news_letter_id만 보내도 된다(기존 프런트 계약). 나머지는 로그 v2(ADR 0025)의 선택 값이다.

    - request_id, position: 클릭한 목록이 GET /newsletters/today의 응답이면 그 응답의 X-Request-Id 헤더와
      목록 안의 0부터 시작하는 순위. 클릭을 노출 로그의 칸과 정확히 잇는다.
    - event: 'click'(기본) | 'detail_view'(상세 화면 체류 보고).
    - dwell_ms: 상세 화면에 머문 시간(밀리초).
    """

    news_letter_id: int
    request_id: Optional[uuid.UUID] = None
    position: Optional[int] = Field(default=None, ge=0, le=32767)
    event: Literal["click", "detail_view"] = "click"
    dwell_ms: Optional[int] = Field(default=None, ge=0, le=2_147_483_647)


class LogResponse(SQLModel):
    status: str
    log_id: int


@router.post("/newsletter/click", response_model=LogResponse)
def create_newsletter_click_log(
    log_req: LogRequest,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session)
):

    new_log = UserNewsLetterCTRLog(
        user_id=user.user_id,
        news_letter_id=log_req.news_letter_id,
        request_id=log_req.request_id,
        position=log_req.position,
        event=log_req.event,
        dwell_ms=log_req.dwell_ms,
    )

    session.add(new_log)
    session.commit()
    session.refresh(new_log)

    return LogResponse(status="success", log_id=new_log.log_id)
