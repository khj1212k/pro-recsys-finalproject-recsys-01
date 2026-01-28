"""update_vector_dims_to_1024

Revision ID: d3e03fb4d2af
Revises: f8f9573fd18a
Create Date: 2026-01-28 16:58:58.145649

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3e03fb4d2af'
down_revision: Union[str, Sequence[str], None] = 'f8f9573fd18a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Nullify existing data
    op.execute("UPDATE news_raw SET embedding_result = NULL")
    op.execute("UPDATE news_letter SET news_letter_embedding = NULL")
    op.execute("UPDATE \"user\" SET user_embedding = NULL")

    # 2. Alter column types to vector(1024)
    op.execute("ALTER TABLE news_raw ALTER COLUMN embedding_result TYPE vector(1024)")
    op.execute("ALTER TABLE news_letter ALTER COLUMN news_letter_embedding TYPE vector(1024)")
    op.execute("ALTER TABLE \"user\" ALTER COLUMN user_embedding TYPE vector(1024)")


def downgrade() -> None:
    # 1. Nullify existing data
    op.execute("UPDATE news_raw SET embedding_result = NULL")
    op.execute("UPDATE news_letter SET news_letter_embedding = NULL")
    op.execute("UPDATE \"user\" SET user_embedding = NULL")

    # 2. Revert column types to vector(768)
    op.execute("ALTER TABLE news_raw ALTER COLUMN embedding_result TYPE vector(768)")
    op.execute("ALTER TABLE news_letter ALTER COLUMN news_letter_embedding TYPE vector(768)")
    op.execute("ALTER TABLE \"user\" ALTER COLUMN user_embedding TYPE vector(768)")
