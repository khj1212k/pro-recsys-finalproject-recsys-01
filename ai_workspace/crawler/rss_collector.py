# RSS 수집기
import hashlib
import html
import re
import socket
import time
from collections import Counter
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

# RSS_FEEDS의 전략 값. 'rss_summary' 피드는 본문을 받지 않고 RSS 요약을 본문 칸에 바로 저장한다(동아일보).
RSS_SUMMARY = "rss_summary"
# 요약 행이 받는 상태들(news_raw.raw_news_extract_status의 CHECK 제약에 있는 값 - backend/alembic/versions/b7e4a1c93d52).
RSS_SUMMARY_STATUS = "rss_summary"       # 요약을 본문 칸에 둔 행. 운영 임베딩·클러스터에 들어간다
SUMMARY_DROPPED_STATUS = "dropped"       # 요약이 짧거나 없다(is_drop_article) - 본문 추출기의 'dropped'와 같다. 본문 ''
SUMMARY_DUPLICATE_STATUS = "duplicate"   # 같은 요약이 이미 있다(URL만 다른 같은 기사) - 본문 추출기의 'duplicate'와 같다. 본문 ''
# 요약 행의 중복 기준: 같은 해시를 가진 이 상태의 행이 이미 있으면 'duplicate'로 넣는다. uq_news_raw_content_sha256_ok는
# 'ok'만 덮으므로(요약 행은 그 인덱스 밖이다) 'rss_summary'는 수집기가 직접 본다.
SUMMARY_DEDUPE_STATUSES = ("ok", RSS_SUMMARY_STATUS)
# 요약에는 쓰지 않는 is_drop_article 사유. 리스트성 마커('실시간'·'랭킹' 등)는 700자 미만 본문에서만 의미가 있는데 요약은
# 길어야 500자라 항상 그 안에 든다 - VM의 'ok' 본문(3.6만 건) 앞 500자의 1.4%(동아일보 3,022건 중 45건)가 마커를 담는다.
# 본문 전체로는 통과한 기사를 요약만 보고 버리게 된다.
SUMMARY_IGNORED_DROP_REASONS = ("list_like_markers",)

# 줄바꿈·문단을 뜻하는 태그는 공백으로, 나머지 태그(img·b·span·a 등)는 빈 문자열로 걷는다 - <b>대</b>통령이 "대 통령"이 되지 않게.
_BLOCK_TAG = re.compile(r"</?(?:br|p|div|li)\b[^<>]*>", re.IGNORECASE)
# 태그는 '<' 바로 뒤에 영문자나 '/'가 오는 것만 본다 - "3 < 5 그리고 7 > 2" 같은 본문 속 부등호를 지우지 않는다.
_TAG = re.compile(r"<!--.*?-->|</?[A-Za-z][^<>]*>", re.DOTALL)
_SPACE = re.compile(r"\s+")


def rss_summary_text(entry) -> str:
    """RSS 항목의 요약(summary/description)을 태그·HTML 엔티티를 걷어 한 줄 텍스트로 만든다. 없으면 ''.

    블록 태그(br·p·div·li)만 공백으로 바꾸고 나머지 태그는 지운다. 엔티티를 푼 뒤 태그를 한 번 더 지운다 -
    피드가 태그를 &lt;p&gt;처럼 이스케이프해 보내면 푼 뒤에야 태그가 보인다.
    """
    raw = entry.get('summary') or entry.get('description') or ''
    text = _TAG.sub('', _BLOCK_TAG.sub(' ', raw))
    text = _TAG.sub('', _BLOCK_TAG.sub(' ', html.unescape(text)))
    return _SPACE.sub(' ', text).strip()


def summary_drop_reasons(summary: str, title: str, press_name: str):
    """요약을 본문 추출기와 같은 기준(crawler.content_extractor.cleaners.is_drop_article)으로 걸러 사유를 돌려준다.
    사유가 없으면 []. 요약에 맞지 않는 사유(SUMMARY_IGNORED_DROP_REASONS)는 뺀다. 요약이 비었으면 길이 사유가 붙는다.

    추출기 패키지는 trafilatura를 끌어오므로 요약 피드를 처리할 때만 임포트한다(같은 ingest 프로세스가 곧바로 추출기를 쓴다).
    """
    from crawler.content_extractor.cleaners import is_drop_article

    _, reasons = is_drop_article(summary, title, press_name)
    return [r for r in reasons if r not in SUMMARY_IGNORED_DROP_REASONS]


def _summary_digest(summary: str) -> str:
    """요약의 해시. 본문 추출기의 content_sha256(정제 본문의 UTF-8 sha256 hex)과 같은 정의다."""
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def _existing_urls(cur, urls) -> Set[str]:
    """urls 가운데 news_raw에 이미 있는 것. uq_news_raw_raw_news_url을 타는 조회라 비용이 작다."""
    cur.execute("SELECT raw_news_url FROM news_raw WHERE raw_news_url = ANY(%s)", (sorted(set(urls)),))
    return {row[0] for row in cur.fetchall()}


def _existing_summary_hashes(cur, digests) -> Set[str]:
    """digests 가운데 SUMMARY_DEDUPE_STATUSES 행이 이미 갖고 있는 해시(같은 트랜잭션에서 앞서 넣은 행 포함).

    상태마다 SELECT를 따로 내고 UNION한다 - 상태 IN (...) 한 줄로 묻으면 플래너가 부분 인덱스의 술어
    (uq_news_raw_content_sha256_ok의 상태 = 'ok', ix_news_raw_content_sha256_rss_summary의 상태 = 'rss_summary')를
    함의로 증명하지 못해 news_raw 전체를 순차로 읽는다(VM EXPLAIN으로 확인, b7e4a1c93d52). 상태는 psycopg2가 SQL에 리터럴로
    채워 넣으므로 플래너가 술어를 증명한다.
    """
    select = ("SELECT raw_news_content_sha256 FROM news_raw "
              "WHERE raw_news_content_sha256 = ANY(%s) AND raw_news_extract_status = %s")
    params = []
    for status in SUMMARY_DEDUPE_STATUSES:
        params += [sorted(digests), status]
    cur.execute(" UNION ".join([select] * len(SUMMARY_DEDUPE_STATUSES)), params)
    return {row[0] for row in cur.fetchall()}


def build_insert_rows(cur, pid, press_name, strategy, entries, now_local, now_utc):
    """(링크, 제목, 발행 시각, 요약) 항목을 INSERT 행으로 만든다. 돌려주는 값: (행 목록, 링크 -> 요약 행 상태).

    행 = (press_id, 제목, 본문, 링크, 발행 시각, 수집 시각, 추출 상태, 해시, 길이, 추출 시각).
    - 일반 피드('direct'): 본문 ''에 상태 NULL - 본문 추출기가 채운다.
    - 'rss_summary' 피드: 요약을 본문 칸에 넣고 상태 'rss_summary'로 닫아 본문 추출 대상에서 뺀다. 단
      · 이미 DB에 있는 URL은 행을 만들지 않는다 - INSERT ... ON CONFLICT DO NOTHING이 어차피 건너뛰어 요약을 받지 못하는데,
        그 항목이 요약 해시를 먼저 차지하면 같은 요약을 가진 새 URL이 'duplicate'(본문 '')로 들어가 그 기사는 어느 행에도
        본문이 남지 않는다(배포 전 행이 50건 피드에 남아 있는 동안 계속 생긴다). 행 수가 입력보다 적을 수 있다.
      · 요약이 비었거나 is_drop_article이 참이면(짧음·[포토]) 본문 ''에 'dropped'로 닫는다. 본문을 따로 요청하지 않는다 -
        이 출발지는 본문 요청을 거절한다(ADR 0026 증거 10).
      · 같은 요약이 이미 'ok'/'rss_summary' 행에 있거나 이번 묶음에서 앞서 나왔으면 본문 ''에 'duplicate'로 넣는다
        (임베딩·클러스터에 같은 글이 두 번 들어가지 않게 한다 - 본문 추출기의 중복 처리와 같다).
    상태 맵은 요약 피드의 항목만 갖는다(실제로 들어간 행의 집계에 쓴다).
    """
    if strategy != RSS_SUMMARY:
        return [(pid, title, '', link, dt, now_local, None, None, None, None) for link, title, dt, _ in entries], {}

    present = _existing_urls(cur, [link for link, _, _, _ in entries]) if entries else set()
    entries = [entry for entry in entries if entry[0] not in present]

    verdicts = []  # (drop 사유 있음, 해시) - 항목 순서대로
    for link, title, dt, summary in entries:
        dropped = bool(summary_drop_reasons(summary, title, press_name))
        verdicts.append((dropped, None if dropped else _summary_digest(summary)))
    digests = {digest for dropped, digest in verdicts if digest}
    taken = _existing_summary_hashes(cur, digests) if digests else set()

    rows, statuses = [], {}
    for (link, title, dt, summary), (dropped, digest) in zip(entries, verdicts):
        if dropped:
            status, content, row_digest, length = SUMMARY_DROPPED_STATUS, '', None, 0
        elif digest in taken:
            status, content, row_digest, length = SUMMARY_DUPLICATE_STATUS, '', digest, 0
        else:
            status, content, row_digest, length = RSS_SUMMARY_STATUS, summary, digest, len(summary)
            taken.add(digest)
        rows.append((pid, title, content, link, dt, now_local, status, row_digest, length, now_utc))
        statuses.setdefault(link, status)  # 같은 링크가 두 번 나오면 먼저 들어가는 행의 상태 - ON CONFLICT가 뒤의 행을 건너뛴다
    return rows, statuses


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
            for feed_name, (strategy, url) in Settings.RSS_FEEDS.items():
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
                                summary = rss_summary_text(e) if strategy == RSS_SUMMARY else ''
                                entries.append((e.link, e.title, dt, summary)) # cutoff 시간 이후의 기사만 추가
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
                    #    'rss_summary' 피드는 요약을 본문 칸에 넣어 본문 추출 대상에서 빼고, 요약이 짧거나 이미 있으면
                    #    'dropped'/'duplicate'로 넣는다(build_insert_rows).
                    candidates, summary_status = build_insert_rows(
                        cur, pid, press_name, strategy, entries, datetime.now(), datetime.now(timezone.utc))
                    inserted_rows = execute_values(
                        cur,
                        """
                        INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, raw_news_crawled_at,
                                              raw_news_extract_status, raw_news_content_sha256, raw_news_content_length, raw_news_extracted_at)
                        VALUES %s
                        ON CONFLICT (raw_news_url) DO NOTHING
                        RETURNING raw_news_url
                        """,
                        candidates,
                        fetch=True,
                    ) if candidates else []
                    inserted_count = len(inserted_rows)
                    # 건너뜀 = 피드 항목 가운데 새로 들어가지 않은 것(이미 있어 build_insert_rows가 뺀 것 포함)
                    skipped_count = len(entries) - inserted_count
                    feed_stats["inserted"] = inserted_count
                    feed_stats["skipped"] = skipped_count
                    if strategy == RSS_SUMMARY:
                        # 이번에 실제로 들어간 요약 행의 상태별 건수(이미 있어 건너뛴 행은 세지 않는다).
                        # 피드가 요약을 비워 보내기 시작하거나 짧은 요약이 많아지면 여기서 드러난다.
                        by_status = Counter(summary_status.get(row[0]) for row in inserted_rows)
                        feed_stats["summary"] = {
                            "stored": by_status[RSS_SUMMARY_STATUS],
                            "dropped": by_status[SUMMARY_DROPPED_STATUS],
                            "duplicate": by_status[SUMMARY_DUPLICATE_STATUS],
                        }
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
