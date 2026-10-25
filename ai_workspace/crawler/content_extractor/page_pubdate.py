# 기사 페이지의 발행 시각으로 RSS pubDate의 "미래 시각"을 바로잡는다 (ADR 0008 "2026-10-25 갱신 2")
# - 국민일보 RSS가 2026-10-19부터 수집 시각보다 최대 24시간 뒤의 pubDate를 준다(원인 미규명, 오프셋이 일정하지 않다).
#   기사는 수집 시각 앞에 발행됐으므로 수집보다 뒤인 발행 시각은 어느 언론사든 틀린 값이다.
# - 본문 추출기는 어차피 기사 페이지를 받는다. 저장된 발행 시각이 수집 시각보다 10분 넘게 뒤인 행에 한해, 그 페이지의
#   JSON-LD datePublished(없으면 meta article:published_time)로 바꾼다. 미래가 아닌 행은 건드리지 않는다.
# - 쓰는 값은 그 기사 자신의 값뿐이다:
#     JSON-LD는 "최상위 기사 노드"의 문자열 datePublished만 받는다. 최상위 기사 노드는 @type이 Article 계열(*Article, BlogPosting 같은
#     게시물 형식. 배열·IRI·하위 형식 포함. JobPosting은 아니다)이고 덩어리 최상위이거나, 최상위 배열의 항목이거나, 최상위 객체의
#     @graph 항목인 노드다. 문서 순서로 첫 노드가 정한다 - 그 노드에 문자열 datePublished가 없으면(키 없음, {"@value": ...} 등)
#     JSON-LD 값은 없는 것이고 다음 기사 노드로 넘어가지 않는다. 중첩 노드(관련 기사 목록, ItemList, image의 ImageObject 등)와
#     기사가 아닌 노드(WebPage 등)의 값은 JSON이 정상으로 읽힌 덩어리에서는 쓰지 않는다.
#     정상으로 읽힌 최상위 기사 노드가 있으면 깨진 JSON-LD 덩어리는 보지 않는다. 깨진 덩어리뿐일 때는 구조를 알 수 없어 글자만 본다:
#     datePublished가 적힌 덩어리가 정확히 하나이고, 그 안의 서로 다른 값이 하나이고, 글자상 첫 "@type"이 Article 계열이고 그 값이
#     둘째 "@type"보다 앞에 있을 때만 쓴다(아니면 버린다). 그래도 글자만 보는 읽기라 중첩 노드의 값을 집을 수 있는 모양이 남는다 -
#     둘째 "@type" 앞에 관련 기사의 날짜가 오는 경우 등(ADR 0008 갱신 2의 한계 절).
#     meta article:published_time은 그다음 후보다. <head>(첫 <body> 앞)의 meta만 본다. JSON-LD와 meta, 또는 meta끼리 시각이 있는 값이
#     10분 넘게 다르면 쓰지 않고 conflict로 센다.
# - 표준 라이브러리만 쓴다 - 런북 6.10의 Mac 쪽 페이지 받기 스크립트가 같은 파일을 경로로 읽어 같은 규칙을 쓴다.
#
# 페이지 값을 쓰는 조건은 scripts/oci/build_kmib_created_at_fix.py(일회성 보정 생성기)와 같다:
#   시간대가 붙어 있고, 수집 시각 + 10분 이하이고, 수집 시각 - 7일 이상이다. 두 곳의 상수는 tests가 같은지 확인한다.
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

# 저장된 발행 시각이 수집 시각보다 이만큼 넘게 뒤이면 "미래"다. 서버 시계 차이로 몇 분 앞서는 값은 정상으로 본다.
FUTURE_SLACK = timedelta(minutes=10)
FUTURE_SLACK_MINUTES = 10
# 페이지 값이 수집 시각보다 이만큼 넘게 앞서면(너무 오래된 기사 / 엉뚱한 날짜) 쓰지 않는다.
MAX_AGE_BEFORE_CRAWL = timedelta(days=7)
# 같은 페이지의 JSON-LD 값과 meta 값이 이만큼 넘게 다르면 어느 쪽도 믿지 않는다.
MAX_SOURCE_DISAGREEMENT = timedelta(minutes=10)

# 판정 결과(ExtractionResult.created_at_fix, job_runs.stats.extract의 created_at_from_page·created_at_unfixed 키)
NOT_FUTURE = ""                # 미래가 아니다(또는 비교할 값이 없다) - 건드리지 않는다
CREATED_AT_FIXED = "fixed"     # 미래였고 페이지 값으로 바꿀 수 있다
NO_PAGE_TIME = "no_page_time"  # 미래인데 페이지에서 읽을 수 있는 발행 시각이 없다(JSON-LD·meta 없음, 값이 날짜가 아님)
NO_TZ = "no_tz"                # 페이지 값에 시간대가 없다 - 어느 시각인지 정할 수 없다
CONFLICT = "conflict"          # 페이지의 JSON-LD 값과 meta 값이 10분 넘게 다르다 - 어느 쪽이 맞는지 알 수 없다
AFTER_CRAWL = "after_crawl"    # 페이지 값도 수집 시각보다 10분 넘게 뒤다
TOO_OLD = "too_old"            # 페이지 값이 수집 시각보다 7일 넘게 앞이다
FETCH_FAILED = "fetch_failed"  # 미래인데 이번 시도에서 페이지를 못 받았다 - 다음 시도(상한 3회)에서 고칠 수 있다. 시도마다 센다
ERROR = "error"                # 미래인데 처리 중 예외로 롤백됐다(페이지를 받은 뒤일 수도 있다). 시도마다 센다
UNFIXED_REASONS = (NO_PAGE_TIME, NO_TZ, CONFLICT, AFTER_CRAWL, TOO_OLD, FETCH_FAILED, ERROR)

_META_KEY = "article:published_time"
_LD_JSON_TYPE = "application/ld+json"
_MAX_HTML_CHARS = 2_000_000  # 앞에서 이만큼만 읽는다(다운로드 상한은 20 MB다). 국민일보 페이지는 40만 바이트 안에 있었다
# 깨진 JSON-LD(줄바꿈이 문자열 안에 있는 경우 등)는 값만 정규식으로 읽는다. 런북 6.10의 예전 스크립트와 같은 식이다.
_DATE_PUBLISHED_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')
_TYPE_KEY_RE = re.compile(r'"@type"\s*:\s*')
_TYPE_VALUE_SPAN = 500  # "@type" 키 뒤에서 값(문자열 하나나 이름 배열)을 읽는 글자 수 - 닫히지 않은 '['가 많아도 읽는 양이 덩어리 크기에 비례하게 한다
_QUOTED_RE = re.compile(r'"([^"]*)"')
_MAX_META_VALUES = 50  # 한 페이지에서 모으는 meta 값 수(서로 견주는 쌍 수의 상한)
# 시각 부분이 있는 값인가: 'T'나 공백 뒤에 숫자(2026-10-25T18:50, 2026-10-25 18:50). 날짜만 적은 값(2026-10-25)은 자정으로 읽히지만 자정의 시각이 아니다.
_TIME_PART_RE = re.compile(r"[Tt ]\d")
# Article 계열 @type 이름의 끝. JobPosting(구인 공고)은 Article 하위 형식이 아니라서 *Posting을 다 받지 않는다.
_ARTICLE_TYPE_SUFFIXES = ("article", "blogposting", "socialmediaposting", "discussionforumposting")


class PageCreatedAt(NamedTuple):
    outcome: str  # NOT_FUTURE | CREATED_AT_FIXED | UNFIXED_REASONS 중 하나
    value: Optional[datetime] = None  # CREATED_AT_FIXED일 때만 채운다(UTC)


class CreatedAtChange(NamedTuple):
    """실제로 바꾼 행 하나의 기록: 행 번호와 바꾸기 전·후 값(시간대 있는 시각). job_runs.stats에 남겨 되돌릴 수 있게 한다."""
    raw_news_id: int
    before: datetime
    after: datetime

    def as_stats_row(self) -> list:
        """[행 번호, 바꾸기 전(UTC ISO), 바꾼 뒤(UTC ISO)]. 세션 시간대와 상관없이 UTC로 적는다."""
        return [self.raw_news_id, _iso_utc(self.before), _iso_utc(self.after)]


class PageValues(NamedTuple):
    json_ld: Optional[str]  # 최상위 기사 노드의 문자열 datePublished(없으면 None)
    meta: Tuple[str, ...]   # meta article:published_time 값들(문서 순서)


class _PageTimeParser(HTMLParser):
    """JSON-LD 스크립트(문서 어디든)와 <head>의 meta article:published_time을 문서 순서로 모은다.

    meta는 첫 <body> 앞의 것만 모은다 - 본문 안(관련 기사 카드 등)의 meta는 그 기사의 것이 아닐 수 있다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ld_blocks: List[str] = []
        self.meta_values: List[str] = []
        self._in_ld = False
        self._in_body = False
        self._buf: List[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "body":
            self._in_body = True
        elif tag == "script" and (attributes.get("type") or "").strip().lower() == _LD_JSON_TYPE:
            self._in_ld, self._buf = True, []
        elif tag == "meta" and not self._in_body and len(self.meta_values) < _MAX_META_VALUES:
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


def _is_article_type_name(name: str) -> bool:
    """@type 한 칸이 Article 계열(*Article, BlogPosting·LiveBlogPosting·SocialMediaPosting·DiscussionForumPosting)인가.
    IRI(http://schema.org/NewsArticle)·접두어(schema:Article)는 마지막 토막만 본다. JobPosting 같은 Article이 아닌 *Posting은 아니다."""
    local = re.split(r"[/#:]", name.strip().rstrip("/#"))[-1].lower()
    return local.endswith(_ARTICLE_TYPE_SUFFIXES)


def _is_article_node(node: dict) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and _is_article_type_name(k) for k in kinds)


def _top_level_nodes(data: Any) -> List[dict]:
    """덩어리 최상위 객체, 최상위 배열의 객체 항목, 최상위 객체의 @graph 항목을 문서 순서로. 그 밖의 중첩은 보지 않는다."""
    if isinstance(data, dict):
        nodes = [data]
        graph = data.get("@graph")
        if isinstance(graph, list):
            nodes.extend(item for item in graph if isinstance(item, dict))
        elif isinstance(graph, dict):
            nodes.append(graph)
        return nodes
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _clean_string(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_type_values(block: str, limit: int = 2) -> List[Tuple[int, List[str]]]:
    """깨진 덩어리의 글자 안에서 앞에서부터 limit개의 "@type" 값: (키의 위치, 이름들). 문자열 하나나 이름 배열을 읽고,
    값은 키 뒤 _TYPE_VALUE_SPAN글자 안에서만 읽는다(닫히지 않은 '['가 많은 덩어리에서도 시간이 덩어리 크기에 비례한다)."""
    found: List[Tuple[int, List[str]]] = []
    for match in _TYPE_KEY_RE.finditer(block):
        tail = block[match.end(): match.end() + _TYPE_VALUE_SPAN]
        names: List[str] = []
        if tail.startswith("["):
            close = tail.find("]")
            names = _QUOTED_RE.findall(tail[1:] if close == -1 else tail[1:close])
        elif tail.startswith('"'):
            close = tail.find('"', 1)
            names = [] if close == -1 else [tail[1:close]]
        found.append((match.start(), names))
        if len(found) >= limit:
            break
    return found


def _broken_block_value(blocks: List[str]) -> Optional[str]:
    """깨진 덩어리뿐일 때 쓸 수 있는 값. 구조를 알 수 없으므로 모호하면 버린다:
    datePublished가 적힌 덩어리가 정확히 하나이고, 그 안의 서로 다른 값이 하나이고, 글자상 첫 "@type"이 Article 계열이고
    그 값(첫 datePublished)이 둘째 "@type"보다 앞에 있을 때만. 첫 "@type"이 기사가 아니면(ItemList 등) 안쪽 기사의 값을 집지 않고,
    둘째 "@type" 뒤의 값은 기사 자신의 것이 아니라 중첩 노드(관련 기사 · image의 ImageObject)의 것일 수 있어 집지 않는다."""
    with_values = []
    for block in blocks:
        values = {value.strip() for value in _DATE_PUBLISHED_RE.findall(block) if value.strip()}
        if values:
            with_values.append((block, values))
    if len(with_values) != 1:
        return None
    block, values = with_values[0]
    if len(values) != 1:
        return None
    types = _first_type_values(block)
    if not types or not any(_is_article_type_name(name) for name in types[0][1]):
        return None
    first_value_at = next(m.start() for m in _DATE_PUBLISHED_RE.finditer(block) if m.group(1).strip())
    if len(types) > 1 and first_value_at > types[1][0]:
        return None
    return next(iter(values))


def _json_ld_value(blocks: List[str]) -> Optional[str]:
    """그 기사 자신의 JSON-LD 발행 시각 문자열. 최상위 기사 노드가 있으면 문서 순서로 첫 노드가 정한다(값이 없으면 None이고
    다음 노드로 넘어가지 않는다) - 깨진 덩어리는 그때 보지 않는다. 최상위 기사 노드가 없을 때만 깨진 덩어리를 본다."""
    broken: List[str] = []
    for block in blocks:
        try:
            data = json.loads(block, strict=False)
        except (ValueError, RecursionError):  # 깨진 덩어리(깊게 중첩된 것 포함) - 정규식으로 읽을지는 마지막에 정한다
            broken.append(block)
            continue
        for node in _top_level_nodes(data):
            if _is_article_node(node):
                return _clean_string(node.get("datePublished"))
    return _broken_block_value(broken)


def read_page_values(html: str) -> PageValues:
    """페이지가 적은 그 기사 자신의 발행 시각 문자열: (JSON-LD 최상위 기사 노드의 값 하나, meta article:published_time 값들).
    예외를 던지지 않는다. 중첩된 노드나 기사가 아닌 노드의 datePublished는 읽지 않는다."""
    if not isinstance(html, str) or not html:
        return PageValues(None, ())
    parser = _PageTimeParser()
    try:
        parser.feed(html[:_MAX_HTML_CHARS])
        parser.close()
    except Exception:  # noqa: BLE001 - 깨진 마크업: 읽은 데까지만 쓴다
        logger.debug("발행 시각 파싱 중 예외 - 읽은 데까지만 쓴다", exc_info=True)
    parser.flush()
    try:
        json_ld = _json_ld_value(parser.ld_blocks)
    except Exception:  # noqa: BLE001 - JSON-LD를 못 읽는 것이 meta까지 버리게 하면 안 된다
        logger.debug("JSON-LD 발행 시각 읽기 중 예외 - meta만 쓴다", exc_info=True)
        json_ld = None
    return PageValues(json_ld, tuple(parser.meta_values))


class _Stamp(NamedTuple):
    value: datetime
    has_time: bool  # 날짜만 적은 값(2026-10-25)이면 거짓 - 자정으로 읽힐 뿐이라 시각 있는 값과 견주지 않는다


def _read_stamp(text: Optional[str]) -> Optional[_Stamp]:
    """페이지가 적은 시각 문자열. ISO가 아니면(날짜가 아님) None."""
    if not text:
        return None
    try:
        return _Stamp(datetime.fromisoformat(text.strip()), bool(_TIME_PART_RE.search(text.strip())))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    # psycopg2는 timestamptz를 시간대 있는 값으로, raw_news_crawled_at(시간대 없는 UTC)은 시간대 없는 값으로 준다.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    """UTC ISO 문자열('...Z'). 되돌리기 SQL의 ::timestamptz가 그대로 읽는다. 마이크로초가 있으면 그대로 적는다."""
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _disagree(a: datetime, b: datetime) -> bool:
    """두 페이지 값이 MAX_SOURCE_DISAGREEMENT 넘게 다른가. 한쪽만 시간대가 없으면 벽시계 시각으로 견준다(시간대 없는 쪽이 같은 곳의 시각이라고 본다)."""
    if (a.tzinfo is None) != (b.tzinfo is None):
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return abs(a - b) > MAX_SOURCE_DISAGREEMENT


def _any_disagree(values: List[datetime]) -> bool:
    """시각 있는 페이지 값 가운데 서로 10분 넘게 다른 쌍이 있는가(JSON-LD와 meta, meta끼리). 같은 값은 한 번만 견준다."""
    unique = list(dict.fromkeys(values))
    return any(_disagree(a, b) for i, a in enumerate(unique) for b in unique[i + 1:])


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
        values = read_page_values(html)
        # 읽을 수 없는 값(날짜가 아님)은 없는 것으로 본다. 순서: JSON-LD, 그다음 meta(문서 순서)
        stamps = [s for s in (_read_stamp(t) for t in (values.json_ld, *values.meta)) if s is not None]
        if not stamps:
            return PageCreatedAt(NO_PAGE_TIME)
        if _any_disagree([s.value for s in stamps if s.has_time]):
            return PageCreatedAt(CONFLICT)  # 한 페이지가 두 시각을 말한다 - 어느 쪽도 쓰지 않는다(날짜만 적은 값은 견주지 않는다)
        chosen = next((s.value for s in stamps if s.value.tzinfo is not None), None)
        if chosen is None:
            return PageCreatedAt(NO_TZ)
        page = chosen.astimezone(timezone.utc)  # JSON-LD 값이 우선이고, 시간대가 없거나 읽을 수 없으면 시간대 있는 첫 meta
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
