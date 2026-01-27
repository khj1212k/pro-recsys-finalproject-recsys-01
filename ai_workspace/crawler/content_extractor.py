"""
본문 추출기
뉴스 URL에서 본문을 추출하여 news_raw 테이블 업데이트
+ 약한 정제(lite) + DROP 마킹
"""
import re
import time
import trafilatura
from multiprocessing import Pool
from db.connection import get_connection
from config.settings import Settings

# Selenium imports (optional)
try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False


# =========================
# 정제 설정
# =========================
DROP_LEN = 350          # clean 길이 이 값 미만이면 DROP
DROP_PHOTO = True       # [포토] 제목이면 DROP
DROP_LIST = True        # 추천기사/에디터픽 리스트성이면 DROP

# =========================
# 정제용 정규식 & 마커
# =========================
_MULTI_SPACE_RE = re.compile(r"\s+")

_END_MARKERS = [
    "댓글을 입력해 주세요",
    "관련기사",
    "추천기사",
    "많이 본 기사",
    "저작권자",
    "무단전재",
    "재배포 금지",
    "AI학습 및 활용 금지",
    "모바일버전",
]

# 동아일보: UI 블록
_DONGA_START_MARKERS = ["본문으로 바로가기"]
_DONGA_CUT_AFTER = ["프린트", "글자크기 설정"]

# 한국경제: 고정 UI 블록
_HK_UI_BLOCK = " - 기사 스크랩 - 공유 - 댓글 - 클린뷰 - 프린트"

# AI타임스: 고정 네비 헤더
_AITIMES_HEADER_MARKERS = ["주요서비스 바로가기", "본문 바로가기", "매체정보 바로가기", "기사검색 바로가기"]

# DROP 판정용
_RE_PHOTO_TITLE = re.compile(r"\[\s*포토\s*\]", re.IGNORECASE)
_LIST_MARKERS = ["추천기사", "에디터 픽", "에디터픽", "Editor's Pick", "추천 기사", "많이 본 기사", "실시간", "랭킹"]


def _cut_end(text: str) -> str:
    """댓글/저작권/모바일버전 이후를 잘라냄"""
    if not text:
        return ""
    idxs = [text.find(m) for m in _END_MARKERS if text.find(m) != -1]
    if idxs:
        cut = min(idxs)
        if cut >= 200:
            return text[:cut].rstrip()
    return text


def _normalize_spaces(t: str) -> str:
    """공백/개행 정리"""
    return _MULTI_SPACE_RE.sub(" ", (t or "")).strip()


def clean_text_lite(raw: str, press_name: str = "") -> str:
    """
    ✅ 임베딩용 1차 약한 정제
    - 언론사별 고정 UI 블록 제거
    - 공통 END 컷 (저작권/댓글/추천기사 등)
    - 공백 정리만
    - URL/이메일/전화 제거 X (정보 보존)
    """
    if not raw:
        return ""

    t = raw.strip()
    pn = (press_name or "").strip()

    if pn == "동아일보":
        for m in _DONGA_START_MARKERS:
            idx = t.find(m)
            if idx != -1 and idx < 2000:
                t = t[idx + len(m):].lstrip()
                break
        last = -1
        for m in _DONGA_CUT_AFTER:
            j = t.find(m)
            if j != -1:
                last = max(last, j + len(m))
        if last != -1 and last < 1500:
            t = t[last:].lstrip()

    elif pn == "한국경제":
        k = t.find(_HK_UI_BLOCK)
        if k != -1 and k < 2000:
            t = t[k + len(_HK_UI_BLOCK):].lstrip()
        else:
            m = re.search(r"\s-\s기사\s스크랩\s-\s공유\s-\s댓글\s-\s클린뷰\s-\s프린트\s", t)
            if m and m.start() < 2000:
                t = t[m.end():].lstrip()

    elif pn == "AI타임스":
        last = -1
        for m in _AITIMES_HEADER_MARKERS:
            j = t.find(m)
            if j != -1:
                last = max(last, j + len(m))
        if last != -1 and last < 2500:
            t = t[last:].lstrip()
        j = t.find("발행일:")
        if j != -1 and j < 2500:
            t = t[j:].lstrip()

    t = _cut_end(t)
    return _normalize_spaces(t)


def is_drop_article(cleaned: str, title: str) -> tuple:
    """
    DROP 판정
    Returns: (is_drop: bool, reasons: list[str])
    """
    reasons = []
    t = cleaned or ""
    ttl = title or ""

    # 1) 포토 기사
    if DROP_PHOTO and _RE_PHOTO_TITLE.search(ttl):
        reasons.append("photo_title")

    # 2) 너무 짧음
    if len(t.strip()) < DROP_LEN:
        reasons.append(f"too_short<{DROP_LEN}")

    # 3) 추천기사/에디터픽 등 리스트성 본문
    if DROP_LIST:
        hit = any(m in t for m in _LIST_MARKERS)
        if hit and len(t.strip()) < max(DROP_LEN * 2, 700):
            reasons.append("list_like_markers")

    return (len(reasons) > 0), reasons


class ContentExtractor:
    """기사 본문 추출기 (news_raw 테이블 사용)"""

    @staticmethod
    def init_driver():
        """Selenium 브라우저 초기화"""
        if not SELENIUM_AVAILABLE:
            raise ImportError("selenium not available. Install: pip install selenium webdriver-manager")
        
        chrome_options = Options()
        chrome_options.page_load_strategy = 'eager'
        prefs = {"profile.managed_default_content_settings.images": 2}
        chrome_options.add_experimental_option("prefs", prefs)
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_argument(f"user-agent={Settings.USER_AGENT}")

        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=chrome_options)

    @staticmethod
    def get_strategy_for_press(press_name: str) -> str:
        """
        언론사명으로 크롤링 전략(strategy) 조회
        전자신문_* 통합 처리
        """
        # RSS_FEEDS에서 strategy 찾기
        for source, (strategy, _) in Settings.RSS_FEEDS.items():
            # 전자신문 통합 처리
            if press_name == '전자신문':
                if source.startswith('전자신문'):
                    return strategy
            elif source == press_name:
                return strategy

        # 기본값: direct
        return 'direct'

    @staticmethod
    def process_single_article(article_data):
        """
        개별 기사 처리 (병렬 처리용)

        Args:
            article_data: (raw_news_id, url, press_name)

        Returns:
            str: 처리 결과 메시지
        """
        raw_news_id, url, press_name = article_data

        conn = get_connection()
        cur = conn.cursor()
        driver = None

        try:
            # 언론사명으로 strategy 조회
            strategy = ContentExtractor.get_strategy_for_press(press_name)
            text = None

            # [A] Selenium 방식
            if strategy == 'selenium' and SELENIUM_AVAILABLE:
                driver = ContentExtractor.init_driver()
                driver.set_page_load_timeout(Settings.SELENIUM_PAGE_LOAD_TIMEOUT)
                try:
                    driver.get(url)
                    time.sleep(2)
                    text = trafilatura.extract(
                        driver.page_source,
                        include_comments=False,
                        include_tables=False
                    )
                except Exception:
                    text = None
                finally:
                    if driver:
                        driver.quit()

            # [B] Direct 방식 (기본)
            else:
                downloaded = trafilatura.fetch_url(url)
                if downloaded:
                    text = trafilatura.extract(
                        downloaded,
                        include_comments=False,
                        include_tables=False
                    )

            # [정제 + DROP 판정 + 저장]
            if text and len(text.strip()) > 0:
                # 1) 약한 정제 적용
                cleaned = clean_text_lite(text, press_name=press_name)

                # 2) 제목 조회 (DROP 판정용)
                cur.execute("SELECT raw_news_title FROM news_raw WHERE raw_news_id = %s", (raw_news_id,))
                title_row = cur.fetchone()
                title = title_row[0] if title_row else ""

                # 3) DROP 판정
                is_drop, reasons = is_drop_article(cleaned, title)

                # 4) 저장 (정제된 텍스트 + DROP 마킹)
                if is_drop:
                    cur.execute("""
                        UPDATE news_raw
                        SET raw_news_content = %s, news_letter_id = -1
                        WHERE raw_news_id = %s
                    """, (cleaned, raw_news_id))
                    conn.commit()
                    return f"🚫 {press_name} DROP ({','.join(reasons)})"
                else:
                    cur.execute("""
                        UPDATE news_raw
                        SET raw_news_content = %s
                        WHERE raw_news_id = %s
                    """, (cleaned, raw_news_id))
                    conn.commit()
                    return f"✅ {press_name} (raw={len(text)} → clean={len(cleaned)})"
            else:
                # 빈 내용 → DROP 마킹
                cur.execute("""
                    UPDATE news_raw
                    SET raw_news_content = '', news_letter_id = -1
                    WHERE raw_news_id = %s
                """, (raw_news_id,))
                conn.commit()
                return f"🚫 {press_name} DROP (empty)"

        except Exception as e:
            conn.rollback()
            return f"❌ {press_name} 에러: {str(e)[:50]}"

        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
            conn.close()

    def extract_parallel(self, num_workers=None):
        """병렬 본문 추출 (news_raw 테이블 사용)"""
        if num_workers is None:
            num_workers = Settings.PARALLEL_WORKERS

        # 1. 대상 조회 (본문이 없는 기사들)
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT N.raw_news_id, N.raw_news_url, P.press_name
            FROM news_raw N
            JOIN press P ON N.press_id = P.press_id
            WHERE N.raw_news_content IS NULL OR N.raw_news_content = ''
        """)
        articles = cur.fetchall()
        conn.close()

        if not articles:
            print("💤 수집할 기사가 없습니다.")
            return 0

        print(f"🚀 병렬 수집 시작! (대상: {len(articles)}건, 워커: {num_workers}명)")

        # 2. 병렬 처리
        from tqdm import tqdm
        
        success_count = 0
        drop_count = 0
        error_count = 0
        
        with Pool(processes=num_workers) as pool:
            # tqdm으로 진행률 표시
            for result in tqdm(pool.imap_unordered(
                self.process_single_article, articles
            ), total=len(articles), desc="🚀 본문 추출 중"):
                if "✅" in result:
                    success_count += 1
                elif "🚫" in result:
                    drop_count += 1
                else:
                    error_count += 1
                    
        print(f"🏁 전체 작업 종료. 성공: {success_count}, DROP: {drop_count}, 에러: {error_count}")
        return success_count


if __name__ == "__main__":
    extractor = ContentExtractor()
    extractor.extract_parallel()
