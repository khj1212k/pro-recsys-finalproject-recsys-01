from typing import Optional, List
from datetime import datetime, timezone
from sqlmodel import Field, SQLModel, JSON, Column
from sqlalchemy import CheckConstraint, DateTime, Index, Integer, SmallInteger, String, text
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
# 본문 추출 상태 (f87f7378672e, d48994e9d26e, b7e4a1c93d52). NULL = 아직 시도 안 함.
# 'rss_summary' = 본문 칸이 RSS 요약인 행(동아일보), 'blocked_origin' = 출발지 거절로 본문 없이 닫은 행 - ADR 0026 2026-10-25.
NEWS_RAW_EXTRACT_STATUSES = (
    "ok", "dropped", "empty", "fetch_failed", "error", "duplicate", "rss_summary", "blocked_origin",
)


class NewsRaw(SQLModel, table=True):
    __tablename__ = "news_raw"
    __table_args__ = (
        CheckConstraint(
            "raw_news_extract_status IS NULL OR raw_news_extract_status IN ("
            + ", ".join(f"'{s}'" for s in NEWS_RAW_EXTRACT_STATUSES) + ")",
            name="ck_news_raw_extract_status",
        ),
        # URL만 다른 같은 기사: 'ok' 행 사이에서 정제 본문 해시가 유일해야 한다.
        Index(
            "uq_news_raw_content_sha256_ok", "raw_news_content_sha256", unique=True,
            postgresql_where=text("raw_news_extract_status = 'ok'"),
        ),
        # 요약 행('rss_summary', 동아일보)의 같은 요약 조회용(b7e4a1c93d52, ADR 0026 2026-10-25). 유일 인덱스가 아니다 -
        # 중복은 수집기가 막고, 이 인덱스는 'ok'만 덮는 위 인덱스로는 못 타는 조회를 받친다.
        Index(
            "ix_news_raw_content_sha256_rss_summary", "raw_news_content_sha256",
            postgresql_where=text("raw_news_extract_status = 'rss_summary'"),
        ),
        # 본문 보존(f1db3221e45f, ADR 0023): 비운 행에는 본문이 없고 길이가 남는다.
        CheckConstraint(
            "raw_news_content_purged_at IS NULL "
            "OR (raw_news_content IS NULL AND raw_news_content_length IS NOT NULL)",
            name="ck_news_raw_purged_body_gone",
        ),
        CheckConstraint(
            "raw_news_kogl_type IS NULL OR raw_news_kogl_type BETWEEN 1 AND 4",
            name="ck_news_raw_kogl_type",
        ),
    )

    raw_news_id: Optional[int] = Field(default=None, primary_key=True)
    press_id: int = Field(foreign_key="press.press_id")
    raw_news_title: str
    # 보존 기한(ADR 0023, 수집 후 30일)이 지난 상업 언론사 본문은 NULL이다(jobs/tasks/retention.py).
    # ''는 "추출했지만 본문 없음"(dropped/empty/duplicate/fetch_failed)이다.
    raw_news_content: Optional[str] = None
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
    # 정제된 본문의 sha256(hex). 본문 보존 기한이 지나 본문을 비워도 중복 판정용으로 남긴다.
    raw_news_content_sha256: Optional[str] = Field(default=None, sa_column=Column(String(64)))
    # 본문 글자 수(char_length). 본문을 쓸 때 함께 쓰고, 본문을 비운 뒤에도 남는다(f1db3221e45f).
    raw_news_content_length: Optional[int] = Field(default=None, sa_column=Column(Integer))
    # 본문을 비운 시각. NULL이면 비운 적 없음.
    raw_news_content_purged_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    # 정책브리핑 기사의 공공누리 유형(1~4). 상업 언론사 행은 NULL.
    raw_news_kogl_type: Optional[int] = Field(default=None, sa_column=Column(SmallInteger))

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