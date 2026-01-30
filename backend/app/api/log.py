from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, SQLModel
from app.database import get_session
from app.api.user_check import get_current_user
from app.models.user import User
from app.models.log import UserNewsLetterCTRLog

router = APIRouter(prefix="/logs", tags=["logs"])

class LogRequest(SQLModel):
    news_letter_id: int

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
        news_letter_id=log_req.news_letter_id
    )
    
    session.add(new_log)
    session.commit()
    session.refresh(new_log)
    
    return LogResponse(status="success", log_id=new_log.log_id)
