from typing import Optional
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel

# 8. 사용자-뉴스레터 로그 (User_NewsLetter_CTR_log)
class UserNewsLetterCTRLog(SQLModel, table=True):
    __tablename__ = "User_NewsLetter_CTR_log"
    
    log_id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.user_id")
    news_letter_id: int = Field(foreign_key="news_letter.news_letter_id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
