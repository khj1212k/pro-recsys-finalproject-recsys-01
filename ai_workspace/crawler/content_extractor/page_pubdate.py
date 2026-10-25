# 기사 페이지의 발행 시각으로 RSS pubDate의 "미래 시각"을 바로잡는다 (ADR 0008 "2026-10-25 갱신 2")
# - 국민일보 RSS가 2026-10-19부터 수집 시각보다 최대 24시간 뒤의 pubDate를 준다(원인 미규명, 오프셋이 일정하지 않다).
#   기사는 수집 시각 앞에 발행됐으므로 수집보다 뒤인 발행 시각은 어느 언론사든 틀린 값이다.
# - 본문 추출기는 어차피 기사 페이지를 받는다. 저장된 발행 시각이 수집 시각보다 10분 넘게 뒤인 행에 한해, 그 페이지의
#   JSON-LD datePublished(없으면 meta article:published_time)로 바꾼다. 미래가 아닌 행은 건드리지 않는다.
# - 쓰는 값은 그 기사 자신의 값뿐이다: 모든 JSON-LD 덩어리를 통틀어 우선순위 1위인 노드의 값, 그다음 meta. 1위 값을 못 쓴다고
#   관련 기사 목록 같은 다른 노드의 값으로 넘어가지 않는다(다른 기사의 시각을 이 행에 써 넣게 된다).
# - 표준 라이브러리만 쓴다 - 런북 6.10의 Mac 쪽 페이지 받기 스크립트가 같은 파일을 경로로 읽어 같은 규칙을 쓴다.
#
# 페이지 값을 쓰는 조건은 scripts/oci/build_kmib_created_at_fix.py(일회성 보정 생성기)와 같다:
#   시간대가 붙어 있고, 수집 시각 + 10분 이하이고, 수집 시각 - 7일 이상이다. 두 곳의 상수는 tests가 같은지 확인한다.
import json
import logging
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

# 저장된 발행 시각이 수집 시각보다 이만큼 넘게 뒤이면 "미래"다. 서버 시계 차이로 몇 분 앞서는 값은 정상으로 본다.
FUTURE_SLACK = timedelta(minutes=10)
FUTURE_SLACK_MINUTES = 10
# 페이지 값이 수집 시각보다 이만큼 넘게 앞서면(너무 오래된 기사 / 엉뚱한 날짜) 쓰지 않는다.
MAX_AGE_BEFORE_CRAWL = timedelta(days=7)

# 판정 결과(ExtractionResult.created_at_fix, job_runs.stats.extract의 created_at_from_page·created_at_unfixed 키)
NOT_FUTURE = ""                # 미래가 아니다(또는 비교할 값이 없다) - 건드리지 않는다
CREATED_AT_FIXED = "fixed"     # 미래였고 페이지 값으로 바꿀 수 있다
NO_PAGE_TIME = "no_page_time"  # 미래인데 페이지에서 읽을 수 있는 발행 시각이 없다(JSON-LD·meta 없음, 값이 날짜가 아님)
NO_TZ = "no_tz"                # 페이지 값에 시간대가 없다 - 어느 시각인지 정할 수 없다
AFTER_CRAWL = "after_crawl"    # 페이지 값도 수집 시각보다 10분 넘게 뒤다
TOO_OLD = "too_old"            # 페이지 값이 수집 시각보다 7일 넘게 앞이다
FETCH_FAILED = "fetch_failed"  # 미래인데 이번 시도에서 페이지를 못 받았다 - 다음 시도(상한 3회)에서 고칠 수 있다. 시도마다 센다
ERROR = "error"                # 미래인데 처리 중 예외로 롤백됐다(페이지를 받은 뒤일 수도 있다). 시도마다 센다
UNFIXED_REASONS = (NO_PAGE_TIME, NO_TZ, AFTER_CRAWL, TOO_OLD, FETCH_FAILED, ERROR)

_META_KEY = "article:published_time"
_LD_JSON_TYPE = "application/ld+json"
_MAX_NODES = 5000  # JSON-LD를 훑는 노드 수 상한 - 비정상적으로 큰 데이터에 시간을 쓰지 않는다
_MAX_HTML_CHARS = 2_000_000  # 앞에서 이만큼만 읽는다(다운로드 상한은 20 MB다). 국민일보 페이지는 40만 바이트 안에 있었다
# 깨진 JSON-LD(줄바꿈이 문자열 안에 있는 경우 등)는 값만 정규식으로 읽는다. 런북 6.10의 예전 스크립트와 같은 식이다.
_DATE_PUBLISHED_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


class PageCreatedAt(NamedTuple):
    outcome: str  # NOT_FUTURE | CREATED_AT_FIXED | UNFIXED_REASONS 중 하나
    value: Optional[datetime] = None  # CREATED_AT_FIXED일 때만 채운다(UTC)


class _PageTimeParser(HTMLParser):
    """JSON-LD 스크립트와 meta article:published_time의 값을 문서 순서로 모은다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ld_blocks: List[str] = []
        self.meta_values: List[str] = []
        self._in_ld = False
        self._buf: List[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and (attributes.get("type") or "").strip().lower() == _LD_JSON_TYPE:
            self._in_ld, self._buf = True, []
        elif tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").strip().lower()
            content = attributes.get("content")
            if key == _META_KEY and content and content.strip():
                self.meta_values.append(content.strip())

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.ld_blocks.append("".join(self._buf))
            self._in_ld, self._buf = False, []

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)

    def flush(self) -> None:
        if self._in_ld and self._buf:  # </script>가 없는 페이지: 읽은 데까지 쓴다
            self.ld_blocks.append("".join(self._buf))
        self._in_ld, self._buf = False, []


def _is_article_node(node: dict) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and ("article" in k.lower() or "posting" in k.lower()) for k in kinds)


# JSON-LD 후보의 순위 키: (@type이 *Article이 아니다, 깊이, 덩어리 번호, 덩어리 안 순서). 작을수록 앞선다. 값은 맨 뒤에 붙는다.
_Ranked = Tuple[bool, int, int, int, str]


def _ld_date_published(block: str, block_index: int) -> List[_Ranked]:
    """JSON-LD 한 덩어리의 datePublished 값을 순위 키와 함께 돌려준다(정렬은 부르는 쪽이 모든 덩어리를 모아서 한다).

    기사 자신의 값이 앞선다: @type이 *Article/*Posting인 노드 > 그 밖의 노드, 얕은 노드 > 깊은 노드(관련 기사 목록 같은
    중첩된 항목이 뒤로 간다), 같으면 문서 순서. 깨진 JSON-LD는 형식 정보가 없어 최상위 *Article 층으로 보고 문서 순서에 둔다."""
    try:
        data = json.loads(block, strict=False)
    except ValueError:
        return [(False, 0, block_index, i, m.group(1)) for i, m in enumerate(_DATE_PUBLISHED_RE.finditer(block))]
    found: List[_Ranked] = []
    queue = deque([(data, 0)])
    visited = 0
    while queue and visited < _MAX_NODES:
        node, depth = queue.popleft()
        visited += 1
        if isinstance(node, list):
            queue.extend((child, depth) for child in node)  # 배열은 깊이를 늘리지 않는다(@graph 항목이 최상위와 같은 층이다)
        elif isinstance(node, dict):
            value = node.get("datePublished")
            if isinstance(value, str) and value.strip():
                found.append((not _is_article_node(node), depth, block_index, len(found), value.strip()))
            queue.extend((child, depth + 1) for child in node.values() if isinstance(child, (dict, list)))
    return found


def _read_page(html: str) -> Tuple[List[str], List[str]]:
    """(JSON-LD datePublished 값들을 우선순위 순으로, meta article:published_time 값들). 예외를 던지지 않는다.

    우선순위는 모든 JSON-LD 덩어리를 통틀어 매긴다 - 앞 덩어리가 관련 기사 목록이어도 뒤 덩어리에 있는 페이지 자신의 *Article이 앞선다."""
    if not isinstance(html, str) or not html:
        return [], []
    parser = _PageTimeParser()
    try:
        parser.feed(html[:_MAX_HTML_CHARS])
        parser.close()
    except Exception:  # noqa: BLE001 - 깨진 마크업: 읽은 데까지만 쓴다
        logger.debug("발행 시각 파싱 중 예외 - 읽은 데까지만 쓴다", exc_info=True)
    parser.flush()
    ranked: List[_Ranked] = []
    for index, block in enumerate(parser.ld_blocks):
        ranked.extend(_ld_date_published(block, index))
    ranked.sort()
    return [entry[-1] for entry in ranked], parser.meta_values


def page_published_candidates(html: str) -> List[str]:
    """페이지가 적은 발행 시각 문자열 전부. JSON-LD datePublished가 우선순위 순으로, 그다음 meta article:published_time. 예외를 던지지 않는다."""
    ld_values, meta_values = _read_page(html)
    return ld_values + meta_values


def page_own_candidates(html: str) -> List[str]:
    """그 기사 자신의 발행 시각 후보: JSON-LD 우선순위 1위 노드의 값 하나, 그다음 meta article:published_time. 예외를 던지지 않는다.

    2위 이하 JSON-LD 값(같은 페이지의 관련 기사·다른 노드)은 넣지 않는다 - 1위 값을 못 쓸 때 다른 기사의 시각으로 넘어가면 안 된다."""
    ld_values, meta_values = _read_page(html)
    return ld_values[:1] + meta_values


def _parse_iso(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    # psycopg2는 timestamptz를 시간대 있는 값으로, raw_news_crawled_at(시간대 없는 UTC)은 시간대 없는 값으로 준다.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def is_future(stored_created_at: Optional[datetime], crawled_at: Optional[datetime]) -> bool:
    """저장된 발행 시각이 수집 시각(UTC)보다 FUTURE_SLACK 넘게 뒤인가. 어느 한쪽이 없으면 거짓."""
    if stored_created_at is None or crawled_at is None:
        return False
    return _as_utc(stored_created_at) > _as_utc(crawled_at) + FUTURE_SLACK


def decide_created_at(stored_created_at: Optional[datetime], crawled_at: Optional[datetime], html: str) -> PageCreatedAt:
    """저장된 발행 시각을 페이지 값으로 바꿀지 정한다. 예외를 던지지 않는다.

    stored_created_at: 행의 raw_news_created_at. crawled_at: 행의 raw_news_crawled_at(시간대 없는 UTC).
    미래가 아닌 행은 페이지를 읽지도 않고 NOT_FUTURE를 돌려준다 - 정상 피드의 값을 페이지 값으로 덮지 않는다."""
    try:
        if not is_future(stored_created_at, crawled_at):
            return PageCreatedAt(NOT_FUTURE)
        crawled = _as_utc(crawled_at)
        parsed = [d for d in (_parse_iso(c) for c in page_own_candidates(html)) if d is not None]
        if not parsed:
            return PageCreatedAt(NO_PAGE_TIME)
        aware = [d for d in parsed if d.tzinfo is not None]
        if not aware:
            return PageCreatedAt(NO_TZ)
        page = aware[0].astimezone(timezone.utc)  # 그 기사 자신의 값 가운데 우선순위가 가장 높은, 시간대가 있는 값
        if page > crawled + FUTURE_SLACK:
            return PageCreatedAt(AFTER_CRAWL)
        if page < crawled - MAX_AGE_BEFORE_CRAWL:
            return PageCreatedAt(TOO_OLD)
        return PageCreatedAt(CREATED_AT_FIXED, page)
    except Exception:  # noqa: BLE001 - 발행 시각을 못 고치는 것이 본문 추출을 막아서는 안 된다
        logger.debug("발행 시각 판정 중 예외 - 고치지 않는다", exc_info=True)
        return PageCreatedAt(NO_PAGE_TIME)


def unfixed_if_future(stored_created_at: Optional[datetime], crawled_at: Optional[datetime], reason: str) -> str:
    """페이지를 읽기 전에 끝난 시도(FETCH_FAILED·ERROR)의 분류: 미래인 행이면 reason, 아니면 NOT_FUTURE. 예외를 던지지 않는다.

    이런 행도 미래인 채 남으므로 created_at_unfixed 통계에 잡히게 한다."""
    try:
        return reason if is_future(stored_created_at, crawled_at) else NOT_FUTURE
    except Exception:  # noqa: BLE001 - 통계를 못 남기는 것이 추출 결과를 막아서는 안 된다
        logger.debug("미래 발행 시각 판정 중 예외 - 세지 않는다", exc_info=True)
        return NOT_FUTURE
