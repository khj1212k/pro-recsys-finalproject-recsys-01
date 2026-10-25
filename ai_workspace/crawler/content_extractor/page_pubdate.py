# 기사 페이지의 발행 시각으로 RSS pubDate의 "미래 시각"을 바로잡는다 (ADR 0008 "2026-10-25 갱신 2")
# - 국민일보 RSS가 2026-10-19부터 수집 시각보다 최대 24시간 뒤의 pubDate를 준다(원인 미규명, 오프셋이 일정하지 않다).
#   기사는 수집 시각 앞에 발행됐으므로 수집보다 뒤인 발행 시각은 어느 언론사든 틀린 값이다.
# - 본문 추출기는 어차피 기사 페이지를 받는다. 저장된 발행 시각이 수집 시각보다 10분 넘게 뒤인 행에 한해, 그 페이지의
#   JSON-LD datePublished(없으면 meta article:published_time)로 바꾼다. 미래가 아닌 행은 건드리지 않는다.
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
UNFIXED_REASONS = (NO_PAGE_TIME, NO_TZ, AFTER_CRAWL, TOO_OLD)

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


def _ld_date_published(block: str) -> List[str]:
    """JSON-LD 한 덩어리에서 datePublished 문자열을 우선순위 순으로 돌려준다.

    기사 자신의 값이 앞선다: @type이 *Article/*Posting인 노드 > 그 밖의 노드, 얕은 노드 > 깊은 노드(관련 기사 목록 같은
    중첩된 항목이 뒤로 간다), 같으면 문서 순서."""
    try:
        data = json.loads(block, strict=False)
    except ValueError:
        return [m.group(1) for m in _DATE_PUBLISHED_RE.finditer(block)]
    found: List[Tuple[bool, int, int, str]] = []
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
                found.append((not _is_article_node(node), depth, len(found), value.strip()))
            queue.extend((child, depth + 1) for child in node.values() if isinstance(child, (dict, list)))
    return [value for _, _, _, value in sorted(found)]


def page_published_candidates(html: str) -> List[str]:
    """페이지가 적은 발행 시각 문자열들. JSON-LD datePublished가 먼저, 그다음 meta article:published_time. 예외를 던지지 않는다."""
    if not isinstance(html, str) or not html:
        return []
    parser = _PageTimeParser()
    try:
        parser.feed(html[:_MAX_HTML_CHARS])
        parser.close()
    except Exception:  # noqa: BLE001 - 깨진 마크업: 읽은 데까지만 쓴다
        logger.debug("발행 시각 파싱 중 예외 - 읽은 데까지만 쓴다", exc_info=True)
    parser.flush()
    candidates: List[str] = []
    for block in parser.ld_blocks:
        candidates.extend(_ld_date_published(block))
    candidates.extend(parser.meta_values)
    return candidates


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
        parsed = [d for d in (_parse_iso(c) for c in page_published_candidates(html)) if d is not None]
        if not parsed:
            return PageCreatedAt(NO_PAGE_TIME)
        aware = [d for d in parsed if d.tzinfo is not None]
        if not aware:
            return PageCreatedAt(NO_TZ)
        page = aware[0].astimezone(timezone.utc)  # 우선순위가 가장 높은, 시간대가 있는 값
        if page > crawled + FUTURE_SLACK:
            return PageCreatedAt(AFTER_CRAWL)
        if page < crawled - MAX_AGE_BEFORE_CRAWL:
            return PageCreatedAt(TOO_OLD)
        return PageCreatedAt(CREATED_AT_FIXED, page)
    except Exception:  # noqa: BLE001 - 발행 시각을 못 고치는 것이 본문 추출을 막아서는 안 된다
        logger.debug("발행 시각 판정 중 예외 - 고치지 않는다", exc_info=True)
        return PageCreatedAt(NO_PAGE_TIME)
