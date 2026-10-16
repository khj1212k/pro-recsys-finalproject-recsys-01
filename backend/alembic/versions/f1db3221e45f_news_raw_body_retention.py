"""news_raw body retention: nullable body, body length, purge time, KOGL type (ADR 0023)

상업 언론사 본문은 수집 후 30일이 지나면 비운다(`python -m jobs.run retention`, ADR 0023). 그 잡이 쓰는 스키마.
- raw_news_content의 NOT NULL을 푼다. 비운 행은 NULL이다(''는 "추출했지만 본문 없음"으로 이미 쓰이고 있다).
- raw_news_content_length integer: 본문 글자 수(char_length, 파이썬 len과 같은 코드 포인트 수). 본문을 쓸 때
  (본문 추출기·정책브리핑 수집기) 함께 쓰고, 본문을 비운 뒤에도 남는다. 기존 행은 아래에서 채운다.
- raw_news_content_purged_at timestamptz: 본문을 비운 시각. NULL이면 비운 적 없음.
- raw_news_kogl_type smallint: 정책브리핑 기사의 공공누리 유형(1~4). 수집할 때 거르기만 하고 저장하지 않던 값을
  행마다 남긴다(ADR 0023 "남는 위험"). 상업 언론사 행은 NULL.
- CHECK ck_news_raw_purged_body_gone: 비운 행(purged_at 있음)은 본문이 NULL이고 길이가 있어야 한다 - 어떤 코드도
  비운 행에 본문을 다시 쓸 수 없다.

락과 I/O (Tier 0: E2.1.Micro 1 GB, 2026-10-16 news_raw 30,712행 = 힙 28 MB + TOAST 49 MB, 본문 27,070개):
1. DDL은 카탈로그만 바꾼다 - DROP NOT NULL, 기본값 없는 ADD COLUMN, NOT VALID CHECK는 테이블을 읽지도 다시
   쓰지도 않는다. ACCESS EXCLUSIVE 락은 이 리비전까지의 트랜잭션이 커밋될 때까지(수 밀리초) 잡힌다.
   lock_timeout 10초: 긴 트랜잭션이 news_raw를 잡고 있으면 그 뒤에 줄을 서서 다른 쿼리까지 막는 대신 실패한다.
2. 그 트랜잭션을 커밋한 뒤(autocommit 블록) CHECK를 VALIDATE한다 - SHARE UPDATE EXCLUSIVE라 읽기·쓰기를 막지
   않고, 힙만 한 번 읽는다(TOAST는 읽지 않는다).
3. 길이 채우기는 id 순서 2,000행씩, 배치마다 커밋한다. 행 락은 그 배치 동안만 잡힌다. 바뀌는 것은 정수 열
   하나지만 PostgreSQL은 행 전체의 새 버전을 쓴다: 힙에 인라인으로 들어 있는 (압축된) 본문은 새 버전으로 복사되고,
   TOAST로 나간 본문은 포인터만 복사된다(TOAST는 다시 쓰지 않는다). char_length가 본문을 한 번씩 풀어 읽는다
   (원문 약 114 MB). 그래서 비용은 "힙 한 벌을 다시 쓰고 본문을 한 번 읽는 것"이고, 죽은 행 버전은 autovacuum이
   회수해 다시 쓴다. 측정은 CI의 Postgres에서 30,000개 합성 행으로 했다 - ADR 0023 개정(2026-10-17).
4. downgrade는 본문을 되살리지 않는다: 비운 행은 ''로 두고(이전 스키마의 NOT NULL을 만족하는 가장 작은 값)
   NOT NULL을 되돌린다. SET NOT NULL은 ACCESS EXCLUSIVE 아래에서 힙을 한 번 읽는다. 길이·비운 시각·공공누리 유형
   열은 사라진다(해시 열은 앞 리비전 것이라 남는다).

Revision ID: f1db3221e45f
Revises: a6f1e83b0d57
Create Date: 2026-10-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import context, op


# revision identifiers, used by Alembic.
revision: str = 'f1db3221e45f'
down_revision: Union[str, Sequence[str], None] = 'a6f1e83b0d57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DDL_LOCK_TIMEOUT = "10s"
BACKFILL_BATCH_ROWS = 2000
BACKFILL_STATEMENT_TIMEOUT = "60s"

KOGL_CHECK = "ck_news_raw_kogl_type"
PURGED_CHECK = "ck_news_raw_purged_body_gone"

# 한 배치: id 순서로 다음 BACKFILL_BATCH_ROWS개 행을 고르고, 그중 본문 칸이 있고 길이가 비어 있는 행만 채운다.
# 반환: (이번 배치의 마지막 id - 없으면 NULL = 끝, 채운 행 수)
_BACKFILL_BATCH_SQL = """
WITH batch AS (
    SELECT raw_news_id FROM news_raw
    WHERE raw_news_id > %(last_id)s
    ORDER BY raw_news_id
    LIMIT %(batch_rows)s
), filled AS (
    UPDATE news_raw n
    SET raw_news_content_length = char_length(n.raw_news_content)
    FROM batch
    WHERE n.raw_news_id = batch.raw_news_id
      AND n.raw_news_content IS NOT NULL
      AND n.raw_news_content_length IS NULL
    RETURNING 1
)
SELECT (SELECT max(raw_news_id) FROM batch), (SELECT count(*) FROM filled)
"""


def upgrade() -> None:
    """Upgrade schema."""
    # 1) 카탈로그만 바꾸는 DDL. 다시 돌려도 되게 IF NOT EXISTS / DROP IF EXISTS로 쓴다 - 아래 2·3이 실패하면
    #    이 DDL은 커밋된 채 alembic_version만 앞 리비전에 남고, 다시 upgrade하면 이 함수가 처음부터 돈다.
    op.execute(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'")
    op.execute("ALTER TABLE news_raw ALTER COLUMN raw_news_content DROP NOT NULL")
    op.execute("ALTER TABLE news_raw ADD COLUMN IF NOT EXISTS raw_news_content_length integer")
    op.execute("ALTER TABLE news_raw ADD COLUMN IF NOT EXISTS raw_news_content_purged_at timestamp with time zone")
    op.execute("ALTER TABLE news_raw ADD COLUMN IF NOT EXISTS raw_news_kogl_type smallint")
    op.execute(f"ALTER TABLE news_raw DROP CONSTRAINT IF EXISTS {KOGL_CHECK}")
    op.execute(
        f"ALTER TABLE news_raw ADD CONSTRAINT {KOGL_CHECK} "
        "CHECK (raw_news_kogl_type IS NULL OR raw_news_kogl_type BETWEEN 1 AND 4) NOT VALID"
    )
    op.execute(f"ALTER TABLE news_raw DROP CONSTRAINT IF EXISTS {PURGED_CHECK}")
    op.execute(
        f"ALTER TABLE news_raw ADD CONSTRAINT {PURGED_CHECK} "
        "CHECK (raw_news_content_purged_at IS NULL "
        "OR (raw_news_content IS NULL AND raw_news_content_length IS NOT NULL)) NOT VALID"
    )

    if context.is_offline_mode():
        # `alembic upgrade --sql`: 행 수를 읽으며 반복할 수 없으므로 한 문장으로 낸다(사람이 검토해서 돌리는 경로).
        op.execute(f"ALTER TABLE news_raw VALIDATE CONSTRAINT {KOGL_CHECK}")
        op.execute(f"ALTER TABLE news_raw VALIDATE CONSTRAINT {PURGED_CHECK}")
        op.execute(
            "UPDATE news_raw SET raw_news_content_length = char_length(raw_news_content) "
            "WHERE raw_news_content IS NOT NULL AND raw_news_content_length IS NULL"
        )
        return

    # 2·3) 위 DDL(과 같은 실행에서 앞서 돈 리비전)을 커밋해 ACCESS EXCLUSIVE를 놓은 뒤, 문장마다 커밋한다.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.exec_driver_sql(f"SET lock_timeout = '{DDL_LOCK_TIMEOUT}'")
        bind.exec_driver_sql(f"SET statement_timeout = '{BACKFILL_STATEMENT_TIMEOUT}'")
        try:
            bind.exec_driver_sql(f"ALTER TABLE news_raw VALIDATE CONSTRAINT {KOGL_CHECK}")
            bind.exec_driver_sql(f"ALTER TABLE news_raw VALIDATE CONSTRAINT {PURGED_CHECK}")
            last_id, batches, filled = 0, 0, 0
            while True:
                batch_last, batch_filled = bind.exec_driver_sql(
                    _BACKFILL_BATCH_SQL, {"last_id": last_id, "batch_rows": BACKFILL_BATCH_ROWS},
                ).fetchone()
                if batch_last is None:
                    break
                last_id, batches, filled = batch_last, batches + 1, filled + batch_filled
            # env.py의 fileConfig가 로거를 꺼 두므로 print로 남긴다(jobs.migrate와 같은 이유).
            print(f"[migrate] f1db3221e45f: 본문 길이 {filled}행 채움 ({batches}배치, 배치당 {BACKFILL_BATCH_ROWS}행)",
                  flush=True)
        finally:
            bind.exec_driver_sql("RESET statement_timeout")
            bind.exec_driver_sql("RESET lock_timeout")


def downgrade() -> None:
    """Downgrade schema. 본문을 되살리지 않는다 - 비운 행은 ''가 된다."""
    op.execute(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE news_raw DROP CONSTRAINT IF EXISTS {PURGED_CHECK}")
    op.execute(f"ALTER TABLE news_raw DROP CONSTRAINT IF EXISTS {KOGL_CHECK}")
    op.execute("UPDATE news_raw SET raw_news_content = '' WHERE raw_news_content IS NULL")
    op.execute("ALTER TABLE news_raw ALTER COLUMN raw_news_content SET NOT NULL")
    op.execute("ALTER TABLE news_raw DROP COLUMN IF EXISTS raw_news_kogl_type")
    op.execute("ALTER TABLE news_raw DROP COLUMN IF EXISTS raw_news_content_purged_at")
    op.execute("ALTER TABLE news_raw DROP COLUMN IF EXISTS raw_news_content_length")
