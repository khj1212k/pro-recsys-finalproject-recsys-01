"""recsys logging v2: request log, slot log columns, click linkage, model role

첫 사용자부터 추천 정책을 비교할 수 있게 로그를 바꾼다(ADR 0025).
- user_newsletter_ctr_log: request_id / position / event / dwell_ms. 클릭을 그 클릭이 나온 추천 응답의
  칸과 정확히 잇는다. 넷 다 선택 값이라 기존 클라이언트({news_letter_id}만 보냄)는 그대로 동작한다.
- recommendation_request_log(신규): 응답 하나가 한 행. 후보 집합, 탐색 위치, 프로필 출처, 캐시 적중,
  폴백 사유, 정책·모델 버전.
- recommendation_impression_log: det_rank / scores_shadow / features 추가, (request_id, position) 유일.
  propensity와 explored는 8b7f830013b7이 자리만 잡아 둔 컬럼이고 이제 값이 들어간다.
- model_registry.role: active | shadow | retired. is_active는 role = 'active'와 같아야 한다.
  이 리비전 이전의 비활성 행은 어디서도 읽지 않던 모델이므로 'retired'로 옮긴다(조용히 shadow로
  서빙되기 시작하지 않게). 새 행의 기본값은 'shadow'다.

Revision ID: c4d2a91e7f30
Revises: 8b7f830013b7
Create Date: 2026-10-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c4d2a91e7f30'
down_revision: Union[str, Sequence[str], None] = '8b7f830013b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1) 클릭 로그: 노출 연결키와 이벤트 종류. 상수 기본값 컬럼 추가는 PostgreSQL 11+에서 테이블을 다시 쓰지 않는다.
    op.add_column("user_newsletter_ctr_log", sa.Column("request_id", sa.Uuid(), nullable=True))
    op.add_column("user_newsletter_ctr_log", sa.Column("position", sa.SmallInteger(), nullable=True))
    op.add_column(
        "user_newsletter_ctr_log",
        sa.Column("event", sa.String(length=16), server_default="click", nullable=False),
    )
    op.add_column("user_newsletter_ctr_log", sa.Column("dwell_ms", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_user_newsletter_ctr_log_event",
        "user_newsletter_ctr_log",
        "event IN ('click', 'detail_view')",
    )
    op.create_index(
        "ix_user_newsletter_ctr_log_request_id",
        "user_newsletter_ctr_log",
        ["request_id"],
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )

    # 2) 요청 로그
    op.create_table(
        "recommendation_request_log",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=160), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("profile_source", sa.String(length=16), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("fallback_reason", sa.String(length=16), nullable=True),
        sa.Column("candidate_count", sa.SmallInteger(), nullable=True),
        sa.Column("eligible_count", sa.SmallInteger(), nullable=True),
        sa.Column("explore_pool_size", sa.SmallInteger(), nullable=True),
        sa.Column("slate_size", sa.SmallInteger(), nullable=False),
        sa.Column("shown_count", sa.SmallInteger(), nullable=False),
        sa.Column("explore_positions", sa.ARRAY(sa.SmallInteger()), nullable=True),
        sa.Column("candidate_ids", sa.ARRAY(sa.Integer()), nullable=True),
        sa.Column("feature_schema_version", sa.SmallInteger(), nullable=True),
        sa.Column("shadow_versions", sa.ARRAY(sa.String(length=160)), nullable=True),
        sa.Column("fatigue_mode", sa.String(length=8), nullable=True),
        sa.Column("fatigued_count", sa.SmallInteger(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "ix_recommendation_request_log_user_id_created_at",
        "recommendation_request_log",
        ["user_id", sa.text("created_at DESC")],
    )

    # 3) 칸(slot) 로그
    op.add_column("recommendation_impression_log", sa.Column("det_rank", sa.SmallInteger(), nullable=True))
    op.add_column(
        "recommendation_impression_log",
        sa.Column("scores_shadow", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("recommendation_impression_log", sa.Column("features", sa.LargeBinary(), nullable=True))
    # 클릭과의 조인 키(request_id)에 인덱스를 주고, 한 응답의 한 위치에 행이 둘 생기지 않게 한다.
    op.create_unique_constraint(
        "uq_recommendation_impression_log_request_position",
        "recommendation_impression_log",
        ["request_id", "position"],
    )

    # 4) 모델 저장소의 역할
    op.add_column(
        "model_registry",
        sa.Column("role", sa.String(length=16), server_default="shadow", nullable=False),
    )
    op.execute("UPDATE model_registry SET role = CASE WHEN is_active THEN 'active' ELSE 'retired' END")
    op.create_check_constraint(
        "ck_model_registry_role", "model_registry", "role IN ('active', 'shadow', 'retired')"
    )
    op.create_check_constraint(
        "ck_model_registry_active_matches_role", "model_registry", "is_active = (role = 'active')"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_model_registry_active_matches_role", "model_registry", type_="check")
    op.drop_constraint("ck_model_registry_role", "model_registry", type_="check")
    op.drop_column("model_registry", "role")

    op.drop_constraint(
        "uq_recommendation_impression_log_request_position",
        "recommendation_impression_log",
        type_="unique",
    )
    op.drop_column("recommendation_impression_log", "features")
    op.drop_column("recommendation_impression_log", "scores_shadow")
    op.drop_column("recommendation_impression_log", "det_rank")

    op.drop_index(
        "ix_recommendation_request_log_user_id_created_at", table_name="recommendation_request_log"
    )
    op.drop_table("recommendation_request_log")

    op.drop_index("ix_user_newsletter_ctr_log_request_id", table_name="user_newsletter_ctr_log")
    op.drop_constraint("ck_user_newsletter_ctr_log_event", "user_newsletter_ctr_log", type_="check")
    op.drop_column("user_newsletter_ctr_log", "dwell_ms")
    op.drop_column("user_newsletter_ctr_log", "event")
    op.drop_column("user_newsletter_ctr_log", "position")
    op.drop_column("user_newsletter_ctr_log", "request_id")
