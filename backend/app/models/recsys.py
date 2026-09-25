import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
    func,
    text,
)
from sqlmodel import Column, Field, SQLModel


# 요청 시점 추천이 실제로 화면에 내보낸 목록. 클릭 로그(user_newsletter_ctr_log)와
# (user_id, news_letter_id, 시각)으로 조인해 노출 대비 클릭을 계산하려고 남긴다.
# 쓰기 빈도가 높은 append-only 로그라 FK를 걸지 않는다 - 뉴스레터/유저 삭제가
# 로그 때문에 막히거나, INSERT마다 FK 검사 비용을 치르지 않도록.
class RecommendationImpressionLog(SQLModel, table=True):
    __tablename__ = "recommendation_impression_log"
    __table_args__ = (
        Index(
            "ix_recommendation_impression_log_user_id_created_at",
            "user_id",
            text("created_at DESC"),
        ),
    )

    impression_id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    request_id: uuid.UUID = Field(sa_column=Column(Uuid, nullable=False))
    user_id: int = Field(nullable=False)
    news_letter_id: int = Field(nullable=False)
    position: int = Field(sa_column=Column(SmallInteger, nullable=False))
    score: Optional[float] = Field(default=None, sa_column=Column(Float, nullable=True))
    source: str = Field(sa_column=Column(String(32), nullable=False))
    model_version: str = Field(sa_column=Column(String(64), nullable=False))
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


# 요청 시점 랭커(LightGBMScorer)가 읽는 모델 저장소. API 프로세스가 여러 개여도
# 공유 파일시스템 없이 같은 모델을 읽을 수 있도록 LightGBM text 모델 본문을
# 그대로 저장한다. 이름별로 활성 모델은 최대 1개(부분 UNIQUE 인덱스).
class ModelRegistry(SQLModel, table=True):
    __tablename__ = "model_registry"
    __table_args__ = (
        UniqueConstraint("model_name", "model_version", name="uq_model_registry_name_version"),
        Index(
            "uq_model_registry_one_active_per_name",
            "model_name",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    model_id: Optional[int] = Field(default=None, primary_key=True)
    model_name: str = Field(sa_column=Column(String(64), nullable=False))
    model_version: str = Field(sa_column=Column(String(64), nullable=False))
    model_format: str = Field(
        default="lightgbm_text",
        sa_column=Column(String(32), nullable=False, server_default="lightgbm_text"),
    )
    model_text: str = Field(sa_column=Column(Text, nullable=False))
    feature_names: Optional[list] = Field(default=None, sa_column=Column(JSON))
    metrics: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    is_active: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=false()),
    )
    created_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
