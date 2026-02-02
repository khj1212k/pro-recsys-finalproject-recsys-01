"""RSS 수집기 (Diet Version)"""
import feedparser, logging
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
from db.connection import get_connection
from config.settings import Settings

logger = logging.getLogger(__name__)

def collect_rss(hours: int = 100) -> int:
    """RSS 피드 수집 실행"""
    conn = get_connection()
    total_new = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    logger.info(f"RSS 수집 시작 (Cutoff: {hours}h)")

    try:
        with conn.cursor() as cur:
            for press_name, (_, url) in Settings.RSS_FEEDS.items():
                try:
                    press_name = '전자신문' if press_name.startswith('전자신문') else press_name
                    cur.execute("SELECT press_id \
                                       FROM press \
                                       WHERE press_name=%s", 
                                       (press_name,))
                    row = cur.fetchone()
                    if not row:
                        logger.info(f"ℹ️ {press_name} press_id 없음 - 스킵")
                        continue
                    pid = row[0]
                    feed = feedparser.parse(url) # rss XML 테그 파싱
                    entries = [] # 수집된 기사 목록
                    
                    # 1. 파싱 및 날짜 필터링
                    for e in feed.entries:
                        try:
                            dt_str = e.get('published') or e.get('updated')
                            dt = date_parser.parse(dt_str) # str -> datetime object
                            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc) # timezone이 없으면 UTC로 변환
                            if dt >= cutoff: entries.append((e.link, e.title, dt)) # cutoff 시간 이후의 기사만 추가
                        except: 
                            continue

                    if not entries: 
                        continue

                    # 2. 링크 기준 중복 제거 
                    links = tuple(x[0] for x in entries) 
                    cur.execute("SELECT raw_news_url \
                                 FROM news_raw \
                                 WHERE raw_news_url IN %s", (links,))
                    existing = {r[0] for r in cur.fetchall()}
                    
                    # 3. 신규 기사만 필터링
                    new_items = [
                        (pid, title, '', link, dt, datetime.now()) 
                        for link, title, dt in entries if link not in existing
                    ]

                    # 4. DB에 신규 기사 추가
                    if new_items:
                        cur.executemany("""
                            INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, raw_news_crawled_at) 
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """, new_items)
                        total_new += len(new_items)
                        logger.info(f"✅ {press_name}: {len(new_items)}건 추가")
                        
                except Exception as e:
                    logger.info(f"ℹ️ {press_name} 처리 중 오류: {e}")
        conn.commit() # 변경사항 저장
    except Exception as e:
        conn.rollback() # 오류시 롤백
        logger.info(f"ℹ️ RSS 수집 중 오류: {e}")
    finally:
        conn.close() # 연결 종료

    logger.info(f"✨ 총 {total_new}건 수집 완료")
    return total_new # 수집된 기사 수 반환

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collect_rss()
