"""news_raw url unique, created_at to timestamptz, ctr log index

news_raw.raw_news_url에 UNIQUE 제약을 추가하기 전에 기존 중복(raw_news_id가 더
큰 쪽)을 지운다. news_raw.raw_news_created_at은 VARCHAR로 저장돼 있었는데,
ai_workspace/crawler/rss_collector.py가 psycopg2의 datetime 어댑터가 만든
"YYYY-MM-DD HH:MM:SS[.ffffff]+HH:MM" 형식 문자열만 써왔으므로 대부분 그대로
timestamptz로 캐스팅 가능하지만, 이 컬럼을 채우는 경로가 하나뿐이라는 보장이
없어 안전하게 파싱 실패 시 NULL로 처리하는 함수를 통해 변환한다.
user_newsletter_ctr_log(user_id, created_at DESC)에는 "유저의 최근 클릭
로그" 조회(recommend_engine DataLoader.load_ctr_logs 등)를 위한 인덱스를 추가한다.

Revision ID: e725a62ffef1
Revises: c97fb5709513
Create Date: 2026-09-25 21:00:43.964643

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e725a62ffef1'
down_revision: Union[str, Sequence[str], None] = 'c97fb5709513'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # (a) news_raw.raw_news_url: 중복 제거 후 UNIQUE
    # 같은 URL이 여러 번 들어간 경우 raw_news_id가 가장 작은(가장 먼저 수집된)
    # 행만 남긴다.
    op.execute(
        """
        DELETE FROM news_raw a
        USING news_raw b
        WHERE a.raw_news_id > b.raw_news_id
          AND a.raw_news_url = b.raw_news_url
        """
    )
    op.create_unique_constraint(
        "uq_news_raw_raw_news_url", "news_raw", ["raw_news_url"]
    )

    # (b) news_raw.raw_news_created_at: VARCHAR -> timestamptz
    # NOT NULL을 먼저 풀어야, 파싱 실패한 값을 NULL로 캐스팅할 때 제약 위반으로
    # 막히지 않는다.
    op.execute("ALTER TABLE news_raw ALTER COLUMN raw_news_created_at DROP NOT NULL")
    op.execute(
        """
        CREATE FUNCTION news_raw_safe_to_timestamptz(text) RETURNS timestamptz AS $$
        BEGIN
            RETURN $1::timestamptz;
        EXCEPTION WHEN OTHERS THEN
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql STABLE
        """
    )
    op.execute(
        """
        ALTER TABLE news_raw
            ALTER COLUMN raw_news_created_at TYPE timestamptz
            USING news_raw_safe_to_timestamptz(raw_news_created_at)
        """
    )
    op.execute("DROP FUNCTION news_raw_safe_to_timestamptz(text)")

    # (c) user_newsletter_ctr_log(user_id, created_at DESC) 인덱스
    op.create_index(
        "ix_user_newsletter_ctr_log_user_id_created_at",
        "user_newsletter_ctr_log",
        ["user_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_user_newsletter_ctr_log_user_id_created_at",
        table_name="user_newsletter_ctr_log",
    )

    # news_raw_safe_to_timestamptz가 NULL로 만든 값은 원래 문자열로 복원할
    # 수 없다 - text로 되돌리되 NOT NULL은 다시 걸지 않는다.
    op.execute(
        "ALTER TABLE news_raw ALTER COLUMN raw_news_created_at TYPE VARCHAR "
        "USING raw_news_created_at::text"
    )

    op.drop_constraint("uq_news_raw_raw_news_url", "news_raw", type_="unique")
    # 중복 제거로 지워진 행은 복원할 수 없다.
