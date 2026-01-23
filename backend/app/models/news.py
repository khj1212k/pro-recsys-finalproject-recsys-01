# backend/app/models/news.py
from typing import Optional, List
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel, JSON, Column
from pgvector.sqlalchemy import Vector

# 1. 카테고리 (Category)
class Category(SQLModel, table=True):
    __tablename__ = "category"
    
    category_id: Optional[int] = Field(default=None, primary_key=True)
    category_name: str
    # 100:Politics, 200:Economy... (내부 관리용 코드)
    category_code: int = Field(sa_column_kwargs={"comment": "100:Politics, 200:Economy..."})

# 2. 언론사 (Press)
class Press(SQLModel, table=True):
    __tablename__ = "press"
    
    press_id: Optional[int] = Field(default=None, primary_key=True)
    press_name: str

# 3. RSS URI (RSS 링크 정보)
class RSS_URI(SQLModel, table=True):
    __tablename__ = "rss_uri"
    
    rss_raw_id: Optional[int] = Field(default=None, primary_key=True)
    press_id: int = Field(foreign_key="press.press_id")
    uri: str

# 4. 원문 기사 (News_Raw)
class NewsRaw(SQLModel, table=True):
    __tablename__ = "news_raw"
    
    raw_news_id: Optional[int] = Field(default=None, primary_key=True)
    press_id: int = Field(foreign_key="press.press_id")
    raw_news_title: str
    raw_news_content: str
    raw_news_url: str
    # [Vector] 원문 기사 임베딩 (768차원 예시)
    embedding_result: List[float] = Field(default=None, sa_column=Column(Vector(768)))
    raw_news_created_at: str
    raw_news_crawled_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # 뉴스레터 ID (FK, Nullable) - 사용여부(used_check) 대신 사용
    # 연결된 뉴스레터가 없으면 NULL(None)
    news_letter_id: Optional[int] = Field(default=None, foreign_key="news_letter.news_letter_id")

# 5. 뉴스 레터 (News_Letter)
class NewsLetter(SQLModel, table=True):
    __tablename__ = "news_letter"
    
    news_letter_id: Optional[int] = Field(default=None, primary_key=True)
    news_letter_title: str
    news_letter_sentence: str # 후킹 문장
    news_letter_content: str
    # [Time] 생성 시각
    news_letter_created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # [Vector] 뉴스레터 임베딩
    news_letter_embedding: List[float] = Field(default=None, sa_column=Column(Vector(768)))
    # [JSON] 뉴스레터 키워드
    news_letter_keywords: List[str] = Field(default=[], sa_column=Column(JSON))
    raw_news_count: int = Field(default=1)

# 6. 뉴스레터-카테고리 매핑 (News_Letter_Categories)
class NewsLetterCategories(SQLModel, table=True):
    __tablename__ = "news_letter_categories"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    news_letter_id: int = Field(foreign_key="news_letter.news_letter_id")
    category_id: int = Field(foreign_key="category.category_id")