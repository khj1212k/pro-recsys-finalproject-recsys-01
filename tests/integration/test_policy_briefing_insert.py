"""crawler/policy_briefing.py가 실제 news_raw/press 스키마에 본문까지 넣고, 재실행해도
중복 없이 skipped로 집계되는지 확인한다 (API 호출은 합성 응답으로 대체)."""
import uuid
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def _xml(news_id):
    body = "<p>" + ("합성 정책뉴스 문단입니다. " * 40) + "</p>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>0</resultCode><resultMsg>OK</resultMsg></header>
<body><NewsItem>
  <NewsItemId>{news_id}</NewsItemId><ApproveDate>09/24/2026 10:30:00</ApproveDate>
  <Title>합성 정책뉴스 {news_id}</Title><ContentsType>H</ContentsType>
  <DataContents><![CDATA[{body}]]></DataContents>
  <OriginalUrl>https://www.korea.kr/news/policyNewsView.do?newsId={news_id}</OriginalUrl>
  <KoglType>1</KoglType>
</NewsItem><totalCount>1</totalCount></body></response>"""


def test_collect_policy_briefing_inserts_body_and_is_idempotent(database_url, pg_conn, monkeypatch):
    from crawler import policy_briefing as pb

    news_id = f"itest{uuid.uuid4().hex[:10]}"
    url = f"https://www.korea.kr/news/policyNewsView.do?newsId={news_id}"
    press_name = f"정책브리핑-itest-{uuid.uuid4().hex[:6]}"
    monkeypatch.setenv(pb.SERVICE_KEY_ENV, "itest-key")

    def fake_fetch(service_key, start, end):
        return pb.parse_policy_news_xml(_xml(news_id))

    now = datetime(2026, 9, 26, 12, 0, tzinfo=KST)
    try:
        first = pb.collect_policy_briefing(days=1, now=now, fetch=fake_fetch, press_name=press_name)
        second = pb.collect_policy_briefing(days=1, now=now, fetch=fake_fetch, press_name=press_name)

        assert (first["inserted"], first["skipped"]) == (1, 0)
        assert (second["inserted"], second["skipped"]) == (0, 1)

        with pg_conn.cursor() as cur:
            cur.execute(
                """SELECT P.press_name, N.raw_news_title, length(N.raw_news_content), N.raw_news_created_at
                   FROM news_raw N JOIN press P ON P.press_id = N.press_id
                   WHERE N.raw_news_url = %s""",
                (url,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        got_press, title, content_len, created_at = rows[0]
        assert got_press == press_name
        assert title == f"합성 정책뉴스 {news_id}"
        assert content_len > 350
        assert created_at == datetime(2026, 9, 24, 10, 30, tzinfo=KST)
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE raw_news_url = %s", (url,))
            cur.execute("DELETE FROM press WHERE press_name = %s", (press_name,))
