"""
RSS 수집기
RSS 피드에서 뉴스 기사를 수집하여 news_raw 테이블에 저장
"""
import feedparser
import requests
import urllib3
from datetime import datetime, timedelta, timezone
from dateutil import parser as date_parser
from db.connection import get_connection
from config.settings import Settings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class RssCollector:
    """RSS 피드 수집 (news_raw 테이블로 저장)"""

    def __init__(self):
        self.session = requests.Session()
        self.headers = {"User-Agent": Settings.USER_AGENT}
        self.press_id_cache = {}  # 언론사 ID 캐시

    def get_press_id(self, source_name: str) -> int:
        """
        언론사 이름으로 press_id 조회 (캐싱)
        전자신문_IT, 전자신문_AI, 전자신문_과학 -> 전자신문으로 통합
        """
        # 전자신문 통합 처리
        if source_name.startswith('전자신문'):
            source_name = '전자신문'

        # 캐시 확인
        if source_name in self.press_id_cache:
            return self.press_id_cache[source_name]

        # DB 조회
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT press_id FROM press WHERE press_name = %s", (source_name,))
        result = cur.fetchone()
        
        if result:
            press_id = result[0]
            self.press_id_cache[source_name] = press_id
            conn.close()
            return press_id
        
        # 언론사가 없으면 생성
        cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (source_name,))
        result = cur.fetchone()
        conn.commit()
        conn.close()
        
        if result:
            press_id = result[0]
            self.press_id_cache[source_name] = press_id
            return press_id
        raise ValueError(f"언론사 '{source_name}'을(를) 생성할 수 없습니다.")

    def collect_rss(self, hours: int = 100):
        """
        RSS 수집 (news_raw 테이블로 직접 저장)
        
        Args:
            hours: 최근 N시간 이내 기사만 수집 (기본: 100시간)
        """
        conn = get_connection()
        cur = conn.cursor()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        print(f"=== ⚔️ 뉴스 수집기 가동 ({datetime.now().strftime('%H:%M:%S')}) ===")
        print(f"    최근 {hours}시간 이내 기사 수집")

        total_collected = 0
        
        for source, (strategy, url) in Settings.RSS_FEEDS.items():
            print(f"📡 {source} 검색...", end="")
            try:
                # press_id 조회
                press_id = self.get_press_id(source)

                response = self.session.get(
                    url,
                    headers=self.headers,
                    timeout=Settings.REQUEST_TIMEOUT,
                    verify=Settings.SSL_VERIFY
                )
                feed = feedparser.parse(response.content)

                count = 0
                for entry in feed.entries:
                    link = entry.link
                    title = entry.title
                    raw_date = entry.get('published') or entry.get('updated') or None

                    # 날짜가 없으면 스킵
                    if not raw_date:
                        continue

                    try:
                        parsed_date = date_parser.parse(raw_date)
                    except:
                        continue  # 파싱 실패 시 스킵

                    # tzinfo 없으면 UTC로 간주
                    if parsed_date.tzinfo is None:
                        parsed_date = parsed_date.replace(tzinfo=timezone.utc)

                    # cutoff 이전이면 스킵
                    if parsed_date < cutoff:
                        continue

                    # URL 중복 체크
                    cur.execute("SELECT raw_news_id FROM news_raw WHERE raw_news_url = %s", (link,))
                    if cur.fetchone():
                        continue  # 이미 존재하면 스킵

                    # news_raw 테이블에 삽입 (raw_news_content는 빈 문자열로 초기화)
                    cur.execute("""
                        INSERT INTO news_raw (
                            press_id, raw_news_title, raw_news_content, raw_news_url,
                            raw_news_created_at, raw_news_crawled_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING raw_news_id;
                    """, (press_id, title, '', link, raw_date, datetime.now()))

                    result = cur.fetchone()
                    if result:
                        count += 1

                conn.commit()
                total_collected += count
                print(f" ✅ {count}건")
            except Exception as e:
                print(f" ❌ 에러: {e}")
                conn.rollback()

        conn.close()
        print(f"✨ RSS 수집 완료. 총 {total_collected}건 신규 수집\n")
        return total_collected


if __name__ == "__main__":
    collector = RssCollector()
    collector.collect_rss()
