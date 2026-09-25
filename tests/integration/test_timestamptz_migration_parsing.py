"""backend/alembic/versions/e725a62ffef1_...py가 news_raw.raw_news_created_at을
VARCHAR -> timestamptz로 바꿀 때 쓰는 안전 캐스팅 함수가, RSS 수집기(
ai_workspace/crawler/rss_collector.py)가 실제로 써온 형식(psycopg2 datetime
어댑터가 만드는 "YYYY-MM-DD HH:MM:SS[.ffffff]+HH:MM")은 제대로 파싱하고,
그 외 값은 예외 없이 NULL로 처리하는지 검증한다. 마이그레이션 자체(revision
e725a62ffef1)의 upgrade/downgrade 왕복은 CI 워크플로의 alembic
downgrade -1 && upgrade head 단계에서 별도로 검증된다.
"""
import uuid
from datetime import datetime, timezone

import pytest

# (입력 문자열, 파싱에 성공해야 하는지)
SAMPLE_INPUTS = [
    ("2026-09-25 10:00:00+09:00", True),  # rss_collector가 실제로 쓰는 형식 (마이크로초 없음)
    ("2026-09-25 10:00:00.123456+00:00", True),  # 마이크로초 포함
    ("2026-01-01 00:00:00+00:00", True),
    ("2026-12-31 23:59:59-05:00", True),
    ("이건 날짜가 아님", False),
    ("", False),
    ("not-a-date-2026", False),
    ("2026-13-99 99:99:99+00:00", False),  # 형식은 그럴듯하지만 값 자체가 무효
]


@pytest.fixture
def safe_cast_function(pg_conn):
    # backend/alembic/versions/e725a62ffef1_news_raw_url_unique_created_at_to_.py의
    # upgrade()에 있는 news_raw_safe_to_timestamptz 함수와 동일한 정의를 재사용해,
    # 마이그레이션이 실제로 쓰는 캐스팅 로직 자체를 검증한다(마이그레이션은 컬럼
    # 변환 후 이 함수를 DROP하므로, 여기서는 별도 이름으로 다시 만든다).
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION news_raw_safe_to_timestamptz_test(text) RETURNS timestamptz AS $$
            BEGIN
                RETURN $1::timestamptz;
            EXCEPTION WHEN OTHERS THEN
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql STABLE
            """
        )
    try:
        yield "news_raw_safe_to_timestamptz_test"
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DROP FUNCTION IF EXISTS news_raw_safe_to_timestamptz_test(text)")


@pytest.mark.parametrize("raw_value,should_parse", SAMPLE_INPUTS)
def test_safe_cast_parses_rss_collector_dates_and_nulls_malformed_values(
    database_url, pg_conn, safe_cast_function, raw_value, should_parse
):
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT {safe_cast_function}(%s)", (raw_value,))
        (result,) = cur.fetchone()

    if should_parse:
        assert result is not None
    else:
        assert result is None


def test_news_raw_created_at_column_is_timestamptz_and_round_trips_timezone_aware_datetime(
    database_url, pg_conn
):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'news_raw' AND column_name = 'raw_news_created_at'"
        )
        (data_type,) = cur.fetchone()
    assert data_type == "timestamp with time zone"

    press_id = None
    url = f"http://example.com/{uuid.uuid4().hex}"
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO press (press_name) VALUES (%s) RETURNING press_id",
                (f"tz-roundtrip-{uuid.uuid4().hex[:8]}",),
            )
            press_id = cur.fetchone()[0]

        # rss_collector.py가 실제로 넘기는 값과 동일하게, 이미 파싱된
        # timezone-aware datetime 객체를 파라미터로 넘긴다(psycopg2가 알아서 적응).
        dt = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, "
                "raw_news_created_at, raw_news_crawled_at) VALUES (%s, %s, %s, %s, %s, NOW())",
                (press_id, "제목", "", url, dt),
            )
            cur.execute(
                "SELECT raw_news_created_at FROM news_raw WHERE raw_news_url = %s", (url,)
            )
            (stored,) = cur.fetchone()

        assert stored == dt
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE raw_news_url = %s", (url,))
            if press_id:
                cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))
