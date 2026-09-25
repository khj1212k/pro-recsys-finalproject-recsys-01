# RSS 수집기
import time
import feedparser, logging
from datetime import datetime, timedelta, timezone
from typing import Dict
from dateutil import parser as date_parser
from psycopg2.extras import execute_values
from db.connection import get_connection, release_connection
from config.settings import Settings

logger = logging.getLogger(__name__)


def parse_feed_with_retry(url, parse_fn=None, max_attempts=None, sleep_fn=time.sleep):
    """feedparser.parse를 지수 백오프로 재시도한다.

    feedparser는 네트워크/파싱 실패 시 예외를 던지는 대신 feed.bozo=1로 표시하므로
    이를 재시도 트리거로 사용한다. parse_fn/sleep_fn은 테스트에서 실제 네트워크
    호출/대기 없이 검증하기 위해 주입 가능하게 열어둔다.
    """
    parse_fn = parse_fn or feedparser.parse
    max_attempts = max_attempts or Settings.MAX_FETCH_RETRIES
    delay = 1.0
    feed = None
    for attempt in range(max_attempts):
        feed = parse_fn(url)
        if not getattr(feed, 'bozo', 0):
            return feed
        if attempt < max_attempts - 1:
            sleep_fn(delay)
            delay = min(delay * Settings.RETRY_EXPONENTIAL_BASE, Settings.MAX_RETRY_WAIT_SECONDS)
    return feed


def collect_rss(hours: int = 100) -> Dict[str, int]:
    """RSS 피드 수집 실행.

    이전에는 INSERT 전에 SELECT raw_news_url ... WHERE raw_news_url IN %s로
    기존 URL을 조회해 후보를 걸러냈다. 이 방식은 조회와 삽입 사이에 다른
    프로세스가 같은 URL을 먼저 넣으면(동시 수집 실행 등) UniqueViolation으로
    깨질 수 있는 TOCTOU다. INSERT ... ON CONFLICT (raw_news_url) DO NOTHING으로
    바꿔 DB가 원자적으로 충돌을 처리하게 하고, RETURNING으로 실제 삽입된 행만
    돌려받아 언론사(press)별 SAVEPOINT 안에서 inserted/skipped 건수를 집계한다.
    """
    conn = get_connection()
    total_inserted = 0
    total_skipped = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    logger.info(f"RSS 수집 시작 (Cutoff: {hours}h)")

    try:
        with conn.cursor() as cur:
            for press_name, (_, url) in Settings.RSS_FEEDS.items():
                cur.execute("SAVEPOINT sp_press")
                try:
                    press_name = '전자신문' if press_name.startswith('전자신문') else press_name
                    cur.execute("SELECT press_id \
                                       FROM press \
                                       WHERE press_name=%s",
                                       (press_name,))
                    row = cur.fetchone()
                    if not row:
                        logger.error(f"❌ {press_name} press_id 없음 - 스킵")
                        continue
                    pid = row[0]
                    feed = parse_feed_with_retry(url)
                    entries = []

                    # 1. 파싱 및 날짜 필터링
                    for e in feed.entries:
                        try:
                            dt_str = e.get('published') or e.get('updated')
                            dt = date_parser.parse(dt_str)
                            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                            if dt >= cutoff: entries.append((e.link, e.title, dt)) # cutoff 시간 이후의 기사만 추가
                        except (ValueError, TypeError, OverflowError) as date_err:
                            logger.debug(f"⚠️ {press_name} 날짜 파싱 실패: {dt_str!r} - {date_err}")
                            continue

                    if not entries:
                        continue

                    # 2. ON CONFLICT DO NOTHING으로 삽입, RETURNING으로 실제
                    #    삽입된 URL만 받아 inserted/skipped 건수 계산
                    candidates = [
                        (pid, title, '', link, dt, datetime.now())
                        for link, title, dt in entries
                    ]
                    inserted_rows = execute_values(
                        cur,
                        """
                        INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, raw_news_crawled_at)
                        VALUES %s
                        ON CONFLICT (raw_news_url) DO NOTHING
                        RETURNING raw_news_url
                        """,
                        candidates,
                        fetch=True,
                    )
                    inserted_count = len(inserted_rows)
                    skipped_count = len(candidates) - inserted_count
                    total_inserted += inserted_count
                    total_skipped += skipped_count
                    logger.info(f"✅ {press_name}: 신규 {inserted_count}건 추가, {skipped_count}건 스킵(중복)")

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_press")
                    logger.error(f"❌ {press_name} 오류: {e}")
        conn.commit() # 변경사항 저장
    except Exception as e:
        conn.rollback()
        logger.error(f"Global Error: {e}")
    finally:
        release_connection(conn)

    logger.info(f"✨ 총 {total_inserted}건 수집 완료 ({total_skipped}건 스킵)")
    return {"inserted": total_inserted, "skipped": total_skipped}

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collect_rss()
