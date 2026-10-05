"""crawler/policy_briefing.py가 실제 news_raw/press 스키마에 본문까지 넣고, 재실행해도
중복 없이 skipped로 집계되는지 확인한다 (API 호출은 합성 응답으로 대체)."""
import uuid
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def _xml(news_id):
    # 본문 해시 부분 unique 인덱스가 있으므로 실행마다 본문도 달라야 한다(중단된 이전 실행의 행과 충돌 방지)
    body = "<p>" + (f"합성 정책뉴스 {news_id} 문단입니다. " * 40) + "</p>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>0</resultCode><resultMsg>OK</resultMsg></header>
<body><NewsItem>
  <NewsItemId>{news_id}</NewsItemId><ContentsStatus>I</ContentsStatus><ApproveDate>09/24/2026 10:30:00</ApproveDate>
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


def _xml_items(*items):
    nodes = "".join(
        f"""<NewsItem>
  <NewsItemId>{news_id}</NewsItemId><ContentsStatus>I</ContentsStatus>
  <ApproveDate>09/24/2026 10:30:00</ApproveDate>
  <Title>합성 정책뉴스 {news_id}</Title><ContentsType>H</ContentsType>
  <DataContents><![CDATA[<p>{body}</p>]]></DataContents>
  <OriginalUrl>https://www.korea.kr/news/policyNewsView.do?newsId={news_id}</OriginalUrl>
  <KoglType>1</KoglType>
</NewsItem>"""
        for news_id, body in items
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>0</resultCode><resultMsg>OK</resultMsg></header>
<body>{nodes}<totalCount>{len(items)}</totalCount></body></response>"""


def test_collect_stores_rows_as_extracted_and_skips_same_body(database_url, pg_conn, monkeypatch):
    """Alembic head(f87f7378672e·d48994e9d26e 포함)의 news_raw에서: 정책브리핑 행은 추출 완료('ok')와
    본문 해시가 채워져 본문 추출기가 다시 내려받지 않고, 본문이 같은 두 번째 URL은 부분 unique
    인덱스(uq_news_raw_content_sha256_ok) 때문에 배치를 롤백시키지 않고 건너뛴다."""
    from crawler import policy_briefing as pb

    with pg_conn.cursor() as cur:
        columns = pb.news_raw_columns(cur)
        cur.execute("SELECT 1 FROM pg_indexes WHERE indexname = 'uq_news_raw_content_sha256_ok'")
        has_index = cur.fetchone() is not None
    # 이 컬럼·인덱스가 없는 DB(e725a62ffef1까지)에서는 수집기가 URL 충돌만 보는 INSERT로 내려간다 -
    # 그 경로는 단위 테스트 test_insert_on_pre_runtime_schema_writes_only_existing_columns가 본다
    assert {pb.EXTRACT_STATUS_COLUMN, pb.EXTRACTED_AT_COLUMN, pb.CONTENT_SHA256_COLUMN} <= columns
    assert has_index

    tag = uuid.uuid4().hex[:10]
    a, b, c = f"itA{tag}", f"itB{tag}", f"itC{tag}"
    same_body = f"합성 정책뉴스 공통 문단 {tag}. " * 30
    other_body = f"합성 정책뉴스 다른 문단 {tag}. " * 30
    urls = [f"https://www.korea.kr/news/policyNewsView.do?newsId={i}" for i in (a, b, c)]
    press_name = f"정책브리핑-itest-{uuid.uuid4().hex[:6]}"
    monkeypatch.setenv(pb.SERVICE_KEY_ENV, "itest-key")

    def fake_fetch(service_key, start, end):
        # b는 a와 본문이 같고 URL만 다르다(같은 기사가 두 주소로 나오는 경우)
        return pb.parse_policy_news_xml(_xml_items((a, same_body), (b, same_body), (c, other_body)))

    now = datetime(2026, 9, 26, 12, 0, tzinfo=KST)
    try:
        first = pb.collect_policy_briefing(days=1, now=now, fetch=fake_fetch, press_name=press_name)
        second = pb.collect_policy_briefing(days=1, now=now, fetch=fake_fetch, press_name=press_name)

        assert (first["inserted"], first["skipped"]) == (2, 1)
        assert (second["inserted"], second["skipped"]) == (0, 3)

        with pg_conn.cursor() as cur:
            cur.execute(
                """SELECT raw_news_url, raw_news_extract_status, raw_news_extracted_at IS NOT NULL,
                          raw_news_extract_attempts,
                          raw_news_content_sha256 = encode(sha256(convert_to(raw_news_content, 'UTF8')), 'hex')
                   FROM news_raw WHERE raw_news_url = ANY(%s) ORDER BY raw_news_url""",
                (urls,),
            )
            rows = cur.fetchall()
        assert [r[0] for r in rows] == sorted([urls[0], urls[2]])
        for _url, status, has_extracted_at, attempts, hash_matches_sql in rows:
            # 추출기(WHERE raw_news_extract_status IS NULL ...)가 다시 내려받지 않는 상태
            assert status == "ok"
            assert has_extracted_at
            assert attempts == 0
            # 파이썬 해시가 마이그레이션 d48994e9d26e의 SQL 식과 같은 값
            assert hash_matches_sql is True
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE raw_news_url = ANY(%s)", (urls,))
            cur.execute("DELETE FROM press WHERE press_name = %s", (press_name,))
