"""add_cluster_history_and_news_columns

Revision ID: f8f9573fd18a
Revises: ea199640471b
Create Date: 2026-01-27 17:55:57.073778

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8f9573fd18a'
down_revision: Union[str, Sequence[str], None] = 'ea199640471b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



from sqlalchemy.dialects.postgresql import TSVECTOR, JSONB
from sqlmodel import SQLModel # might not be needed if using sa directly but consistent

def upgrade() -> None:
    # 1. Add search_vector to news_raw
    op.add_column('news_raw', sa.Column('search_vector', TSVECTOR(), nullable=True))

    # 2. Add columns to news_letter
    op.add_column('news_letter', sa.Column('run_id', sa.Integer(), nullable=True))
    op.add_column('news_letter', sa.Column('generation_history', JSONB(), nullable=True))

    # 3. Create cluster_history table
    # Check if table exists is tricky here, but we assume it doesn't in a fresh env
    op.create_table('cluster_history',
        sa.Column('history_id', sa.Integer(), nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=True),
        sa.Column('cluster_log', JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('history_id')
    )


def downgrade() -> None:
    op.drop_table('cluster_history')
    op.drop_column('news_letter', 'generation_history')
    op.drop_column('news_letter', 'run_id')
    op.drop_column('news_raw', 'search_vector')
