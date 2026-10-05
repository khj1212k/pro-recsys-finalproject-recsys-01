import logging
import uuid
from datetime import datetime
from typing import Callable, Literal, Optional

from fastapi import APIRouter, Depends
from sqlmodel import Field, Session, SQLModel
from app.database import get_session
from app.api.user_check import get_current_user
from app.models.user import User
from app.models.log import UserNewsLetterCTRLog
from app.recsys import profile_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/logs", tags=["logs"])

# (클릭 행을 쓴 트랜잭션의 커넥션, user_id, news_letter_id, 클릭 시각) -> 상태에 반영했는지
ProfileUpdater = Callable[[object, int, int, datetime], bool]


def get_profile_updater() -> ProfileUpdater:
    """클릭을 장기 프로필의 증분 상태에 반영하는 함수(ADR 0033). 테스트에서 바꿔 끼운다."""
    return profile_store.apply_click


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
    session: Session = Depends(get_session),
    update_profile: ProfileUpdater = Depends(get_profile_updater),
):
    user_id = user.user_id
    new_log = UserNewsLetterCTRLog(
        user_id=user_id,
        news_letter_id=log_req.news_letter_id,
        request_id=log_req.request_id,
        position=log_req.position,
        event=log_req.event,
        dwell_ms=log_req.dwell_ms,
    )

    session.add(new_log)
    session.flush()
    if log_req.event == "click":
        # 클릭 행과 같은 트랜잭션에서 장기 프로필 상태를 한 번 갱신한다(ADR 0033): 다음 요청이 이 클릭을
        # 반영한 프로필을 읽는다. 상태는 클릭 로그의 캐시라, 갱신이 실패해도 클릭은 잃지 않는다 - savepoint까지만
        # 되돌리고 클릭 행은 커밋한다. 어긋난 상태는 `jobs.run rebuild_user_state`가 로그에서 다시 만든다.
        try:
            with session.begin_nested():
                update_profile(session.connection(), user_id, log_req.news_letter_id, new_log.created_at)
        except Exception:
            logger.exception("profile state update failed for user %s; the click is still stored", user_id)
    session.commit()
    session.refresh(new_log)

    return LogResponse(status="success", log_id=new_log.log_id)
