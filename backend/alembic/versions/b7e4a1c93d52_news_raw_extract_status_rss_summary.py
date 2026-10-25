"""news_raw extraction statuses 'rss_summary' / 'blocked_origin' (ADR 0026 2026-10-25, 범위 종결 C-31)

동아일보는 VM(OCI) 출발지의 본문 요청을 거절한다(ADR 0026 결정 9·증거 10). 수집기는 동아일보 RSS 요약을 본문 칸에 바로
저장하고 본문 추출 대상에서 뺀다. 그 행을 나타내는 두 상태를 CHECK 제약 ck_news_raw_extract_status에 더한다 - 제약에 없으면
수집기의 INSERT가 CheckViolation으로 매 회차 통째로 실패한다.

- 'rss_summary': 본문 칸이 RSS 요약(약 500자)인 행. 본문 추출기가 다시 고르지 않는다(추출 대기 = 상태 NULL 또는
  fetch_failed/error). 동결 반출(실험)은 'ok'만 고르므로 요약 행은 들어가지 않는다. 임베딩·클러스터에는 들어간다.
- 'blocked_origin': 배포 전에 들어온 동아일보 행 중 본문을 영영 받지 못하는 것을 본문 없이 닫은 상태
  (scripts/oci/close_donga_pending.sql이 쓴다).

uq_news_raw_content_sha256_ok는 바꾸지 않는다. 같은 요약이 두 행에 들어가는 것은 수집기가 막는다(같은 해시의 'ok'/
'rss_summary' 행이 이미 있으면 'duplicate'로 넣는다 - crawler/rss_collector.py). 그 조회가 'rss_summary' 쪽에서도 인덱스를
타도록 부분 인덱스 ix_news_raw_content_sha256_rss_summary(해시, WHERE 상태 = 'rss_summary')를 더한다 - 'ok' 쪽은 기존
uq_news_raw_content_sha256_ok가 덮는다. 유일 인덱스가 아니다: 수집기의 INSERT ... ON CONFLICT (raw_news_url) DO NOTHING이 다른
인덱스의 충돌로 깨지면 안 되고, 중복 방지는 수집기가 맡는다. 이 인덱스가 없으면 조회의 조건(상태 IN ('ok', 'rss_summary'))이
'ok'만 덮는 기존 인덱스의 술어를 함의하지 않아 플래너가 news_raw 전체를 순차로 읽는다(2026-10-25 VM EXPLAIN: Seq Scan,
힙 54 MB·4.3만 행, 해시 50개에 0.18~0.52초(두 번 쟀다) - 동아일보 피드를 읽을 때마다, 테이블 크기에 비례해 늘어난다).

락과 I/O (Tier 0: E2.1.Micro 1 GB, news_raw 약 4만 행 - 힙 54 MB):
1. 인덱스를 먼저 만든다 - 이 시점에 'rss_summary' 행이 없어 인덱스는 비지만, 만드는 동안 힙을 한 번 읽는다(SHARE 락:
   읽기는 되고 쓰기만 기다린다, 1초 안팎). 같은 트랜잭션이라 그 락은 커밋까지 이어지므로 이 마이그레이션은 수집기가 도는
   중에 돌리지 않는다(런북 6.9: 스케줄러를 멈춘 상태에서 한다). CONCURRENTLY를 쓰지 않는 이유: 다른 세션이 열어 둔 트랜잭션이
   끝나기를 무한히 기다릴 수 있고, 이 크기에서는 얻는 것이 없다.
2. 제약을 넓히기만 하므로 기존 행은 모두 새 제약을 만족한다. DROP + ADD ... NOT VALID는 테이블을 읽지 않는다
   (ACCESS EXCLUSIVE는 이 트랜잭션이 커밋될 때까지, 수 밀리초). lock_timeout 10초: 긴 트랜잭션이 news_raw를 잡고 있으면
   그 뒤에 줄을 서서 다른 쿼리까지 막는 대신 실패한다(f1db3221e45f와 같은 방식).
3. 커밋한 뒤(autocommit 블록) VALIDATE한다 - SHARE UPDATE EXCLUSIVE라 읽기·쓰기를 막지 않는다.

downgrade는 이전 스키마에 없는 두 상태를 먼저 바꾼 뒤 제약을 좁힌다:
- 인덱스 ix_news_raw_content_sha256_rss_summary를 지운다(아래 매핑으로 'rss_summary' 행이 없어진다).
- 'blocked_origin' -> 'fetch_failed'이고 시도 횟수를 MAX_EXTRACT_ATTEMPTS(환경 변수, 없으면 3) 이상으로 올린다.
  이전 코드에서 "본문 요청이 계속 실패해 재시도를 마친 행"이 되어 다시 내려받지 않는다(본문은 처음부터 ''였다).
- 'rss_summary' -> 'dropped'이고 본문을 ''(길이 0)로, 임베딩을 NULL로 비운다. 근거: 이전 코드에는 "본문이 아닌 텍스트를
  든 행"이라는 개념이 없다. 'ok'로 두면 요약이 본문 행으로 읽혀 동결 반출·평가에 섞이고, 같은 요약이 둘이면 'ok' 해시
  유일 인덱스에 걸려 downgrade가 실패할 수 있다. 상태를 NULL로 두면 이전 추출기가 동아일보 본문을 다시 요청한다(VM 출발지는
  거절된다). 'dropped'는 다시 받아도 결과가 같다고 보는 상태라 어느 코드도 다시 집지 않는다. 잃는 것은 요약 텍스트뿐이고
  (본문 보존 기한 30일이 지나면 어차피 지워진다) 제목·URL·발행 시각·해시는 남는다. 보존 잡이 이미 본문을 NULL로 비운 행
  (raw_news_content_purged_at 있음)은 CHECK ck_news_raw_purged_body_gone(비운 행은 본문 NULL)을 지키려고 본문·길이를
  건드리지 않고 상태만 바꾼다. 다시 올려도 요약은 돌아오지 않는다(상태는 'dropped'로 남는다).

Revision ID: b7e4a1c93d52
Revises: f1db3221e45f
Create Date: 2026-10-25 12:00:00.000000

"""
import os
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import context, op


# revision identifiers, used by Alembic.
revision: str = 'b7e4a1c93d52'
down_revision: Union[str, Sequence[str], None] = 'f1db3221e45f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DDL_LOCK_TIMEOUT = "10s"
VALIDATE_STATEMENT_TIMEOUT = "60s"
CHECK_NAME = "ck_news_raw_extract_status"
# 요약 행의 중복 조회용 부분 인덱스(모델 backend/app/models/news.py의 Index와 이름·열·술어가 같아야 한다).
SUMMARY_HASH_INDEX = "ix_news_raw_content_sha256_rss_summary"

OLD_EXTRACT_STATUSES = ("ok", "dropped", "empty", "fetch_failed", "error", "duplicate")
EXTRACT_STATUSES = OLD_EXTRACT_STATUSES + ("rss_summary", "blocked_origin")

# downgrade에서 'blocked_origin' 행의 시도 횟수를 올릴 상한. 추출기의 Settings.MAX_EXTRACT_ATTEMPTS와 같은 환경 변수·기본값
# (config/settings.py)이다 - 마이그레이션은 앱 설정 모듈을 임포트하지 않는다. 같은 컨테이너 환경을 읽으므로 두 값이 같다.
# compose는 이 변수를 컨테이너로 넘기지 않아(AI_ENV_FILE로 넘기는 경우만 예외) 호스트 .env에 적어도 소용없고, 기본 3이 된다.
DEFAULT_MAX_EXTRACT_ATTEMPTS = 3
SMALLINT_MAX = 32767


def _status_check(statuses) -> str:
    return (
        "raw_news_extract_status IS NULL OR raw_news_extract_status IN ("
        + ", ".join(f"'{s}'" for s in statuses) + ")"
    )


def _max_extract_attempts() -> int:
    raw = os.environ.get("MAX_EXTRACT_ATTEMPTS", str(DEFAULT_MAX_EXTRACT_ATTEMPTS)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"MAX_EXTRACT_ATTEMPTS는 정수여야 한다: {raw!r}") from None
    if not 1 <= value <= SMALLINT_MAX:
        raise RuntimeError(f"MAX_EXTRACT_ATTEMPTS는 1 이상 {SMALLINT_MAX} 이하여야 한다: {value}")
    return value


def _create_summary_hash_index() -> None:
    op.create_index(
        SUMMARY_HASH_INDEX,
        "news_raw",
        ["raw_news_content_sha256"],
        postgresql_where=sa.text("raw_news_extract_status = 'rss_summary'"),
        if_not_exists=True,
    )


def _replace_check(statuses) -> None:
    """제약을 NOT VALID로 다시 만든다(락은 카탈로그만 바꾸는 동안). 다시 돌려도 되게 DROP IF EXISTS로 쓴다."""
    op.execute(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'")
    op.execute(f"ALTER TABLE news_raw DROP CONSTRAINT IF EXISTS {CHECK_NAME}")
    op.execute(f"ALTER TABLE news_raw ADD CONSTRAINT {CHECK_NAME} CHECK ({_status_check(statuses)}) NOT VALID")


def _validate_check() -> None:
    if context.is_offline_mode():
        # `alembic upgrade --sql`: 한 문장으로 낸다(사람이 검토해서 돌리는 경로).
        op.execute(f"ALTER TABLE news_raw VALIDATE CONSTRAINT {CHECK_NAME}")
        return
    # 위 DDL(과 같은 실행에서 앞서 돈 리비전)을 커밋해 ACCESS EXCLUSIVE를 놓은 뒤 검증한다.
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        bind.exec_driver_sql(f"SET lock_timeout = '{DDL_LOCK_TIMEOUT}'")
        bind.exec_driver_sql(f"SET statement_timeout = '{VALIDATE_STATEMENT_TIMEOUT}'")
        try:
            bind.exec_driver_sql(f"ALTER TABLE news_raw VALIDATE CONSTRAINT {CHECK_NAME}")
        finally:
            bind.exec_driver_sql("RESET statement_timeout")
            bind.exec_driver_sql("RESET lock_timeout")


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'")
    _create_summary_hash_index()
    _replace_check(EXTRACT_STATUSES)
    _validate_check()


def downgrade() -> None:
    """Downgrade schema. 요약 텍스트는 되살리지 않는다 - 위 docstring의 매핑."""
    attempts = _max_extract_attempts()
    op.execute(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'")
    op.execute(
        "UPDATE news_raw "
        "SET raw_news_extract_status = 'fetch_failed', "
        f"    raw_news_extract_attempts = GREATEST(raw_news_extract_attempts, {attempts}) "
        "WHERE raw_news_extract_status = 'blocked_origin'"
    )
    op.execute(
        "UPDATE news_raw "
        "SET raw_news_extract_status = 'dropped', "
        "    raw_news_content = CASE WHEN raw_news_content_purged_at IS NULL THEN '' ELSE raw_news_content END, "
        "    raw_news_content_length = CASE WHEN raw_news_content_purged_at IS NULL THEN 0 "
        "                                   ELSE raw_news_content_length END, "
        "    embedding_result = NULL "
        "WHERE raw_news_extract_status = 'rss_summary'"
    )
    op.drop_index(SUMMARY_HASH_INDEX, table_name="news_raw", if_exists=True)
    _replace_check(OLD_EXTRACT_STATUSES)
    _validate_check()
