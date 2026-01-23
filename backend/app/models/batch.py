from typing import Optional, List
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel, JSON, Column

# 1. 분야별 뉴스레터 모음 (랭킹 결과 저장)
class NewsLettersCategory(SQLModel, table=True):
    __tablename__ = "news_letters_category"
    
    news_letter_batch_id: Optional[int] = Field(default=None, primary_key=True)
    news_letter_ids: List[int] = Field(default=[], sa_column=Column(JSON))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

# 2. 오늘의 뉴스레터 배치 (개인화 추천 결과 저장)
class NewsLetterTodayBatch(SQLModel, table=True):
    __tablename__ = "news_letter_today_batch"
    
    news_letter_batch_id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.user_id")
    news_letter_ids: List[int] = Field(default=[], sa_column=Column(JSON))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )