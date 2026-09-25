from typing import Optional, List
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel, JSON, Column
from sqlalchemy import DateTime, SmallInteger, text
from pgvector.sqlalchemy import Vector

from sqlalchemy.dialects.postgresql import TSVECTOR

# 1. 카테고리 (Category)
class Category(SQLModel, table=True):
    __tablename__ = "category"
    
    category_id: Optional[int] = Field(default=None, primary_key=True)
    category_name: str
    category_code: int = Field(sa_column_kwargs={"comment": "100:Politics, 200:Economy..."})

# 2. 언론사 (Press)
class Press(SQLModel, table=True):
    __tablename__ = "press"
    
    press_id: Optional[int] = Field(default=None, primary_key=True)
    press_name: str

# 3. RSS URI (RSS 정보)
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
    embedding_result: List[float] = Field(default=None, sa_column=Column(Vector(1024)))
    # 2026-09 마이그레이션(e725a62ffef1) 이전에는 VARCHAR였다. RSS 수집기가 쓰는
    # 형식을 파싱하지 못한 기존 값은 NULL로 대체됐으므로 Optional이다.
    raw_news_created_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    raw_news_crawled_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    news_letter_id: Optional[int] = Field(default=None, foreign_key="news_letter.news_letter_id")
    search_vector: Optional[str] = Field(default=None, sa_column=Column(TSVECTOR))
    # 본문 추출 결과(f87f7378672e). NULL = 아직 시도 안 함. 'dropped'/'empty'도
    # raw_news_content는 ''이므로 이 컬럼 없이는 "미처리"와 구분할 수 없다.
    raw_news_extract_status: Optional[str] = Field(default=None, max_length=16)
    raw_news_extracted_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    raw_news_extract_attempts: int = Field(
        default=0, sa_column=Column(SmallInteger, nullable=False, server_default=text("0"))
    )

# 5. 뉴스 레터 (News_Letter)
class NewsLetter(SQLModel, table=True):
    __tablename__ = "news_letter"
    
    news_letter_id: Optional[int] = Field(default=None, primary_key=True)
    news_letter_title: str
    news_letter_sentence: str
    news_letter_content: str
    news_letter_created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    news_letter_embedding: List[float] = Field(default=None, sa_column=Column(Vector(1024)))
    news_letter_keywords: List[str] = Field(default=[], sa_column=Column(JSON))
    raw_news_count: int = Field(default=1)
    run_id: Optional[int] = Field(default=None)
    generation_history: Optional[dict] = Field(default={}, sa_column=Column(JSON))

# 6. 뉴스레터-카테고리 매핑 (News_Letter_Categories)
class NewsLetterCategories(SQLModel, table=True):
    __tablename__ = "news_letter_categories"
    
    id: Optional[int] = Field(default=None, primary_key=True)
    news_letter_id: int = Field(foreign_key="news_letter.news_letter_id")
    category_id: int = Field(foreign_key="category.category_id")

# 7. 클러스터링 히스토리 (Cluster_History)
class ClusterHistory(SQLModel, table=True):
    __tablename__ = "cluster_history"
    
    history_id: Optional[int] = Field(default=None, primary_key=True)
    run_id: Optional[int] = Field(default=None)
    cluster_log: Optional[dict] = Field(default={}, sa_column=Column(JSON))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )