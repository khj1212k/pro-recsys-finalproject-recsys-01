# RSS 수집기
import socket
import time
from contextlib import contextmanager
import feedparser, logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Set
from urllib.parse import urlparse
from dateutil import parser as date_parser
from psycopg2.extras import execute_values
from db.connection import get_connection, release_connection
from config.settings import Settings
from config.sources import press_name_for_feed

logger = logging.getLogger(__name__)

# 시간대 없는 pubDate는 KST 벽시계 시각으로 읽는다 (docs/adr/0008 "2026-10-06 갱신").
# 설정된 피드는 모두 한국 언론사이고, 그중 AI타임스가 "2026-10-06 09:00:00"처럼 시간대를 빼고 내보낸다.
# 여기에 UTC를 붙이던 때에는 발행 시각이 9시간 뒤(수집 시각보다 미래)로 저장됐다.
# 한국은 서머타임이 없어 고정 +09:00이면 되고, 이미지에 tzdata가 없어도 된다.
KST = timezone(timedelta(hours=9))


@contextmanager
def _socket_timeout(seconds):
    """이 블록에서 새로 여는 소켓의 대기 1번(연결·읽기)을 seconds로 묶는다.

    feedparser는 urllib을 timeout 없이 부르고 timeout 인자도 받지 않는다 - 그래서 프로세스 기본값을 잠깐 바꾼다.
    ingest의 RSS 단계는 한 스레드에서 차례로 돌고, DB 연결(psycopg2/libpq)은 파이썬 소켓을 쓰지 않아 영향이 없다.
    """
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(seconds)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous)


def _fetch_feed(url):
    # 한국경제는 feedparser 기본 UA("feedparser/x.y ...")에 403을 준다 - UA를 명시한다.
    # 소켓 대기 1번은 RSS_SOCKET_TIMEOUT_S(120초)로 묶는다(ADR 0026 결정 9). 응답 헤더나 본문 도중에 걸린 시간 초과는
    # feedparser가 잡지 않고 올리므로 collect_rss가 그 피드의 error로 남긴다(재시도 없음). 연결 단계의 시간 초과는
    # feedparser가 bozo로 돌려줘 parse_feed_with_retry가 MAX_FETCH_RETRIES번까지 다시 보낸다.
    with _socket_timeout(Settings.RSS_SOCKET_TIMEOUT_S):
        return feedparser.parse(url, agent=Settings.HTTP_USER_AGENT)


def parse_feed_with_retry(url, parse_fn=None, max_attempts=None, sleep_fn=time.sleep):
    """feedparser.parse를 지수 백오프로 재시도한다.

    feedparser는 네트워크/파싱 실패 시 예외를 던지는 대신 feed.bozo=1로 표시하므로
    이를 재시도 트리거로 사용한다. parse_fn/sleep_fn은 테스트에서 실제 네트워크
    호출/대기 없이 검증하기 위해 주입 가능하게 열어둔다.
    """
    parse_fn = parse_fn or _fetch_feed
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


def collect_rss(hours: int = 100) -> Dict[str, Any]:
    """RSS 피드 수집 실행.

    이전에는 INSERT 전에 SELECT raw_news_url ... WHERE raw_news_url IN %s로
    기존 URL을 조회해 후보를 걸러냈다. 이 방식은 조회와 삽입 사이에 다른
    프로세스가 같은 URL을 먼저 넣으면(동시 수집 실행 등) UniqueViolation으로
    깨질 수 있는 TOCTOU다. INSERT ... ON CONFLICT (raw_news_url) DO NOTHING으로
    바꿔 DB가 원자적으로 충돌을 처리하게 하고, RETURNING으로 실제 삽입된 행만
    돌려받아 언론사(press)별 SAVEPOINT 안에서 inserted/skipped 건수를 집계한다.

    반환값의 per_feed는 피드(Settings.RSS_FEEDS 키)별 entries/inserted/skipped(중복)/
    too_old(cutoff 이전)/bad_date(날짜 파싱 실패)/error로, jobs.run ingest가
    job_runs.stats에 그대로 남긴다(수집 누락이 어느 피드에서 났는지 추적용).
    """
    conn = get_connection()
    total_inserted = 0
    total_skipped = 0
    per_feed: Dict[str, Dict[str, Any]] = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    logger.info(f"RSS 수집 시작 (Cutoff: {hours}h)")
    # 이번 실행에서 피드를 읽지 못한 호스트. 같은 호스트의 남은 피드(국민일보 여섯·전자신문 셋)는 재시도 없이 건너뛴다 -
    # 연결 단계에서 막힌 사이트 하나가 피드마다 시도 3회 x 120초를 곱해 회차를 붙잡지 않게 한다(ADR 0026 결정 9).
    failed_hosts: Set[str] = set()

    try:
        with conn.cursor() as cur:
            for feed_name, (_, url) in Settings.RSS_FEEDS.items():
                press_name = press_name_for_feed(feed_name)
                host = urlparse(url).netloc
                feed_stats = {
                    "press": press_name, "entries": 0, "inserted": 0, "skipped": 0,
                    "too_old": 0, "bad_date": 0, "error": None,
                }
                per_feed[feed_name] = feed_stats
                cur.execute("SAVEPOINT sp_press")
                try:
                    cur.execute("SELECT press_id \
                                       FROM press \
                                       WHERE press_name=%s",
                                       (press_name,))
                    row = cur.fetchone()
                    if not row:
                        logger.error(f"❌ {press_name} press_id 없음 - 스킵")
                        feed_stats["error"] = "press_id not found"
                        continue
                    pid = row[0]
                    if host in failed_hosts:
                        feed_stats["error"] = f"skipped: {host} unreadable earlier in this run"
                        logger.error(f"❌ {feed_name} 건너뜀 - 같은 호스트({host})를 이번 실행에서 읽지 못했다")
                        continue
                    feed = parse_feed_with_retry(url)
                    feed_stats["entries"] = len(feed.entries)
                    if getattr(feed, 'bozo', 0) and not feed.entries:
                        # 재시도 후에도 bozo + 항목 0건이면 피드를 못 읽은 것(네트워크/차단 등)
                        status = getattr(feed, 'status', None)
                        http = f" (HTTP {status})" if status else ""
                        feed_stats["error"] = f"feed unreadable{http}: {getattr(feed, 'bozo_exception', 'bozo')}"[:300]
                        failed_hosts.add(host)
                        logger.error(f"❌ {feed_name} 피드를 읽지 못함{http}")
                    entries = []

                    # 1. 파싱 및 날짜 필터링
                    for e in feed.entries:
                        try:
                            dt_str = e.get('published') or e.get('updated')
                            dt = date_parser.parse(dt_str)
                            if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
                            if dt >= cutoff:
                                entries.append((e.link, e.title, dt)) # cutoff 시간 이후의 기사만 추가
                            else:
                                feed_stats["too_old"] += 1
                        except (ValueError, TypeError, OverflowError) as date_err:
                            feed_stats["bad_date"] += 1
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
                    feed_stats["inserted"] = inserted_count
                    feed_stats["skipped"] = skipped_count
                    total_inserted += inserted_count
                    total_skipped += skipped_count
                    logger.info(f"✅ {feed_name}: 신규 {inserted_count}건 추가, {skipped_count}건 스킵(중복)")

                except Exception as e:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_press")
                    failed_hosts.add(host)
                    feed_stats["inserted"] = feed_stats["skipped"] = 0
                    feed_stats["error"] = f"{type(e).__name__}: {e}"[:300]
                    logger.error(f"❌ {feed_name} 오류: {e}")
        conn.commit() # 변경사항 저장
    except Exception as e:
        conn.rollback()
        logger.error(f"Global Error: {e}")
        raise
    finally:
        release_connection(conn)

    failed_feeds = sum(1 for f in per_feed.values() if f["error"])
    logger.info(f"✨ 총 {total_inserted}건 수집 완료 ({total_skipped}건 스킵, 실패 피드 {failed_feeds}개)")
    return {
        "inserted": total_inserted,
        "skipped": total_skipped,
        "failed_feeds": failed_feeds,
        "per_feed": per_feed,
    }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collect_rss()
