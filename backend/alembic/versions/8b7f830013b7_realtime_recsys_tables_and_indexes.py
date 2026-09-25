"""realtime recsys: impression log, model registry, request-path indexes

GET /newsletters/today를 요청 시점에 계산하면서(ADR 0015) 필요한 스키마.
- recommendation_impression_log: 실제로 내보낸 추천 목록(요청 ID/순위/점수/
  출처/모델 버전). FK 없이 append-only.
- model_registry: LightGBMScorer가 60초마다 확인하는 LightGBM text 모델 저장소.
  이름별 활성 모델은 부분 UNIQUE 인덱스로 최대 1개.
- 요청 경로가 매번 치는 조회용 인덱스:
  news_letter(news_letter_created_at DESC) - 신선도 창/최근 N개/인기 후보,
  news_letter_today_batch(user_id, created_at DESC) - 배치 폴백,
  news_letter_categories(news_letter_id) - 표시 가능(카테고리 있음) 필터,
  user_preferred_newsletter(user_id), user_preferred_categories(user_id) - 콜드스타트.

Revision ID: 8b7f830013b7
Revises: e725a62ffef1
Create Date: 2026-09-25 23:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8b7f830013b7'
down_revision: Union[str, Sequence[str], None] = 'e725a62ffef1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "recommendation_impression_log",
        sa.Column("impression_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("news_letter_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("impression_id"),
    )
    op.create_index(
        "ix_recommendation_impression_log_user_id_created_at",
        "recommendation_impression_log",
        ["user_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "model_registry",
        sa.Column("model_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=False),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column(
            "model_format",
            sa.String(length=32),
            server_default="lightgbm_text",
            nullable=False,
        ),
        sa.Column("model_text", sa.Text(), nullable=False),
        sa.Column("feature_names", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("model_id"),
        sa.UniqueConstraint("model_name", "model_version", name="uq_model_registry_name_version"),
    )
    op.create_index(
        "uq_model_registry_one_active_per_name",
        "model_registry",
        ["model_name"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_index(
        "ix_news_letter_created_at",
        "news_letter",
        [sa.text("news_letter_created_at DESC")],
    )
    op.create_index(
        "ix_news_letter_today_batch_user_id_created_at",
        "news_letter_today_batch",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_news_letter_categories_news_letter_id",
        "news_letter_categories",
        ["news_letter_id"],
    )
    op.create_index(
        "ix_user_preferred_newsletter_user_id",
        "user_preferred_newsletter",
        ["user_id"],
    )
    op.create_index(
        "ix_user_preferred_categories_user_id",
        "user_preferred_categories",
        ["user_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_user_preferred_categories_user_id", table_name="user_preferred_categories")
    op.drop_index("ix_user_preferred_newsletter_user_id", table_name="user_preferred_newsletter")
    op.drop_index("ix_news_letter_categories_news_letter_id", table_name="news_letter_categories")
    op.drop_index(
        "ix_news_letter_today_batch_user_id_created_at", table_name="news_letter_today_batch"
    )
    op.drop_index("ix_news_letter_created_at", table_name="news_letter")

    op.drop_index("uq_model_registry_one_active_per_name", table_name="model_registry")
    op.drop_table("model_registry")

    op.drop_index(
        "ix_recommendation_impression_log_user_id_created_at",
        table_name="recommendation_impression_log",
    )
    op.drop_table("recommendation_impression_log")
