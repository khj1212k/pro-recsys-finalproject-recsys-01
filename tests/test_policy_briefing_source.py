# 정책브리핑(korea.kr) 정책뉴스 수집 (docs/adr/0023).
# korea.kr RSS는 2026-07-01에 중단됐고, 같은 콘텐츠를 공공데이터포털 Open API
# (문화체육관광부_정책브리핑_정책뉴스_API, 공공누리 제1유형)로 받는다.
# 아래 XML은 API 명세(swagger)의 필드 이름으로 만든 합성 응답이다 - 실제 기사 본문이 아니다.
import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from crawler import policy_briefing as pb

KST = timezone(timedelta(hours=9))

LONG_PARAGRAPH = "합성 문단입니다. " * 40  # 정제 후에도 DROP_LEN(350자)을 넘는 길이


def _item(news_id, kogl="1", url=None, contents=None, ctype="H", approve="09/24/2026 10:30:00",
          embargo="", title=None):
    url = f"https://www.korea.kr/news/policyNewsView.do?newsId={news_id}" if url is None else url
    contents = contents if contents is not None else f"<p>{LONG_PARAGRAPH}</p>"
    return f"""
    <NewsItem>
      <NewsItemId>{news_id}</NewsItemId>
      <ContentsStatus>I</ContentsStatus>
      <ModifyId>1</ModifyId>
      <ModifyDate>{approve}</ModifyDate>
      <ApproveDate>{approve}</ApproveDate>
      <EmbargoDate>{embargo}</EmbargoDate>
      <GroupingCode>policy</GroupingCode>
      <Title>{title or f"합성 정책뉴스 {news_id}"}</Title>
      <SubTitle1></SubTitle1>
      <ContentsType>{ctype}</ContentsType>
      <DataContents><![CDATA[{contents}]]></DataContents>
      <MinisterCode>합성부</MinisterCode>
      <OriginalUrl>{url}</OriginalUrl>
      <KoglType>{kogl}</KoglType>
    </NewsItem>"""


def _response(*items, code="0", msg="NORMAL SERVICE."):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<response>
  <header><resultCode>{code}</resultCode><resultMsg>{msg}</resultMsg></header>
  <body>{''.join(items)}<totalCount>{len(items)}</totalCount></body>
</response>"""


GATEWAY_ERROR = """<?xml version="1.0" encoding="UTF-8"?>
<OpenAPI_ServiceResponse>
<cmmMsgHeader>
  <errMsg>SERVICE_KEY_IS_NULL</errMsg>
  <returnAuthMsg>서비스 접근거부</returnAuthMsg>
  <returnReasonCode>20</returnReasonCode>
</cmmMsgHeader>
</OpenAPI_ServiceResponse>"""


# --------------------------------------------------------------------------- 파싱

def test_parse_reads_items_with_kst_approve_date():
    items = pb.parse_policy_news_xml(_response(_item("101"), _item("102", kogl="4")))

    assert [i.news_item_id for i in items] == ["101", "102"]
    first = items[0]
    assert first.title == "합성 정책뉴스 101"
    assert first.contents_type == "H"
    assert first.original_url.endswith("newsId=101")
    assert first.kogl_type == "1"
    assert first.approve_date == datetime(2026, 9, 24, 10, 30, tzinfo=KST)


def test_parse_raises_on_api_result_code_error():
    with pytest.raises(pb.PolicyBriefingAPIError) as exc:
        pb.parse_policy_news_xml(_response(code="98", msg="날짜범위 3일 초과"))
    assert exc.value.code == "98"


def test_parse_raises_on_gateway_error_envelope():
    with pytest.raises(pb.PolicyBriefingAPIError) as exc:
        pb.parse_policy_news_xml(GATEWAY_ERROR)
    assert exc.value.code == "20"
    assert "SERVICE_KEY_IS_NULL" in str(exc.value)


# --------------------------------------------------------------------------- 날짜 창

def test_date_windows_split_a_week_into_chunks_of_at_most_three_days():
    windows = pb.date_windows(date(2026, 9, 20), date(2026, 9, 26))

    assert windows == [
        (date(2026, 9, 20), date(2026, 9, 22)),
        (date(2026, 9, 23), date(2026, 9, 25)),
        (date(2026, 9, 26), date(2026, 9, 26)),
    ]


def test_date_windows_empty_when_start_after_end():
    assert pb.date_windows(date(2026, 9, 26), date(2026, 9, 25)) == []


# --------------------------------------------------------------------------- 공공누리 유형

@pytest.mark.parametrize("raw, expected", [
    ("1", 1), ("01", 1), ("제1유형", 1), ("KOGL1", 1), ("4", 4),
    ("", None), (None, None), ("1,4", None), ("5", None),
])
def test_normalize_kogl_type(raw, expected):
    assert pb.normalize_kogl_type(raw) == expected


# --------------------------------------------------------------------------- 본문 정제

def test_html_cleaner_drops_images_and_captions_and_keeps_words_intact():
    html = (
        "<p><b>정부</b>는 합성 정책을 발표했다.</p>"
        "<figure><img src='x.jpg' alt='사진 설명'/><figcaption>사진=합성통신</figcaption></figure>"
        "<p>둘째 문단<br/>줄바꿈 뒤 문장.</p>"
        "<script>var x = 1;</script>"
    )

    text = pb.clean_policy_news_html(html)

    assert text == "정부는 합성 정책을 발표했다. 둘째 문단 줄바꿈 뒤 문장."


def test_plain_text_contents_are_only_whitespace_normalized():
    item = pb.parse_policy_news_xml(_response(_item("7", ctype="T", contents="첫 줄\n\n  둘째   줄")))[0]

    assert pb.clean_policy_news_content(item) == "첫 줄 둘째 줄"


# --------------------------------------------------------------------------- 수집 대상 선별

def test_select_rows_keeps_only_kogl_type_1_items_with_url_and_enough_text():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=KST)
    items = pb.parse_policy_news_xml(_response(
        _item("1"),                                   # 채택
        _item("2", kogl="4"),                         # 공공누리 4유형 - 공개 부분집합 대상 아님
        _item("3", kogl=""),                          # 유형 표시 없음 - fail closed
        _item("4", url=""),                           # 원문 URL 없음
        _item("5", contents="<p>짧은 본문</p>"),       # 정제 후 너무 짧음
        _item("6", embargo="09/27/2026 09:00:00"),    # 엠바고 미해제
    ))

    rows, stats = pb.select_rows(items, now=now)

    assert [r.url for r in rows] == ["https://www.korea.kr/news/policyNewsView.do?newsId=1"]
    assert rows[0].title == "합성 정책뉴스 1"
    assert rows[0].published_at == datetime(2026, 9, 24, 10, 30, tzinfo=KST)
    assert "<p>" not in rows[0].content
    assert stats == {
        "fetched": 6, "kept": 1, "not_kogl_type_1": 2, "no_url": 1, "too_short_or_filtered": 1, "embargoed": 1,
    }


# --------------------------------------------------------------------------- HTTP

class _Resp:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


def test_fetch_sends_service_key_and_yyyymmdd_dates():
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return _Resp(_response(_item("1")))

    items = pb.fetch_policy_news("KEY", date(2026, 9, 24), date(2026, 9, 26), http_get=fake_get)

    assert len(items) == 1
    url, params, timeout = calls[0]
    assert url == pb.POLICY_NEWS_ENDPOINT
    assert params == {"serviceKey": "KEY", "startDate": "20260924", "endDate": "20260926"}
    assert timeout


def test_fetch_errors_never_echo_the_service_key():
    def failing_get(url, params=None, timeout=None):
        raise requests.exceptions.ConnectionError(f"Max retries exceeded with url: {url}?serviceKey={params['serviceKey']}")

    with pytest.raises(pb.PolicyBriefingFetchError) as exc:
        pb.fetch_policy_news("SECRET-KEY-123", date(2026, 9, 24), date(2026, 9, 24),
                             http_get=failing_get, max_attempts=2, sleep_fn=lambda s: None)

    assert "SECRET-KEY-123" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_fetch_retries_server_errors_then_succeeds():
    responses = [_Resp("upstream down", status_code=503), _Resp(_response(_item("1")))]
    sleeps = []

    items = pb.fetch_policy_news("KEY", date(2026, 9, 24), date(2026, 9, 24),
                                 http_get=lambda url, params=None, timeout=None: responses.pop(0),
                                 max_attempts=3, sleep_fn=sleeps.append)

    assert len(items) == 1
    assert len(sleeps) == 1


# --------------------------------------------------------------------------- 수집 실행

def test_collect_is_a_noop_without_a_service_key(monkeypatch):
    monkeypatch.delenv(pb.SERVICE_KEY_ENV, raising=False)
    with patch("crawler.policy_briefing.get_connection") as get_conn:
        stats = pb.collect_policy_briefing(days=3)

    assert stats["skipped"] == "no service key"
    get_conn.assert_not_called()


def test_collect_inserts_selected_rows_with_on_conflict_and_counts(monkeypatch):
    monkeypatch.setenv(pb.SERVICE_KEY_ENV, "KEY")
    now = datetime(2026, 9, 26, 12, 0, tzinfo=KST)
    fetched_windows = []

    def fake_fetch(service_key, start, end):
        fetched_windows.append((start, end))
        if start == date(2026, 9, 24):
            return pb.parse_policy_news_xml(_response(_item("1"), _item("2"), _item("3", kogl="3")))
        return []

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = (42,)  # press_id

    with patch("crawler.policy_briefing.get_connection", return_value=conn), \
         patch("crawler.policy_briefing.release_connection"), \
         patch("crawler.policy_briefing.execute_values",
               return_value=[("https://www.korea.kr/news/policyNewsView.do?newsId=1",)]) as ev:
        stats = pb.collect_policy_briefing(days=3, now=now, fetch=fake_fetch)

    assert fetched_windows == [(date(2026, 9, 24), date(2026, 9, 26))]
    assert stats["inserted"] == 1 and stats["skipped"] == 1
    assert stats["not_kogl_type_1"] == 1 and stats["fetched"] == 3
    sql, rows = ev.call_args[0][1], ev.call_args[0][2]
    assert "ON CONFLICT (raw_news_url) DO NOTHING" in sql
    assert {r[0] for r in rows} == {42}
    conn.commit.assert_called_once()


# --------------------------------------------------------------------------- 파이프라인 Stage1 연결

def test_stage1_keeps_rss_result_and_adds_policy_briefing_stats():
    from pipeline.stages import Stage1_RSSCollection

    with patch("crawler.rss_collector.collect_rss", return_value={"inserted": 3, "skipped": 1}), \
         patch("crawler.policy_briefing.collect_policy_briefing", return_value={"skipped": "no service key"}):
        result = Stage1_RSSCollection(settings=None).execute()

    assert result["inserted"] == 3 and result["skipped"] == 1
    assert result["policy_briefing"] == {"skipped": "no service key"}


def test_stage1_policy_briefing_failure_does_not_discard_rss_result():
    from pipeline.stages import Stage1_RSSCollection

    def boom():
        raise pb.PolicyBriefingAPIError("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR")

    with patch("crawler.rss_collector.collect_rss", return_value={"inserted": 3, "skipped": 1}), \
         patch("crawler.policy_briefing.collect_policy_briefing", side_effect=boom):
        result = Stage1_RSSCollection(settings=None).execute()

    assert result["inserted"] == 3
    assert "PolicyBriefingAPIError" in result["policy_briefing"]["error"]
