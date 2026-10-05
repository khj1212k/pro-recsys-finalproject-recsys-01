"""recsys serving parity: incremental profile state, feature as-of, schema hash, popularity indexes

오프라인에서 검증한 랭커가 요청 시점에 같은 피처를 보게 하는 데 필요한 스키마(ADR 0033).
- user_profile_state(신규): 장기 프로필의 증분 상태. 사용자당 한 행이고 클릭 API가 클릭마다 갱신한다.
  hist_sum은 float64 little-endian 바이트(감쇠 합), hist_anchor_ts는 그 기준 시각(= 마지막 클릭),
  hist_len은 반영된 클릭 수, hist_cat_counts는 대표 카테고리별 클릭 수다.
  **이 리비전은 테이블만 만든다. 기존 클릭 로그로 상태를 채우는 것은 잡이다:**
      python -m jobs.run rebuild_user_state
  채우기 전에는 클릭 이력이 있는 사용자도 장기 프로필이 없는 사용자로 읽힌다(온보딩·카테고리·인기 경로).
- recommendation_request_log.features_as_of: 칸 로그의 피처를 계산한 요청 시각. 로그에서 피처를 다시
  계산할 때의 기준이다.
- model_registry.feature_schema_hash: 모델을 등록할 때의 서빙 피처 스키마 지문.
- 인기도 창 집계용 인덱스 둘: 클릭 로그(event = 'click' 행만 담는 부분 인덱스)와 칸 로그의
  (news_letter_id, created_at).

Revision ID: a6f1e83b0d57
Revises: c4d2a91e7f30
Create Date: 2026-10-06 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a6f1e83b0d57'
down_revision: Union[str, Sequence[str], None] = 'c4d2a91e7f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "user_profile_state",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("hist_sum", sa.LargeBinary(), nullable=True),
        sa.Column("hist_anchor_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hist_len", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("hist_cat_counts", sa.JSON(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # 사용자를 지우면 상태도 같이 지워진다(클릭 로그에서 다시 만들 수 있는 캐시다).
        sa.ForeignKeyConstraint(["user_id"], ["user.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.add_column(
        "recommendation_request_log",
        sa.Column("features_as_of", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "model_registry",
        sa.Column("feature_schema_hash", sa.String(length=64), nullable=True),
    )

    # 집계가 읽는 것은 'click' 행의 (뉴스레터, 시각)뿐이다: 그 행만 담는 부분 인덱스(인덱스만 읽고 끝낼 수 있다).
    op.create_index(
        "ix_user_newsletter_ctr_log_news_letter_id_created_at",
        "user_newsletter_ctr_log",
        ["news_letter_id", "created_at"],
        postgresql_where=sa.text("event = 'click'"),
    )
    op.create_index(
        "ix_recommendation_impression_log_news_letter_id_created_at",
        "recommendation_impression_log",
        ["news_letter_id", "created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_recommendation_impression_log_news_letter_id_created_at",
        table_name="recommendation_impression_log",
    )
    op.drop_index(
        "ix_user_newsletter_ctr_log_news_letter_id_created_at",
        table_name="user_newsletter_ctr_log",
    )
    op.drop_column("model_registry", "feature_schema_hash")
    op.drop_column("recommendation_request_log", "features_as_of")
    op.drop_table("user_profile_state")
