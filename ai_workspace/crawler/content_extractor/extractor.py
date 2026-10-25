# Stage2: 기사 본문 추출
# - RSS에서 수집한 URL로 실제 기사 내용 크롤링
# - 요청마다 시간 상한을 둔 다운로드(fetch.py) + trafilatura로 HTML에서 본문 텍스트 추출
# - 멀티프로세싱으로 병렬 처리. 어느 기사를 언제 보낼지는 부모가 정한다(언론사별 차단기·실행 시간 예산)

import hashlib
import logging
import multiprocessing
import time
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from typing import Any, Callable, Deque, Dict, Iterable, List, NamedTuple, Optional, Tuple

import psycopg2
import trafilatura
from tqdm import tqdm
from db.connection import get_connection, release_connection
from config.retention import BODY_RETENTION_DAYS, RETRYABLE_EXTRACT_STATUSES
from config.settings import Settings
from .body_container import FULL_PAGE, select_body_html
from .cleaners import clean_text_lite, is_drop_article
from .fetch import fetch_page, is_site_failure, latency_bucket
from .page_pubdate import CREATED_AT_FIXED, FUTURE_SLACK_MINUTES, NOT_FUTURE, PageCreatedAt, decide_created_at

logger = logging.getLogger(__name__)


class ExtractionResult(NamedTuple):
    raw_news_id: int
    press_name: str
    status: str  # ok | dropped | empty | fetch_failed | error | duplicate (DB에 남는 값) | deferred (부모만 씀)
    reasons: Tuple[str, ...] = ()
    detail: str = ""
    fetch_cause: str = ""  # fetch_failed일 때 마지막 요청의 실패 원인(fetch.py) - 개수만 stats에 남긴다
    fetch_s: Optional[float] = None  # 다운로드에 성공했을 때 그 요청 1번에 걸린 시간 - 구간별 개수만 stats에 남긴다
    # 본문 컨테이너 규칙이 있는 언론사(body_container.py)만 채운다: "container"(본문 노드만 잘라 추출) 또는
    # "container_missing"(노드를 못 찾아 전체 페이지로 되돌아감). 규칙이 없는 언론사는 "" - 개수만 stats에 남긴다
    container: str = ""
    # 저장된 발행 시각이 수집 시각보다 10분 넘게 뒤(미래)였던 행의 처리(page_pubdate.py): "fixed"(기사 페이지의 발행 시각으로
    # 바꿨다) 또는 못 바꾼 사유(no_page_time·no_tz·after_crawl·too_old). 미래가 아니었던 행은 "" - 건수만 stats에 남긴다
    created_at_fix: str = NOT_FUTURE


# raw_news_extract_status 값(d48994e9d26e의 CHECK 제약과 동일).
_RECORDED_STATUSES = ("ok", "dropped", "empty", "fetch_failed", "error", "duplicate")
# 이번 실행에서 보내지 않은 기사. DB에 아무것도 쓰지 않으므로(시도 횟수도 그대로) 다음 실행에서 다시 대상이 된다.
DEFERRED = "deferred"
DEFER_PRESS_BREAKER = "press_breaker"
DEFER_TIME_BUDGET = "time_budget"
# 다시 받으면 결과가 달라질 수 있는 상태 - Settings.MAX_EXTRACT_ATTEMPTS회까지만 다시 시도한다.
# 보존 잡(jobs/tasks/retention.py)의 "추출 대기" 조건과 같아야 해서 가벼운 config.retention에 두고 함께 쓴다.
RETRYABLE_STATUSES = RETRYABLE_EXTRACT_STATUSES


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_extraction(cur, raw_news_id, content, status, digest=None):
    # 본문 길이(글자 수)는 해시처럼 본문을 쓸 때 함께 쓴다 - 보존 기한(ADR 0023)이 지나 본문을 비워도 남는다.
    cur.execute("""
        UPDATE news_raw
        SET raw_news_content = %s,
            raw_news_extract_status = %s,
            raw_news_content_sha256 = %s,
            raw_news_content_length = %s,
            raw_news_extracted_at = now(),
            raw_news_extract_attempts = raw_news_extract_attempts + 1
        WHERE raw_news_id = %s
    """, (content, status, digest, len(content), raw_news_id))


def _record_page_created_at(cur, raw_news_id, fix: PageCreatedAt) -> str:
    """미래였던 발행 시각을 기사 페이지 값으로 바꾼다. 추출 결과를 쓴 UPDATE와 같은 트랜잭션에서 호출하고 커밋은 부른 쪽이 한다.

    WHERE의 조건은 지금 행의 값이 아직 미래일 때만 바꾼다 - 대상을 고른 뒤에 다른 곳(런북 6.10의 보정)이 이미 고친 값을
    덮어쓰지 않는다. 돌려주는 값은 ExtractionResult.created_at_fix에 남길 분류다(행이 이미 고쳐졌으면 "")."""
    if fix.outcome != CREATED_AT_FIXED:
        return fix.outcome
    cur.execute("""
        UPDATE news_raw
        SET raw_news_created_at = %s
        WHERE raw_news_id = %s
          AND raw_news_created_at > (raw_news_crawled_at AT TIME ZONE 'UTC') + make_interval(mins => %s)
    """, (fix.value, raw_news_id, FUTURE_SLACK_MINUTES))
    return CREATED_AT_FIXED if cur.rowcount == 1 else NOT_FUTURE


def _record_ok_or_duplicate(conn, cur, raw_news_id, cleaned, fix: PageCreatedAt = PageCreatedAt(NOT_FUTURE)) -> Tuple[str, str]:
    """본문을 'ok'로 저장한다. 같은 본문이 이미 'ok'로 있으면(URL만 다른 같은 기사)
    uq_news_raw_content_sha256_ok가 막으므로 사본은 본문 없이 'duplicate'로 남긴다.
    병렬 워커가 같은 본문을 동시에 저장하는 경합도 인덱스가 한쪽만 통과시킨다.
    돌려주는 값은 (저장한 상태, 발행 시각 처리 분류). 발행 시각은 결과와 같은 트랜잭션에서 바꾼다 - 'ok'가 막혀 롤백하면
    함께 사라지므로 'duplicate'로 다시 쓸 때 발행 시각도 다시 쓴다."""
    digest = content_sha256(cleaned)
    try:
        _record_extraction(cur, raw_news_id, cleaned, "ok", digest)
        created_at_fix = _record_page_created_at(cur, raw_news_id, fix)
        conn.commit()
        return "ok", created_at_fix
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
    _record_extraction(cur, raw_news_id, "", "duplicate", digest)
    created_at_fix = _record_page_created_at(cur, raw_news_id, fix)
    conn.commit()
    return "duplicate", created_at_fix


class DispatchPlan:
    """어느 기사를 다음에 보낼지 정한다(부모 프로세스에서만 쓴다, ADR 0026 결정 9).

    - 언론사별 차단기: 같은 언론사 기사의 다운로드가 결과 도착 순서로 연달아 breaker_failures번 사이트 단위 원인
      (연결·읽기 실패, 5xx, 403·429 - fetch.is_site_failure)으로 실패하면, 그 언론사의 남은 기사는 이번 실행에서
      보내지 않는다. 성공(ok·dropped·empty·duplicate)이 오면 0으로 돌린다. 기사 단위 실패(404·410 같은 4xx, 크기·
      리다이렉트 상한)와 'error'(파서·DB 예외)는 세지도 되돌리지도 않는다 - 대상이 raw_news_id 순이라 앞서 실패한
      죽은 URL이 회차 맨 앞에 몰려 오는데, 그것으로 건강한 언론사의 새 기사를 미루면 안 된다.
    - 실행 시간 예산: deadline(clock 기준 시각)이 지나면 남은 기사를 하나도 보내지 않는다. 이미 보낸 기사는
      요청 상한(fetch.py) 안에서 끝난다.
    미룬 기사는 DB에 쓰지 않으므로 시도 횟수(MAX_EXTRACT_ATTEMPTS)를 쓰지 않고 다음 실행에서 다시 대상이 된다.
    """

    def __init__(self, articles: Iterable[tuple], *, breaker_failures: int,
                 deadline: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        self._queue: Deque[tuple] = deque(articles)
        self.breaker_failures = breaker_failures
        self.deadline = deadline
        self.clock = clock
        self.consecutive_failures: Counter = Counter()
        self.tripped: Dict[str, int] = {}  # 언론사 -> 차단기가 열린 시점의 연속 실패 수
        self.deferred: List[Tuple[tuple, str]] = []
        self.budget_exhausted = False

    def next_article(self) -> Optional[tuple]:
        while self._queue:
            article = self._queue.popleft()
            press = article[2]
            if self.deadline is not None and self.clock() >= self.deadline:
                self.budget_exhausted = True
                self.deferred.append((article, DEFER_TIME_BUDGET))
                continue
            if press in self.tripped:
                self.deferred.append((article, DEFER_PRESS_BREAKER))
                continue
            return article
        return None

    def record(self, result: ExtractionResult) -> None:
        press = result.press_name
        if result.status == "fetch_failed" and is_site_failure(result.fetch_cause):
            self.consecutive_failures[press] += 1
            if (self.breaker_failures > 0 and press not in self.tripped
                    and self.consecutive_failures[press] >= self.breaker_failures):
                self.tripped[press] = self.consecutive_failures[press]
                logger.warning(
                    f"   [Breaker] {press}: 다운로드 {self.consecutive_failures[press]}건 연속 실패 - "
                    "이번 실행의 남은 기사는 다음 실행으로 미룬다"
                )
        elif result.status not in ("fetch_failed", "error"):  # 기사 단위 실패·예외는 세지도 되돌리지도 않는다
            self.consecutive_failures[press] = 0


def _executor(num_workers: int) -> ProcessPoolExecutor:
    # fork(리눅스 기본값)면 부모의 커넥션 풀과 열린 소켓이 자식에 복제돼 여러 워커가 같은 DB 소켓을 동시에 쓰게
    # 된다 - spawn으로 자식마다 새 풀을 만든다(macOS는 원래 spawn이 기본값이라 로컬에서는 드러나지 않았음).
    return ProcessPoolExecutor(max_workers=num_workers, mp_context=multiprocessing.get_context("spawn"))


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

        # 대상 조회(extract_parallel_with_stats)는 발행 시각과 수집 시각을 더해 6개를 준다. 4개만 주면(예전 호출) 발행 시각은
        # 건드리지 않는다.
        raw_news_id, url, press_name, title, *times = article_data
        created_at, crawled_at = times[:2] if len(times) >= 2 else (None, None)
        conn = get_connection()
        cur = conn.cursor()

        try:
            # 전략 조회
            _ = ContentExtractor.get_strategy_for_press(press_name)

            fetched = fetch_page(url)
            if not fetched.html:
                # 이번 실행의 요청(fetch.py 상한)이 모두 실패 - 다음 실행에서 Settings.MAX_EXTRACT_ATTEMPTS회까지 다시 시도
                _record_extraction(cur, raw_news_id, '', 'fetch_failed')
                conn.commit()
                return ExtractionResult(raw_news_id, press_name, "fetch_failed", fetch_cause=fetched.cause)

            # 언론사별 본문 컨테이너 규칙이 있으면 본문 노드만 잘라 넘긴다. 전체 페이지를 넘기면 trafilatura가 본문 대신
            # 특집 카드 묶음을 고르는 사이트가 있다(국민일보 2026-10-19 개편, body_container.py). 규칙이 없는 언론사는
            # 페이지를 그대로 넘기므로 동작이 같다.
            selected = select_body_html(fetched.html, press_name)
            # 저장된 발행 시각이 수집 시각보다 10분 넘게 뒤(미래)인 행만 페이지의 발행 시각으로 바꾼다(page_pubdate.py).
            # 미래가 아닌 행은 페이지를 읽지도 않는다. 아래 추출 결과를 쓰는 트랜잭션에서 함께 쓴다.
            fix = decide_created_at(created_at, crawled_at, fetched.html)
            container = "" if selected.mode == FULL_PAGE else selected.mode
            text = trafilatura.extract(
                selected.html,
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
                    created_at_fix = _record_page_created_at(cur, raw_news_id, fix)
                    conn.commit()
                    return ExtractionResult(raw_news_id, press_name, "dropped", tuple(drop_reasons),
                                            fetch_s=fetched.request_s, container=container,
                                            created_at_fix=created_at_fix)

                status, created_at_fix = _record_ok_or_duplicate(conn, cur, raw_news_id, cleaned, fix)
                return ExtractionResult(
                    raw_news_id, press_name, status, detail=f"raw={len(text)} clean={len(cleaned)}",
                    fetch_s=fetched.request_s, container=container, created_at_fix=created_at_fix,
                )

            # 페이지는 받았지만 본문을 뽑지 못함(목록/영상 페이지 등) - 재시도해도 같으므로 확정
            _record_extraction(cur, raw_news_id, '', 'empty')
            created_at_fix = _record_page_created_at(cur, raw_news_id, fix)
            conn.commit()
            return ExtractionResult(raw_news_id, press_name, "empty", fetch_s=fetched.request_s, container=container,
                                    created_at_fix=created_at_fix)

        except Exception as e:
            conn.rollback()
            # 시도 횟수를 올려 둬야 같은 기사에서 매번 죽는 경우(파서 크래시 등) 상한 뒤 멈춘다.
            # DB 자체가 문제면 기록도 실패한다 - 그때는 상태 없이 다음 실행에서 다시 시도된다.
            try:
                _record_extraction(cur, raw_news_id, '', 'error')
                conn.commit()
            except Exception:
                conn.rollback()
            return ExtractionResult(raw_news_id, press_name, "error", detail=str(e)[:200])

        finally:
            release_connection(conn)

    def extract_parallel(self, num_workers=None):
        """하위 호환: 성공(본문 저장) 건수만 반환한다. 통계 전체는 extract_parallel_with_stats."""
        return self.extract_parallel_with_stats(num_workers)["ok"]

    def extract_parallel_with_stats(self, num_workers=None, time_budget_s: Optional[float] = None,
                                    clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
        """대상 기사의 본문을 받아 저장하고 통계를 돌려준다.

        time_budget_s: 이 시간(초)이 지나면 새 기사를 보내지 않고 남은 기사는 다음 실행으로 미룬다(None이면 제한 없음).
        ingest가 다음 정시 회차를 넘기지 않게 하려는 값이다 - 스케줄러(supercronic)는 앞 실행이 안 끝났으면
        다음 회차를 시작하지 않고 기록도 남기지 않는다(ADR 0026 증거 8).
        """
        if num_workers is None:
            num_workers = Settings.PARALLEL_WORKERS
        started = clock()

        # 1. 대상 조회 - 아직 추출을 시도하지 않은 기사 + 다운로드 실패/예외로 재시도 여지가 남은 기사.
        #    'dropped'/'empty'/'duplicate'는 다시 받아도 결과가 같으므로 제외한다.
        #    본문 보존 기한(ADR 0023, 수집 후 config.retention.BODY_RETENTION_DAYS = 30일)이 지난 기사와 본문을 비운
        #    기사는 내려받지 않는다 - 받으면 기한이 지난 본문을 새로 저장하게 된다. raw_news_crawled_at은 시간대 없는
        #    UTC 값이라 기준도 UTC로 만든다(jobs/tasks/retention.py와 같은 식). 기한은 설정(.env)이 아니라 ADR의
        #    상수를 쓴다 - 설정이 틀려도 기한이 지난 본문을 받지 않고, 수집을 멈추지도 않는다.
        #    보존 잡(jobs/tasks/retention.py)은 여기서 고르는 행 가운데 본문이 빈 행을 건드리지 않는다.
        #    발행 시각·수집 시각은 고르는 조건이 아니라 기사와 함께 넘기는 값이다 - 발행 시각이 수집 시각보다 10분 넘게
        #    뒤인 행을 페이지 값으로 바꾸는 데 쓴다(page_pubdate.py). 그래서 위 조건과 보존 잡의 "추출 대기" 조건은 그대로다.
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT N.raw_news_id, N.raw_news_url, P.press_name, N.raw_news_title,
                       N.raw_news_created_at, N.raw_news_crawled_at
                FROM news_raw N
                JOIN press P ON N.press_id = P.press_id
                WHERE (N.raw_news_extract_status IS NULL
                       OR (N.raw_news_extract_status = ANY(%s)
                           AND N.raw_news_extract_attempts < %s))
                  AND N.raw_news_content_purged_at IS NULL
                  AND N.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - make_interval(days => %s)
                ORDER BY N.raw_news_id
            """, (list(RETRYABLE_STATUSES), Settings.MAX_EXTRACT_ATTEMPTS, BODY_RETENTION_DAYS))
            articles = cur.fetchall()
        finally:
            release_connection(conn)

        stats: Dict[str, Any] = {
            "targets": len(articles), "workers": num_workers,
            **{status: 0 for status in _RECORDED_STATUSES}, DEFERRED: 0,
            "drop_reasons": {}, "per_press": {}, "error_samples": [],
            # 실패 원인(fetch.py 분류)별 개수 - 언론사별. URL·본문은 남기지 않는다.
            "fetch_fail_causes": {},
            # 다운로드에 성공한 요청의 시간 구간(<5s·5-15s·>=15s)별 개수 - 언론사별(fetch.latency_bucket)
            "fetch_ok_latency": {},
            # 본문 컨테이너 규칙이 있는 언론사(body_container.py)의 추출 경로별 개수: container | container_missing.
            # container_missing은 노드를 못 찾아 전체 페이지로 되돌아간 기사 - 페이지 개편 신호라 ingest가 경고한다
            "body_container": {},
            # 저장된 발행 시각이 수집 시각보다 10분 넘게 뒤(미래)여서 기사 페이지의 발행 시각으로 바꾼 행 수 - 언론사별.
            # created_at_unfixed는 미래였는데 못 바꾼 행의 사유(no_page_time·no_tz·after_crawl·too_old)별 개수 - 그 행은 미래인 채 남는다
            "created_at_from_page": {}, "created_at_unfixed": {},
            "deferred_reasons": {}, "press_breaker": {},
            "time_budget_s": time_budget_s, "time_budget_exhausted": False,
        }
        if not articles:
            logger.info("💤 본문을 추출할 기사가 없습니다.")
            return stats

        logger.info(f"🚀 병렬 본문 추출 시작 (대상: {len(articles)}건, 워커: {num_workers})")

        plan = DispatchPlan(
            articles, breaker_failures=Settings.EXTRACT_PRESS_BREAKER_FAILURES,
            deadline=None if time_budget_s is None else started + time_budget_s, clock=clock,
        )
        drop_reasons: Counter = Counter()
        progress = tqdm(total=len(articles), desc="🚀 본문 추출 중")

        def _count(press_name: str, status: str) -> None:
            stats[status] += 1
            press = stats["per_press"].setdefault(press_name, {})
            press[status] = press.get(status, 0) + 1

        def _collect(result: ExtractionResult) -> None:
            plan.record(result)
            progress.update(1)
            _count(result.press_name, result.status)
            drop_reasons.update(result.reasons)
            if result.status == "fetch_failed":
                causes = stats["fetch_fail_causes"].setdefault(result.press_name, {})
                cause = result.fetch_cause or "unknown"
                causes[cause] = causes.get(cause, 0) + 1
            elif result.fetch_s is not None:
                buckets = stats["fetch_ok_latency"].setdefault(result.press_name, {})
                bucket = latency_bucket(result.fetch_s)
                buckets[bucket] = buckets.get(bucket, 0) + 1
            if result.container:
                paths = stats["body_container"].setdefault(result.press_name, {})
                paths[result.container] = paths.get(result.container, 0) + 1
            if result.created_at_fix == CREATED_AT_FIXED:
                fixed = stats["created_at_from_page"]
                fixed[result.press_name] = fixed.get(result.press_name, 0) + 1
            elif result.created_at_fix != NOT_FUTURE:
                reasons = stats["created_at_unfixed"].setdefault(result.press_name, {})
                reasons[result.created_at_fix] = reasons.get(result.created_at_fix, 0) + 1
            if result.status == "error" and len(stats["error_samples"]) < 5:
                stats["error_samples"].append(result.detail)
                logger.error(f"   [Error] {result.press_name}: {result.detail}")

        try:
            if num_workers <= 1:
                while (article := plan.next_article()) is not None:
                    _collect(self.process_single_article(article))
            else:
                with _executor(num_workers) as executor:
                    in_flight: Dict[Any, tuple] = {}
                    while True:
                        # 워커 수만큼만 보내 둔다 - 결과를 보고 다음 기사를 고르므로 차단기와 예산이 바로 적용된다.
                        while len(in_flight) < num_workers and (article := plan.next_article()) is not None:
                            in_flight[executor.submit(self.process_single_article, article)] = article
                        if not in_flight:
                            break
                        done, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                        for future in done:
                            in_flight.pop(future)
                            _collect(future.result())
        finally:
            progress.close()

        for article, reason in plan.deferred:
            _count(article[2], DEFERRED)
            stats["deferred_reasons"][reason] = stats["deferred_reasons"].get(reason, 0) + 1
            if reason == DEFER_PRESS_BREAKER:
                entry = stats["press_breaker"].setdefault(
                    article[2], {"failures": plan.tripped.get(article[2], 0), "deferred": 0})
                entry["deferred"] += 1
        for press, failures in plan.tripped.items():  # 열렸지만 남은 기사가 없던 언론사도 남긴다
            stats["press_breaker"].setdefault(press, {"failures": failures, "deferred": 0})
        stats["time_budget_exhausted"] = plan.budget_exhausted
        stats["drop_reasons"] = dict(drop_reasons)
        logger.info(
            f"🏁 본문 추출 종료. 성공: {stats['ok']}, 필터링: {stats['dropped']}, 내용없음: {stats['empty']}, "
            f"중복: {stats['duplicate']}, 다운로드 실패: {stats['fetch_failed']}, 에러: {stats['error']}, "
            f"미룸: {stats[DEFERRED]} {stats['deferred_reasons'] or ''}"
        )
        return stats
