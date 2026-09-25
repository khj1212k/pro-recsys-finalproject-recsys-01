# Stage2: 기사 본문 추출
# - RSS에서 수집한 URL로 실제 기사 내용 크롤링
# - trafilatura로 HTML에서 본문 텍스트 추출
# - 멀티프로세싱으로 병렬 처리

import trafilatura
from multiprocessing import Pool
from tqdm import tqdm
from db.connection import get_connection, release_connection
from config.settings import Settings
from .cleaners import clean_text_lite  

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

        raw_news_id, url, press_name = article_data
        conn = get_connection()
        cur = conn.cursor()

        try:
            # 전략 조회
            _ = ContentExtractor.get_strategy_for_press(press_name)

            text = None
            downloaded = trafilatura.fetch_url(url)
            if downloaded:
                text = trafilatura.extract(
                    downloaded,
                    include_comments=False,
                    include_tables=False
                )

            # 정제 및 저장
            if text and len(text.strip()) > 0:
                cleaned = clean_text_lite(text, press_name=press_name)

                
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
            SELECT N.raw_news_id, N.raw_news_url, P.press_name
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
        error_count = 0

        with Pool(processes=num_workers) as pool:
            for result in tqdm(pool.imap_unordered(
                self.process_single_article, articles
            ), total=len(articles), desc="🚀 본문 추출 중"):
                if "✅" in result:
                    success_count += 1
                elif "⚪" in result:
                    empty_count += 1
                else:
                    error_count += 1
                    if error_count <= 5:
                        print(f"   [Error] {result}")

        print(f"🏁 전체 작업 종료. 성공: {success_count}, 내용없음: {empty_count}, 에러: {error_count}")
        return success_count
