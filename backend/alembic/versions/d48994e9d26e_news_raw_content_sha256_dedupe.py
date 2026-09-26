"""news_raw content sha256 dedupe + extraction statuses 'duplicate'/'error'

(a) 같은 기사가 URL만 달리해 두 번 들어온다: 동아일보 total.xml은 처음에 /news/list/... 로
    내보낸 기사를 분류 뒤 /news/It/... 처럼 섹션 경로로 다시 내보낸다(raw_news_url UNIQUE로는
    못 막는다). 2026-09-26 수집분 579건 중 2쌍이 본문·임베딩까지 동일했다. 정제된 본문의
    sha256을 저장하고, 상태가 'ok'인 행 사이에서 유일하게 한다(부분 unique 인덱스 - 병렬
    추출 워커가 같은 본문을 동시에 저장하는 경합도 DB가 막는다). 뒤에 들어온 사본은
    'duplicate'로 두고 본문/임베딩을 비워 클러스터·LLM 평가셋에 두 번 들어가지 않게 한다.
    본문 보존 기한(30일) 뒤 본문을 지우더라도 해시는 남겨 중복 판정을 이어 간다.
(b) 'error': 추출 중 예외(파서 오류 등)도 시도 횟수를 올리고 fetch_failed처럼 상한까지만
    다시 시도한다. 이전에는 상태를 남기지 않아 같은 URL을 매시 영원히 다시 받았다.

Revision ID: d48994e9d26e
Revises: f87f7378672e
Create Date: 2026-09-26 09:40:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd48994e9d26e'
down_revision: Union[str, Sequence[str], None] = 'f87f7378672e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_EXTRACT_STATUSES = ("ok", "dropped", "empty", "fetch_failed")
EXTRACT_STATUSES = ("ok", "dropped", "empty", "fetch_failed", "error", "duplicate")


def _status_check(statuses) -> str:
    return (
        "raw_news_extract_status IS NULL OR raw_news_extract_status IN ("
        + ", ".join(f"'{s}'" for s in statuses) + ")"
    )


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("ck_news_raw_extract_status", "news_raw", type_="check")
    op.create_check_constraint("ck_news_raw_extract_status", "news_raw", _status_check(EXTRACT_STATUSES))

    op.add_column("news_raw", sa.Column("raw_news_content_sha256", sa.String(length=64), nullable=True))
    op.execute(
        """
        UPDATE news_raw
        SET raw_news_content_sha256 = encode(sha256(convert_to(raw_news_content, 'UTF8')), 'hex')
        WHERE raw_news_extract_status = 'ok' AND raw_news_content <> ''
        """
    )
    # 먼저 들어온(작은 id) 행을 원본으로 남기고 나머지를 duplicate로 바꾼다.
    op.execute(
        """
        UPDATE news_raw d
        SET raw_news_extract_status = 'duplicate',
            raw_news_content = '',
            embedding_result = NULL
        FROM news_raw k
        WHERE d.raw_news_extract_status = 'ok'
          AND k.raw_news_extract_status = 'ok'
          AND d.raw_news_content_sha256 = k.raw_news_content_sha256
          AND k.raw_news_id < d.raw_news_id
        """
    )
    op.create_index(
        "uq_news_raw_content_sha256_ok",
        "news_raw",
        ["raw_news_content_sha256"],
        unique=True,
        postgresql_where=sa.text("raw_news_extract_status = 'ok'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_news_raw_content_sha256_ok", table_name="news_raw")
    # 사본에 원본 본문을 되돌려 'ok'로(임베딩은 NULL이라 다음 embed가 다시 채운다).
    op.execute(
        """
        UPDATE news_raw d
        SET raw_news_extract_status = 'ok',
            raw_news_content = k.raw_news_content
        FROM news_raw k
        WHERE d.raw_news_extract_status = 'duplicate'
          AND k.raw_news_extract_status = 'ok'
          AND k.raw_news_content_sha256 = d.raw_news_content_sha256
        """
    )
    # 이전 스키마에 없는 상태: 원본을 못 찾은 사본은 dropped, error는 미처리(NULL)로 되돌려 다시 시도하게 한다.
    op.execute("UPDATE news_raw SET raw_news_extract_status = 'dropped' WHERE raw_news_extract_status = 'duplicate'")
    op.execute("UPDATE news_raw SET raw_news_extract_status = NULL WHERE raw_news_extract_status = 'error'")
    op.drop_column("news_raw", "raw_news_content_sha256")
    op.drop_constraint("ck_news_raw_extract_status", "news_raw", type_="check")
    op.create_check_constraint("ck_news_raw_extract_status", "news_raw", _status_check(OLD_EXTRACT_STATUSES))
