# 'rss_summary' 피드(동아일보 - VM 출발지의 본문 요청을 거절한다)는 본문을 받지 않고 RSS 요약을 본문 칸에 바로 넣는다.
# 요약 행도 임베딩·클러스터에 들어가므로 짧은 요약은 'dropped', 같은 요약은 'duplicate'로 넣는다(ADR 0026 2026-10-25).
# 지어낸 텍스트만 쓴다.
import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from config.retention import RETRYABLE_EXTRACT_STATUSES
from crawler.rss_collector import (
    RSS_SUMMARY_STATUS,
    SUMMARY_DEDUPE_STATUSES,
    collect_rss,
    rss_summary_text,
    summary_drop_reasons,
)

FEEDS = {"요약언론": ("rss_summary", "http://x/rss")}


class FakeEntry(dict):
    def __init__(self, link, title, published, summary=None):
        super().__init__(published=published)
        if summary is not None:
            self["summary"] = summary
        self.link = link
        self.title = title


def _recent():
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S %z")


def _summary(n):
    """350자를 넘는 서로 다른 요약(약 480자)."""
    return f"{n}번 지어낸 요약 문장이다. " * 30


def _run(feeds, entries, existing_hashes=(), inserted="all", existing_urls=(), sql_log=None):
    """collect_rss를 한 번 돌린다. 돌려주는 값: (INSERT SQL, INSERT 행, 해시 조회 인자 또는 None, collect_rss 결과).
    inserted="all"이면 INSERT가 모든 행을 새로 넣은 것으로(RETURNING이 모든 URL), 목록이면 그 URL만 넣은 것으로 흉내낸다.
    existing_urls는 DB에 이미 있는 URL(URL 조회가 돌려준다), existing_hashes는 같은 요약을 가진 행의 해시(해시 조회가 돌려준다).
    sql_log(목록)를 주면 cur.execute가 받은 SQL을 차례로 담는다. INSERT 호출이 없었으면 INSERT SQL과 행은 None이다."""
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    conn.cursor.return_value.__exit__.return_value = False
    cur.fetchone.return_value = (1,)
    last = {}

    def fake_execute(sql, params=None):
        last["sql"] = sql
        if sql_log is not None:
            sql_log.append(sql)

    def fake_fetchall():
        sql = last.get("sql", "")
        if "WHERE raw_news_url = ANY" in sql:
            return [(u,) for u in existing_urls]
        if "raw_news_content_sha256" in sql:
            return [(h,) for h in existing_hashes]
        return []

    cur.execute.side_effect = fake_execute
    cur.fetchall.side_effect = fake_fetchall
    feed = MagicMock()
    feed.entries = entries

    def fake_execute_values(_cur, _sql, rows, fetch=False):
        urls = [r[3] for r in rows] if inserted == "all" else list(inserted)
        return [(u,) for u in urls]

    with patch("crawler.rss_collector.get_connection", return_value=conn), \
         patch("crawler.rss_collector.release_connection"), \
         patch("crawler.rss_collector.Settings.RSS_FEEDS", feeds), \
         patch("crawler.rss_collector.parse_feed_with_retry", return_value=feed), \
         patch("crawler.rss_collector.execute_values", side_effect=fake_execute_values) as ev:
        result = collect_rss()
    lookups = [c for c in cur.execute.call_args_list if "raw_news_content_sha256" in str(c.args[0])]
    lookup_params = lookups[0].args[1] if lookups else None
    if not ev.mock_calls:
        return None, None, lookup_params, result
    _, args, _ = ev.mock_calls[0]
    return args[1], args[2], lookup_params, result


def _by_url(rows):
    return {r[3]: r for r in rows}


def test_summary_text_strips_tags_entities_and_whitespace():
    e = {"summary": "<p>지어낸&nbsp;요약 <b>첫 문장</b>.\n둘째 &amp; 셋째</p>"}
    assert rss_summary_text(e) == "지어낸 요약 첫 문장. 둘째 & 셋째"
    assert rss_summary_text({}) == ""


def test_summary_text_replaces_only_block_tags_with_a_space():
    # 블록 태그(br·p·div·li)는 단어 경계라 공백, 인라인 태그·이미지는 지운다(<b>대</b>통령이 "대 통령"이 되지 않는다)
    assert rss_summary_text({"summary": "첫 줄<br/>둘째 줄<BR>셋째</p><div>넷째</div><li>다섯째"}) == "첫 줄 둘째 줄 셋째 넷째 다섯째"
    assert rss_summary_text({"summary": "<b>대</b>통령이 <span class='x'>말했다</span>"}) == "대통령이 말했다"
    # 동아일보 요약은 맨 앞에 <img ... />가 붙어 온다
    img = '<img align="left" hspace="10" src="https://example.com/a.jpg" vspace="10" />지어낸 첫 문장이다.'
    assert rss_summary_text({"summary": img}) == "지어낸 첫 문장이다."
    # pre·link처럼 블록 태그 이름으로 시작하는 다른 태그를 블록으로 보지 않는다
    assert rss_summary_text({"summary": "앞<pre>중간</pre>뒤"}) == "앞중간뒤"


def test_summary_text_strips_tags_that_were_escaped_in_the_feed():
    # 엔티티를 푼 뒤에야 보이는 태그도 지운다
    assert rss_summary_text({"summary": "&lt;p&gt;지어낸 &lt;b&gt;요약&lt;/b&gt;&lt;/p&gt;"}) == "지어낸 요약"
    assert rss_summary_text({"description": "&lt;br&gt;둘째&lt;br/&gt;셋째"}) == "둘째 셋째"


def test_summary_text_keeps_angle_brackets_that_are_not_tags():
    assert rss_summary_text({"summary": "3 < 5 그리고 7 > 2 이다"}) == "3 < 5 그리고 7 > 2 이다"
    assert rss_summary_text({"summary": "A<-B 이고 C->D"}) == "A<-B 이고 C->D"


def test_summary_text_drops_html_comments():
    assert rss_summary_text({"summary": "앞<!-- 광고 --> 뒤"}) == "앞 뒤"


def test_rss_summary_feed_stores_the_summary_as_content_and_closes_extraction():
    summary = _summary(1)
    sql, rows, _, _ = _run(FEEDS, [FakeEntry("http://s.example/1", "제목", _recent(), summary=summary)])
    assert "raw_news_extract_status" in sql and "raw_news_content_sha256" in sql
    (pid, title, content, link, _dt, _crawled, status, digest, length, extracted_at), = rows
    assert content == rss_summary_text({"summary": summary})
    assert status == RSS_SUMMARY_STATUS
    assert digest == hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert length == len(content)
    assert extracted_at is not None


def test_empty_summary_is_closed_as_dropped_without_asking_the_origin_for_the_body():
    # 이 출발지는 본문 요청을 거절한다(ADR 0026 증거 10) - 요약이 없으면 본문 추출기에 맡기지 않고 닫는다
    _, rows, lookup, _ = _run(FEEDS, [FakeEntry("http://s.example/2", "제목", _recent(), summary="  "),
                                      FakeEntry("http://s.example/3", "제목", _recent())])
    assert lookup is None  # 걸러진 항목뿐이라 중복 조회도 하지 않는다
    for row in rows:
        (_, _, content, _, _, _, status, digest, length, extracted_at) = row
        assert (content, status, digest, length) == ("", "dropped", None, 0) and extracted_at is not None


def test_short_summary_is_dropped_like_a_short_body_and_a_long_one_is_stored():
    short = "지어낸 짧은 요약이다. " * 8  # 350자 미만
    assert len(short) < 350
    _, rows, _, result = _run(FEEDS, [
        FakeEntry("http://s.example/short", "제목", _recent(), summary=short),
        FakeEntry("http://s.example/long", "제목", _recent(), summary=_summary(2)),
    ])
    by_url = _by_url(rows)
    assert (by_url["http://s.example/short"][2], by_url["http://s.example/short"][6]) == ("", "dropped")
    assert by_url["http://s.example/long"][6] == RSS_SUMMARY_STATUS
    assert result["per_feed"]["요약언론"]["summary"] == {"stored": 1, "dropped": 1, "duplicate": 0}


def test_summary_drop_reasons_ignore_list_markers_but_keep_length_and_photo_rules():
    # 리스트성 마커 규칙은 700자 미만 본문용이라 길어야 500자인 요약에는 쓰지 않는다 - '실시간'이 든 정상 기사가 버려진다
    with_marker = ("지어낸 실시간 방송 소식이다. " * 25)
    assert 350 <= len(with_marker) < 700
    assert summary_drop_reasons(with_marker, "제목", "동아일보") == []
    assert summary_drop_reasons("짧다", "제목", "동아일보") == ["too_short<350"]
    assert "photo_title_short" in summary_drop_reasons("지어낸 설명. " * 10, "[포토] 제목", "동아일보")
    assert summary_drop_reasons("", "제목", "동아일보") == ["too_short<350"]


def test_same_summary_in_one_batch_is_stored_once_and_the_copy_is_a_duplicate():
    summary = _summary(3)
    digest = hashlib.sha256(rss_summary_text({"summary": summary}).encode("utf-8")).hexdigest()
    _, rows, _, result = _run(FEEDS, [
        FakeEntry("http://s.example/list/3", "제목", _recent(), summary=summary),
        FakeEntry("http://s.example/It/3", "제목", _recent(), summary=summary),  # 분류 뒤 다른 경로로 다시 나온 같은 기사
        FakeEntry("http://s.example/4", "제목", _recent(), summary=_summary(4)),
    ])
    by_url = _by_url(rows)
    first, copy = by_url["http://s.example/list/3"], by_url["http://s.example/It/3"]
    assert (first[2] != "", first[6], first[7]) == (True, RSS_SUMMARY_STATUS, digest)
    assert (copy[2], copy[6], copy[7], copy[8]) == ("", "duplicate", digest, 0)  # 본문 없이, 해시는 남긴다
    assert by_url["http://s.example/4"][6] == RSS_SUMMARY_STATUS
    assert result["per_feed"]["요약언론"]["summary"] == {"stored": 2, "dropped": 0, "duplicate": 1}


def test_summary_already_stored_as_ok_or_rss_summary_is_inserted_as_a_duplicate():
    summary = _summary(5)
    digest = hashlib.sha256(rss_summary_text({"summary": summary}).encode("utf-8")).hexdigest()
    _, rows, lookup, _ = _run(
        FEEDS,
        [FakeEntry("http://s.example/5", "제목", _recent(), summary=summary),
         FakeEntry("http://s.example/6", "제목", _recent(), summary=_summary(6))],
        existing_hashes=[digest],
    )
    by_url = _by_url(rows)
    assert (by_url["http://s.example/5"][2], by_url["http://s.example/5"][6]) == ("", "duplicate")
    assert by_url["http://s.example/6"][6] == RSS_SUMMARY_STATUS
    # 조회는 새 요약의 해시를 한 번에 물어보고, 'ok'와 'rss_summary' 행을 본다 - 상태마다 SELECT를 따로 내 UNION한다
    # (파라미터: 해시 목록, 상태, 해시 목록, 상태)
    hashes_ok, status_ok, hashes_summary, status_summary = lookup
    assert digest in hashes_ok and len(hashes_ok) == 2 and hashes_ok == hashes_summary
    assert {status_ok, status_summary} == set(SUMMARY_DEDUPE_STATUSES) == {"ok", "rss_summary"}


def test_summary_dedupe_lookup_asks_each_status_separately_so_each_partial_index_can_be_used():
    # 상태 = ANY(...) 한 줄로 물으면 부분 인덱스(uq_news_raw_content_sha256_ok: 'ok', ix_news_raw_content_sha256_rss_summary:
    # 'rss_summary')의 술어를 플래너가 증명하지 못해 news_raw 전체를 순차로 읽는다(VM EXPLAIN으로 확인)
    log = []
    _run(FEEDS, [FakeEntry("http://s.example/p1", "제목", _recent(), summary=_summary(30))], sql_log=log)
    (sql,) = [q for q in log if "raw_news_content_sha256" in q]
    assert sql.count("raw_news_extract_status = %s") == len(SUMMARY_DEDUPE_STATUSES) == 2
    assert sql.count("UNION") == 1
    assert "raw_news_extract_status = ANY" not in sql and "IN (" not in sql


def test_url_already_in_the_table_does_not_claim_the_summary_hash_of_a_new_url():
    # 배포 전 행(이미 있는 URL)은 ON CONFLICT DO NOTHING으로 건너뛰어져 요약을 받지 못한다. 그런 항목이 같은 요약의 해시를 먼저
    # 차지하면 새 URL이 'duplicate'(본문 '')로 들어가 그 기사는 어느 행에도 본문이 남지 않는다
    summary = _summary(31)
    _, rows, _, result = _run(
        FEEDS,
        [FakeEntry("http://s.example/old-url", "제목", _recent(), summary=summary),  # 이미 DB에 있다
         FakeEntry("http://s.example/new-url", "제목", _recent(), summary=summary)],  # 같은 요약의 새 URL
        existing_urls=["http://s.example/old-url"],
    )
    by_url = _by_url(rows)
    assert set(by_url) == {"http://s.example/new-url"}  # 이미 있는 URL의 행은 만들지 않는다
    assert by_url["http://s.example/new-url"][6] == RSS_SUMMARY_STATUS
    assert by_url["http://s.example/new-url"][2] != ""
    # 건너뜀 = 피드 항목 중 새로 들어가지 않은 것 - 이미 있어 뺀 항목도 센다
    assert result["inserted"] == 1 and result["skipped"] == 1
    assert result["per_feed"]["요약언론"]["summary"] == {"stored": 1, "dropped": 0, "duplicate": 0}


def test_when_every_entry_is_already_in_the_table_nothing_is_looked_up_or_inserted():
    log = []
    sql, rows, lookup, result = _run(
        FEEDS,
        [FakeEntry(f"http://s.example/old{i}", "제목", _recent(), summary=_summary(40 + i)) for i in range(3)],
        existing_urls=[f"http://s.example/old{i}" for i in range(3)],
        sql_log=log,
    )
    assert (sql, rows, lookup) == (None, None, None)  # 해시 조회도, INSERT도 보내지 않는다
    assert result["inserted"] == 0 and result["skipped"] == 3
    assert result["per_feed"]["요약언론"]["summary"] == {"stored": 0, "dropped": 0, "duplicate": 0}
    assert sum("WHERE raw_news_url = ANY" in q for q in log) == 1  # URL 조회는 한 번(피드당)


def test_direct_feeds_do_not_look_up_existing_urls():
    log = []
    _run({"직접언론": ("direct", "http://x/rss")}, [FakeEntry("http://d.example/9", "제목", _recent())], sql_log=log)
    assert not [q for q in log if "FROM news_raw" in q]


def test_summary_stats_count_only_rows_that_were_really_inserted():
    # 50개가 피드에 계속 남아 있어도 이미 들어간 행(ON CONFLICT로 건너뜀)은 세지 않는다
    entries = [FakeEntry(f"http://s.example/n{i}", "제목", _recent(), summary=_summary(10 + i)) for i in range(3)]
    _, _, _, result = _run(FEEDS, entries, inserted=["http://s.example/n1"])
    assert result["inserted"] == 1 and result["skipped"] == 2
    assert result["per_feed"]["요약언론"]["summary"] == {"stored": 1, "dropped": 0, "duplicate": 0}


def test_direct_feeds_are_unchanged_even_if_the_feed_has_a_summary():
    _, rows, lookup, result = _run({"직접언론": ("direct", "http://x/rss")},
                                   [FakeEntry("http://d.example/1", "제목", _recent(), summary="지어낸 요약")])
    (_, _, content, _, _, _, status, *_rest), = rows
    assert (content, status) == ("", None)
    assert lookup is None and "summary" not in result["per_feed"]["직접언론"]


def test_rss_summary_rows_are_not_picked_up_again_by_the_extractor():
    # 추출기 대상 = 상태 NULL 또는 재시도 가능한 상태(crawler/content_extractor/extractor.py, jobs/tasks/retention.py)
    assert RSS_SUMMARY_STATUS not in RETRYABLE_EXTRACT_STATUSES
    assert not {"dropped", "duplicate"} & set(RETRYABLE_EXTRACT_STATUSES)


def test_donga_uses_the_summary_strategy():
    from config.settings import Settings
    assert Settings.RSS_FEEDS["동아일보"][0] == "rss_summary"
