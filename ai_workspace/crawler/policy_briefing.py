# 정책브리핑(korea.kr) 정책뉴스 수집 - 재배포 가능한 공개 평가 부분집합용 (docs/adr/0023)
#
# korea.kr RSS는 2026-07-01에 전부 중단됐다("콘텐츠 저작권 등 권리 보호에 따른 제공방식 변경",
# https://www.korea.kr/etc/noticeView.do?newsId=132038885, 2026-09-26 확인). 같은 정책뉴스를
# 공공데이터포털 Open API "문화체육관광부_정책브리핑_정책뉴스_API"로 받는다
# (https://www.data.go.kr/data/15095335/openapi.do, 이용허락범위: 공공저작물 출처표시 제1유형).
#
# - 인증키가 필요하다: 공공데이터포털에서 활용신청(자동승인) 후 발급받은 "일반 인증키(Decoding)"를
#   환경변수 DATA_GO_KR_SERVICE_KEY에 둔다. 키가 없으면 수집을 건너뛴다(에러 아님).
# - 한 번에 조회할 수 있는 날짜 범위는 3일이다(에러 코드 98 "날짜범위 3일 초과").
# - 기사마다 KoglType(공공누리 유형)이 붙는다. 제1유형인 기사만 저장한다 - 이 출처를 넣는 이유가
#   "출처만 밝히면 재배포할 수 있는 부분집합"이기 때문이다. 사진·이미지는 공공누리 대상이
#   아니므로(https://www.korea.kr/guide/copyRight.do) 본문 정제에서 이미지·캡션을 뺀다.
# - 날짜 파라미터 형식(YYYYMMDD)은 공공데이터포털 API의 관례를 따른 것이고, 인증키가 없어
#   실제 응답으로는 아직 확인하지 못했다. 형식이 틀리면 API가 에러 코드 97을 돌려주고
#   PolicyBriefingAPIError로 드러난다.
import hashlib
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import lxml.html
import requests
from psycopg2.extras import execute_values

from config.settings import Settings
from config.sources import POLICY_BRIEFING_PRESS
from crawler.content_extractor.cleaners import is_drop_article
from db.connection import get_connection, release_connection

logger = logging.getLogger(__name__)

POLICY_NEWS_ENDPOINT = "https://apis.data.go.kr/1371000/policyNewsService2/policyNewsList2"
SERVICE_KEY_ENV = "DATA_GO_KR_SERVICE_KEY"
MAX_WINDOW_DAYS = 3
REQUEST_TIMEOUT_S = 30
KST = timezone(timedelta(hours=9))

# 이미지·캡션·스크립트 등 텍스트 본문이 아닌 요소. 공공누리 표시는 텍스트에만 적용된다.
_DROP_TAGS = ("script", "style", "img", "figure", "figcaption", "iframe", "video", "audio",
              "object", "embed", "noscript", "picture", "source", "svg")
# 뒤에 공백을 넣어 문단/줄 경계가 붙어 버리지 않게 할 블록 요소 (인라인 요소는 그대로 둔다:
# "<b>정부</b>는"이 "정부 는"으로 쪼개지면 형태소가 깨진다)
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "section", "article",
               "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt", "hr"}


class PolicyBriefingAPIError(RuntimeError):
    """API가 정상 HTTP로 에러 코드를 돌려준 경우(인증키 오류, 날짜 범위 초과 등)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"정책브리핑 API 에러 {code}: {message}")
        self.code = code
        self.message = message


class PolicyBriefingFetchError(RuntimeError):
    """네트워크 실패. 메시지에 요청 URL(인증키 포함)을 싣지 않는다."""


@dataclass
class PolicyNewsItem:
    news_item_id: str
    title: str
    contents_type: str
    data_contents: str
    approve_date: Optional[datetime]
    embargo_date: Optional[datetime]
    original_url: str
    minister_code: str
    kogl_type: str
    grouping_code: str


@dataclass
class PolicyNewsRow:
    title: str
    content: str
    url: str
    published_at: Optional[datetime]


# --------------------------------------------------------------------------- 파싱

def parse_kst(value: Optional[str]) -> Optional[datetime]:
    """API 날짜 형식 "MM/DD/YYYY HH24:MI:SS"(명세)를 KST aware datetime으로."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%m/%d/%Y %H:%M:%S").replace(tzinfo=KST)
    except ValueError:
        return None


def _text(el: Optional[ET.Element], tag: str) -> str:
    if el is None:
        return ""
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def parse_policy_news_xml(xml_text: str) -> List[PolicyNewsItem]:
    root = ET.fromstring(xml_text)

    # 게이트웨이 에러 봉투(인증키 없음/무효 등): <OpenAPI_ServiceResponse><cmmMsgHeader>...
    if root.tag == "OpenAPI_ServiceResponse":
        header = root.find("cmmMsgHeader")
        code = _text(header, "returnReasonCode") or "unknown"
        message = " / ".join(filter(None, [_text(header, "errMsg"), _text(header, "returnAuthMsg")]))
        raise PolicyBriefingAPIError(code, message)

    header = root.find("header")
    code = _text(header, "resultCode")
    # 성공 코드 표기는 명세에 없다("0"/"00"/"INFO-000" 등이 쓰인다) - 숫자가 모두 0이면 성공으로 본다.
    digits = re.sub(r"\D", "", code)
    if code and not (digits and int(digits) == 0):
        raise PolicyBriefingAPIError(code, _text(header, "resultMsg"))

    items = []
    for node in root.iter("NewsItem"):
        items.append(PolicyNewsItem(
            news_item_id=_text(node, "NewsItemId"),
            title=_text(node, "Title"),
            contents_type=_text(node, "ContentsType").upper(),
            data_contents=_text(node, "DataContents"),
            approve_date=parse_kst(_text(node, "ApproveDate")),
            embargo_date=parse_kst(_text(node, "EmbargoDate")),
            original_url=_text(node, "OriginalUrl"),
            minister_code=_text(node, "MinisterCode"),
            kogl_type=_text(node, "KoglType"),
            grouping_code=_text(node, "GroupingCode"),
        ))
    return items


def normalize_kogl_type(value: Optional[str]) -> Optional[int]:
    """KoglType 값을 1~4 유형 번호로. 표기 형식이 명세에 없어("1", "제1유형" 등) 숫자 하나만
    들어 있을 때만 인정하고, 비었거나 애매하면 None(= 재배포 불가로 취급)."""
    numbers = re.findall(r"\d+", value or "")
    if len(numbers) != 1:
        return None
    n = int(numbers[0])
    return n if 1 <= n <= 4 else None


# --------------------------------------------------------------------------- 날짜 창

def date_windows(start: date, end: date, max_days: int = MAX_WINDOW_DAYS) -> List[Tuple[date, date]]:
    """[start, end](양끝 포함)를 겹치지 않는 max_days일 이하 구간으로 자른다."""
    windows = []
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=max_days - 1), end)
        windows.append((cur, stop))
        cur = stop + timedelta(days=1)
    return windows


# --------------------------------------------------------------------------- 정제

def _normalize_spaces(text: str) -> str:
    # 다른 언론사 본문(cleaners.clean_text_lite)과 같은 형태: 모든 공백을 한 칸으로
    return " ".join((text or "").split())


def clean_policy_news_html(html: str) -> str:
    if not (html or "").strip():
        return ""
    root = lxml.html.fragment_fromstring(html, create_parent="div")
    for el in list(root.iter(*_DROP_TAGS)):
        el.drop_tree()
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.lower() in _BLOCK_TAGS:
            el.tail = " " + (el.tail or "")
    return _normalize_spaces(root.text_content())


def clean_policy_news_content(item: PolicyNewsItem) -> str:
    # ContentsType: H = HTML, T = 태그를 모두 뺀 텍스트(명세)
    if item.contents_type == "T":
        return _normalize_spaces(item.data_contents)
    return clean_policy_news_html(item.data_contents)


def select_rows(items: List[PolicyNewsItem], now: datetime) -> Tuple[List[PolicyNewsRow], Dict[str, int]]:
    """저장할 기사만 고른다: 공공누리 제1유형, 원문 URL 있음, 엠바고 해제, 정제 후 본문이 충분함."""
    stats = {"fetched": len(items), "kept": 0, "not_kogl_type_1": 0, "no_url": 0,
             "too_short_or_filtered": 0, "embargoed": 0}
    rows = []
    for item in items:
        if normalize_kogl_type(item.kogl_type) != 1:
            stats["not_kogl_type_1"] += 1
            continue
        if not item.original_url:
            stats["no_url"] += 1
            continue
        if item.embargo_date and item.embargo_date > now:
            stats["embargoed"] += 1
            continue
        content = clean_policy_news_content(item)
        should_drop, _reasons = is_drop_article(content, item.title, POLICY_BRIEFING_PRESS)
        if not content or should_drop:
            stats["too_short_or_filtered"] += 1
            continue
        rows.append(PolicyNewsRow(title=item.title, content=content, url=item.original_url,
                                  published_at=item.approve_date))
        stats["kept"] += 1
    return rows, stats


# --------------------------------------------------------------------------- HTTP

def fetch_policy_news(service_key: str, start: date, end: date, http_get: Callable = requests.get,
                      max_attempts: Optional[int] = None, sleep_fn: Callable = time.sleep) -> List[PolicyNewsItem]:
    """한 날짜 구간(최대 3일)의 정책뉴스를 받는다. 네트워크 실패·5xx만 지수 백오프로 재시도하고,
    API 에러 코드(인증키·날짜 범위 등)는 재시도해도 같으므로 바로 올린다."""
    params = {"serviceKey": service_key, "startDate": start.strftime("%Y%m%d"),
              "endDate": end.strftime("%Y%m%d")}
    max_attempts = max_attempts or Settings.MAX_FETCH_RETRIES
    delay = 1.0
    last_problem = "no attempt"
    for attempt in range(max_attempts):
        try:
            resp = http_get(POLICY_NEWS_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT_S)
        except requests.exceptions.RequestException as e:
            # 예외 메시지에는 인증키가 든 요청 URL이 들어 있다 - 타입 이름만 남긴다.
            last_problem = type(e).__name__
        else:
            if resp.status_code < 500:
                # 4xx도 본문이 XML 에러 봉투라 파서가 코드와 메시지를 꺼내 올린다.
                return parse_policy_news_xml(resp.text)
            last_problem = f"HTTP {resp.status_code}"
        if attempt < max_attempts - 1:
            sleep_fn(delay)
            delay = min(delay * Settings.RETRY_EXPONENTIAL_BASE, Settings.MAX_RETRY_WAIT_SECONDS)
    raise PolicyBriefingFetchError(
        f"정책브리핑 API 요청 실패({last_problem}, {start:%Y%m%d}~{end:%Y%m%d}, {max_attempts}회 시도)"
    ) from None


# --------------------------------------------------------------------------- 수집

_BASE_COLUMNS = ("press_id", "raw_news_title", "raw_news_content", "raw_news_url",
                 "raw_news_created_at", "raw_news_crawled_at")
# 수집 런타임 브랜치(PR #8)의 마이그레이션 f87f7378672e·d48994e9d26e가 news_raw에 더하는 컬럼.
# 그 스키마의 본문 추출기는 raw_news_extract_status IS NULL인 행을 골라 원문 페이지를 다시
# 내려받아 본문을 덮어쓴다. API 본문(공공누리 텍스트만, 사진 캡션 제외)을 지키려면 이 행을
# 처음부터 추출 완료('ok')로 넣어야 한다. 컬럼이 없는 스키마(main)에서는 넣지 않는다.
EXTRACT_STATUS_COLUMN = "raw_news_extract_status"
EXTRACTED_AT_COLUMN = "raw_news_extracted_at"
CONTENT_SHA256_COLUMN = "raw_news_content_sha256"


def content_sha256(text: str) -> str:
    """본문 해시. 마이그레이션 d48994e9d26e(encode(sha256(convert_to(content, 'UTF8')), 'hex'))와
    그 브랜치 추출기의 content_sha256과 같은 값이다."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def news_raw_columns(cur) -> set:
    """현재 연결의 스키마에서 news_raw 컬럼 이름 집합 (수집 1회당 1번 조회)."""
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'news_raw'
        """
    )
    return {row[0] for row in cur.fetchall()}


def build_insert(columns: set) -> Tuple[str, str, Callable[[int, PolicyNewsRow, datetime], tuple]]:
    """스키마에 맞는 (INSERT 문, execute_values 템플릿, 행 -> 값 튜플 함수)를 만든다.

    - 추출 상태 컬럼이 있으면 'ok'와 now()를, 해시 컬럼이 있으면 본문 sha256을 채운다.
    - 해시 컬럼이 있는 스키마에는 'ok' 행끼리 본문 해시가 유일해야 하는 부분 unique 인덱스
      (uq_news_raw_content_sha256_ok)가 있다. ON CONFLICT (raw_news_url)로 대상을 좁히면
      본문이 같은 기사(URL만 다름) 하나 때문에 배치 전체가 UniqueViolation으로 롤백된다.
      그래서 그 스키마에서는 대상 없는 ON CONFLICT DO NOTHING으로 두 경우를 모두 건너뛴다.
    """
    names = list(_BASE_COLUMNS)
    placeholders = ["%s"] * len(_BASE_COLUMNS)
    if EXTRACT_STATUS_COLUMN in columns:
        names.append(EXTRACT_STATUS_COLUMN)
        placeholders.append("'ok'")
    if EXTRACTED_AT_COLUMN in columns:
        names.append(EXTRACTED_AT_COLUMN)
        placeholders.append("now()")
    with_hash = CONTENT_SHA256_COLUMN in columns
    if with_hash:
        names.append(CONTENT_SHA256_COLUMN)
        placeholders.append("%s")
    conflict = "ON CONFLICT DO NOTHING" if with_hash else "ON CONFLICT (raw_news_url) DO NOTHING"

    sql = (
        f"INSERT INTO news_raw ({', '.join(names)}) VALUES %s "
        f"{conflict} RETURNING raw_news_url"
    )
    template = f"({', '.join(placeholders)})"

    def values(press_id: int, row: PolicyNewsRow, crawled_at: datetime) -> tuple:
        base = (press_id, row.title, row.content, row.url, row.published_at, crawled_at)
        return base + (content_sha256(row.content),) if with_hash else base

    return sql, template, values


def _get_or_create_press_id(cur, press_name: str) -> int:
    cur.execute("SELECT press_id FROM press WHERE press_name = %s", (press_name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (press_name,))
    return cur.fetchone()[0]


def collect_policy_briefing(days: int = 3, now: Optional[datetime] = None, fetch: Optional[Callable] = None,
                            press_name: str = POLICY_BRIEFING_PRESS) -> Dict[str, object]:
    """최근 days일(오늘 포함)의 정책뉴스를 news_raw에 넣는다. 재실행 안전(ON CONFLICT DO NOTHING).
    skipped는 이미 있는 URL(그리고 본문 해시 컬럼이 있는 스키마에서는 이미 있는 본문)의 수다.

    본문은 API가 주므로 본문 추출(Stage2)을 거치지 않고 바로 저장한다. 인증키가 없으면
    아무것도 하지 않고 {"skipped": ...}를 돌려준다.
    """
    service_key = os.getenv(SERVICE_KEY_ENV, "").strip()
    if not service_key:
        logger.info(f"정책브리핑 수집 건너뜀: {SERVICE_KEY_ENV}가 설정되지 않음")
        return {"skipped": "no service key"}

    now = now or datetime.now(KST)
    fetch = fetch or fetch_policy_news
    today = now.astimezone(KST).date()
    items: List[PolicyNewsItem] = []
    for start, end in date_windows(today - timedelta(days=max(days, 1) - 1), today):
        items.extend(fetch(service_key, start, end))

    rows, stats = select_rows(items, now=now)
    stats.update({"inserted": 0, "skipped": 0})
    if not rows:
        logger.info(f"정책브리핑: 저장할 기사 없음 {stats}")
        return stats

    crawled_at = datetime.now()  # rss_collector와 같은 규칙(naive 로컬 시각, timestamp 컬럼)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql, template, values = build_insert(news_raw_columns(cur))
            press_id = _get_or_create_press_id(cur, press_name)
            inserted = execute_values(
                cur, sql, [values(press_id, r, crawled_at) for r in rows],
                template=template, fetch=True,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_connection(conn)

    stats["inserted"] = len(inserted)
    stats["skipped"] = len(rows) - len(inserted)
    logger.info(f"정책브리핑: {stats}")
    return stats


if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="정책브리핑 정책뉴스(공공누리 제1유형) 수집")
    parser.add_argument("--days", type=int, default=3, help="오늘 포함 최근 며칠 (API는 3일 단위로 나눠 호출)")
    args = parser.parse_args()
    print(json.dumps(collect_policy_briefing(days=args.days), ensure_ascii=False, default=str))
