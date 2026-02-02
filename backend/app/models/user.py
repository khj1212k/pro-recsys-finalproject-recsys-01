from typing import Optional, List
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel, Relationship, Column
from pgvector.sqlalchemy import Vector

# 1. 사용자 (User)
class User(SQLModel, table=True):
    __tablename__ = "user"

    user_id: Optional[int] = Field(default=None, primary_key=True)
    user_email: str = Field(unique=True, index=True)
    user_password_hash: str 
    user_nickname: str
    user_created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    user_embedding: List[float] = Field(default=None, sa_column=Column(Vector(1024)))
    user_gender_code: Optional[int] = Field(default=None, sa_column_kwargs={"comment": "0:unknown, 1:male, 2:female"})
    user_birth_year: Optional[int] = None

# 2. 사용자-카테고리 매핑 (User_Preferred_Categories)
class UserPreferredCategories(SQLModel, table=True):
    __tablename__ = "user_preferred_categories"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    category_id: int = Field(foreign_key="category.category_id")
    user_id: int = Field(foreign_key="user.user_id")

# 3. 사용자 선호 뉴스레터 (User_Preferred_Newsletter)
class UserPreferredNewsletter(SQLModel, table=True):
    __tablename__ = "user_preferred_newsletter"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    news_letter_id: int = Field(foreign_key="news_letter.news_letter_id")
    user_id: int = Field(foreign_key="user.user_id")