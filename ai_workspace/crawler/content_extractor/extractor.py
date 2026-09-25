# Stage2: 기사 본문 추출
# - RSS에서 수집한 URL로 실제 기사 내용 크롤링
# - trafilatura로 HTML에서 본문 텍스트 추출
# - 멀티프로세싱으로 병렬 처리

import time
import trafilatura
from multiprocessing import Pool
from tqdm import tqdm
from db.connection import get_connection, release_connection
from config.settings import Settings
from .cleaners import clean_text_lite, is_drop_article


def fetch_url_with_retry(url, fetch_fn=None, max_attempts=None, sleep_fn=time.sleep):
    """trafilatura.fetch_url을 지수 백오프로 재시도한다.

    trafilatura는 네트워크 실패 시 예외를 던지기도 하고 단순히 None을 반환하기도
    하므로 두 경우 모두 재시도 대상으로 취급한다. fetch_fn/sleep_fn은 테스트에서
    실제 네트워크 호출/대기 없이 검증하기 위해 주입 가능하게 열어둔다.
    """
    fetch_fn = fetch_fn or trafilatura.fetch_url
    max_attempts = max_attempts or Settings.MAX_FETCH_RETRIES
    delay = 1.0
    downloaded = None
    for attempt in range(max_attempts):
        try:
            downloaded = fetch_fn(url)
        except Exception:
            downloaded = None
        if downloaded:
            return downloaded
        if attempt < max_attempts - 1:
            sleep_fn(delay)
            delay = min(delay * Settings.RETRY_EXPONENTIAL_BASE, Settings.MAX_RETRY_WAIT_SECONDS)
    return downloaded


class ContentExtractor:
    # 기사 본문 추출기 

    @staticmethod
    def get_strategy_for_press(press_name: str) -> str:
        for source, (strategy, _) in Settings.RSS_FEEDS.items():
            if press_name == '전자신문':
                if source.startswith('전자신문'):
                    return strategy
            elif source == press_name:
                return strategy
        return 'direct'

    @staticmethod
    def process_single_article(article_data):
        # 개별 기사 처리 (병렬 처리)

        raw_news_id, url, press_name, title = article_data
        conn = get_connection()
        cur = conn.cursor()

        try:
            # 전략 조회
            _ = ContentExtractor.get_strategy_for_press(press_name)

            text = None
            downloaded = fetch_url_with_retry(url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=False
                )

            # 정제 및 저장
            if text and len(text.strip()) > 0:
                cleaned = clean_text_lite(text, press_name=press_name)

                # 저품질/광고성 기사 필터링 (짧은 본문, [포토] 제목, 리스트성 마커 등)
                should_drop, drop_reasons = is_drop_article(cleaned, title, press_name)
                if should_drop:
                    cur.execute("""
                        UPDATE news_raw
                        SET raw_news_content = ''
                        WHERE raw_news_id = %s
                    """, (raw_news_id,))
                    conn.commit()
                    return f"🚫 {press_name} (dropped: {','.join(drop_reasons)})"

                cur.execute("""
                    UPDATE news_raw
                    SET raw_news_content = %s
                    WHERE raw_news_id = %s
                """, (cleaned, raw_news_id))
                conn.commit()

                return f"✅ {press_name} (raw={len(text)} → clean={len(cleaned)})"

            else:
                # 추출된 내용이 아예 없는 경우에만 빈 값 처리
                cur.execute("""
                    UPDATE news_raw
                    SET raw_news_content = ''
                    WHERE raw_news_id = %s
                """, (raw_news_id,))
                conn.commit()
                return f"⚪ {press_name} (empty content)"

        except Exception as e:
            conn.rollback()
            return f"❌ {press_name} 에러: {str(e)[:50]}"

        finally:
            release_connection(conn)

    def extract_parallel(self, num_workers=None):
        # 병렬 본문 추출 실행
        if num_workers is None:
            num_workers = Settings.PARALLEL_WORKERS

        # 1. 대상 조회 - 아직 본문이 추출되지 않은 기사
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT N.raw_news_id, N.raw_news_url, P.press_name, N.raw_news_title
            FROM news_raw N
            JOIN press P ON N.press_id = P.press_id
            WHERE N.raw_news_content IS NULL OR N.raw_news_content = ''
        """)
        articles = cur.fetchall()
        release_connection(conn)

        if not articles:
            print("💤 수집할 기사가 없습니다.")
            return 0

        print(f"🚀 병렬 수집 시작! (대상: {len(articles)}건, 워커: {num_workers}명)")

        # 2. 병렬 처리
        success_count = 0
        empty_count = 0
        dropped_count = 0
        error_count = 0

        with Pool(processes=num_workers) as pool:
            for result in tqdm(pool.imap_unordered(
                self.process_single_article, articles
            ), total=len(articles), desc="🚀 본문 추출 중"):
                if "✅" in result:
                    success_count += 1
                elif "⚪" in result:
                    empty_count += 1
                elif "🚫" in result:
                    dropped_count += 1
                else:
                    error_count += 1
                    if error_count <= 5:
                        print(f"   [Error] {result}")

        print(f"🏁 전체 작업 종료. 성공: {success_count}, 내용없음: {empty_count}, 필터링: {dropped_count}, 에러: {error_count}")
        return success_count
