# Stage2: 기사 본문 추출
# - RSS에서 수집한 URL로 실제 기사 내용 크롤링
# - trafilatura로 HTML에서 본문 텍스트 추출
# - 멀티프로세싱으로 병렬 처리

import logging
import multiprocessing
import time
from collections import Counter
from typing import Any, Dict, NamedTuple, Tuple

import trafilatura
from tqdm import tqdm
from db.connection import get_connection, release_connection
from config.settings import Settings
from .cleaners import clean_text_lite, is_drop_article

logger = logging.getLogger(__name__)


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


class ExtractionResult(NamedTuple):
    raw_news_id: int
    press_name: str
    status: str  # ok | dropped | empty | fetch_failed | error
    reasons: Tuple[str, ...] = ()
    detail: str = ""


# raw_news_extract_status로 기록하는 값(f87f7378672e의 CHECK 제약과 동일). 'error'는
# DB 오류 등으로 결과를 기록하지 못한 경우라 상태를 남기지 않고 다음 실행에서 다시 시도한다.
_RECORDED_STATUSES = ("ok", "dropped", "empty", "fetch_failed")


def _record_extraction(cur, raw_news_id, content, status):
    cur.execute("""
        UPDATE news_raw
        SET raw_news_content = %s,
            raw_news_extract_status = %s,
            raw_news_extracted_at = now(),
            raw_news_extract_attempts = raw_news_extract_attempts + 1
        WHERE raw_news_id = %s
    """, (content, status, raw_news_id))


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
    def process_single_article(article_data) -> ExtractionResult:
        # 개별 기사 처리 (병렬 처리)

        raw_news_id, url, press_name, title = article_data
        conn = get_connection()
        cur = conn.cursor()

        try:
            # 전략 조회
            _ = ContentExtractor.get_strategy_for_press(press_name)

            downloaded = fetch_url_with_retry(url)
            if not downloaded:
                # 재시도까지 실패 - 다음 실행에서 Settings.MAX_EXTRACT_ATTEMPTS회까지 다시 시도
                _record_extraction(cur, raw_news_id, '', 'fetch_failed')
                conn.commit()
                return ExtractionResult(raw_news_id, press_name, "fetch_failed")

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
                    _record_extraction(cur, raw_news_id, '', 'dropped')
                    conn.commit()
                    return ExtractionResult(raw_news_id, press_name, "dropped", tuple(drop_reasons))

                _record_extraction(cur, raw_news_id, cleaned, 'ok')
                conn.commit()
                return ExtractionResult(
                    raw_news_id, press_name, "ok", detail=f"raw={len(text)} clean={len(cleaned)}"
                )

            # 페이지는 받았지만 본문을 뽑지 못함(목록/영상 페이지 등) - 재시도해도 같으므로 확정
            _record_extraction(cur, raw_news_id, '', 'empty')
            conn.commit()
            return ExtractionResult(raw_news_id, press_name, "empty")

        except Exception as e:
            conn.rollback()
            return ExtractionResult(raw_news_id, press_name, "error", detail=str(e)[:200])

        finally:
            release_connection(conn)

    def extract_parallel(self, num_workers=None):
        """하위 호환: 성공(본문 저장) 건수만 반환한다. 통계 전체는 extract_parallel_with_stats."""
        return self.extract_parallel_with_stats(num_workers)["ok"]

    def extract_parallel_with_stats(self, num_workers=None) -> Dict[str, Any]:
        if num_workers is None:
            num_workers = Settings.PARALLEL_WORKERS

        # 1. 대상 조회 - 아직 추출을 시도하지 않은 기사 + 다운로드 실패로 재시도 여지가 남은 기사.
        #    'dropped'/'empty'는 다시 받아도 결과가 같으므로 제외한다.
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT N.raw_news_id, N.raw_news_url, P.press_name, N.raw_news_title
                FROM news_raw N
                JOIN press P ON N.press_id = P.press_id
                WHERE N.raw_news_extract_status IS NULL
                   OR (N.raw_news_extract_status = 'fetch_failed'
                       AND N.raw_news_extract_attempts < %s)
                ORDER BY N.raw_news_id
            """, (Settings.MAX_EXTRACT_ATTEMPTS,))
            articles = cur.fetchall()
        finally:
            release_connection(conn)

        stats: Dict[str, Any] = {
            "targets": len(articles), "workers": num_workers,
            **{status: 0 for status in (*_RECORDED_STATUSES, "error")},
            "drop_reasons": {}, "per_press": {}, "error_samples": [],
        }
        if not articles:
            logger.info("💤 본문을 추출할 기사가 없습니다.")
            return stats

        logger.info(f"🚀 병렬 본문 추출 시작 (대상: {len(articles)}건, 워커: {num_workers})")

        # 2. 병렬 처리. fork(리눅스 기본값)면 부모의 커넥션 풀과 열린 소켓이 자식에
        #    복제돼 여러 워커가 같은 DB 소켓을 동시에 쓰게 된다 - spawn으로 자식마다
        #    새 풀을 만든다(macOS는 원래 spawn이 기본값이라 로컬에서는 드러나지 않았음).
        drop_reasons: Counter = Counter()

        def _collect(results):
            for result in tqdm(results, total=len(articles), desc="🚀 본문 추출 중"):
                stats[result.status] += 1
                press = stats["per_press"].setdefault(result.press_name, {})
                press[result.status] = press.get(result.status, 0) + 1
                drop_reasons.update(result.reasons)
                if result.status == "error" and len(stats["error_samples"]) < 5:
                    stats["error_samples"].append(result.detail)
                    logger.error(f"   [Error] {result.press_name}: {result.detail}")

        if num_workers <= 1:
            _collect(map(self.process_single_article, articles))
        else:
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(processes=num_workers) as pool:
                _collect(pool.imap_unordered(self.process_single_article, articles))

        stats["drop_reasons"] = dict(drop_reasons)
        logger.info(
            f"🏁 본문 추출 종료. 성공: {stats['ok']}, 필터링: {stats['dropped']}, 내용없음: {stats['empty']}, "
            f"다운로드 실패: {stats['fetch_failed']}, 에러: {stats['error']}"
        )
        return stats
