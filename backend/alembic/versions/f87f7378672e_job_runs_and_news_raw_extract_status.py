"""job_runs table + news_raw extraction status

(a) job_runs: `python -m jobs.run <job>`(jobs/ 패키지) 실행 1회당 1행. 스케줄러가
    무엇을 언제 돌렸고 무엇을 남겼는지(stats JSONB), 어떤 코드(git_sha)로
    돌렸는지를 DB 안에 남겨 수집 데이터와 같은 곳에서 조회할 수 있게 한다.
(b) news_raw 본문 추출 상태: 이전에는 "아직 추출 안 함"과 "추출했지만 저품질이라
    버림/본문 없음"이 둘 다 raw_news_content = '' 로 같아서, 본문 추출기가 매
    실행마다 과거에 버린 기사까지 다시 내려받았다(2시간 주기 수집에서 재요청
    수가 누적해서 커진다). 상태/시각/시도 횟수를 따로 기록한다.

Revision ID: f87f7378672e
Revises: e725a62ffef1
Create Date: 2026-09-25 23:49:55

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f87f7378672e'
down_revision: Union[str, Sequence[str], None] = 'e725a62ffef1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JOB_RUN_STATUSES = ("running", "succeeded", "failed", "skipped", "abandoned")
EXTRACT_STATUSES = ("ok", "dropped", "empty", "fetch_failed")


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("job", sa.String(length=64), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("git_sha", sa.String(length=40), nullable=True),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in JOB_RUN_STATUSES) + ")",
            name="ck_job_runs_status",
        ),
    )
    op.create_index(
        "ix_job_runs_job_started_at", "job_runs", ["job", sa.text("started_at DESC")]
    )

    op.add_column(
        "news_raw", sa.Column("raw_news_extract_status", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "news_raw",
        sa.Column("raw_news_extracted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "news_raw",
        sa.Column(
            "raw_news_extract_attempts", sa.SmallInteger(), nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_news_raw_extract_status",
        "news_raw",
        "raw_news_extract_status IS NULL OR raw_news_extract_status IN ("
        + ", ".join(f"'{s}'" for s in EXTRACT_STATUSES) + ")",
    )
    # 본문이 이미 채워진 기존 행만 'ok'로 확정한다. 빈 본문('')인 기존 행은 "아직 안 함"과
    # "버림"을 구분할 수 없으므로 NULL로 남겨 새 추출기가 한 번 더 시도하게 한다.
    op.execute(
        """
        UPDATE news_raw
        SET raw_news_extract_status = 'ok',
            raw_news_extracted_at = raw_news_crawled_at,
            raw_news_extract_attempts = 1
        WHERE raw_news_content IS NOT NULL AND raw_news_content <> ''
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ck_news_raw_extract_status", "news_raw", type_="check")
    op.drop_column("news_raw", "raw_news_extract_attempts")
    op.drop_column("news_raw", "raw_news_extracted_at")
    op.drop_column("news_raw", "raw_news_extract_status")

    op.drop_index("ix_job_runs_job_started_at", table_name="job_runs")
    op.drop_table("job_runs")
