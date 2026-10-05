"""scripts/oci/merge_tier0_news_raw.sql(ADR 0026 결정 4, 런북 6.4)을 실제 Postgres에서 돌린다.

수집기 두 대(Tier 0 = 원본, A1 = 대상)를 테스트 DB 안의 스키마 두 개로 흉내 낸다. 각 스키마에
public.press / public.news_raw를 `LIKE ... INCLUDING ALL`로 복제하므로(체크 제약, URL UNIQUE,
'ok' 행의 본문 해시 부분 UNIQUE 포함) Alembic head의 스키마 그대로다. public.news_raw에는 아무것도
넣지 않는다 - 테이블 전체를 도는 다른 integration 테스트(isolated_news_raw)와 섞이지 않는다.

런북과 같은 경로를 탄다: 원본 스키마에서 런북 6.4의 COPY 쿼리로 CSV를 뽑고, 대상 스키마에서
`psql -v ON_ERROR_STOP=1 -1 -f merge_tier0_news_raw.sql < CSV`로 병합한다. 스키마는 PGOPTIONS의
search_path로 고른다(병합 SQL은 테이블 이름에 스키마를 붙이지 않는다).
"""
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MERGE_SQL = os.path.join(REPO_ROOT, "scripts", "oci", "merge_tier0_news_raw.sql")

# docs/runbook-hosting.md 6.4의 4단계, scripts/oci/merge_tier0_news_raw.sql 머리말과 같은 쿼리(열 순서 고정).
EXPORT_SQL = (
    "COPY (SELECT p.press_name, n.raw_news_title, n.raw_news_content, n.raw_news_url, "
    "n.raw_news_created_at, n.raw_news_crawled_at, n.raw_news_extract_status, n.raw_news_extracted_at, "
    "n.raw_news_extract_attempts, n.raw_news_content_sha256 "
    "FROM news_raw n JOIN press p USING (press_id) ORDER BY n.raw_news_id) "
    "TO STDOUT WITH (FORMAT csv, HEADER)"
)

NOTICE_RE = re.compile(r"target_before=(\d+) tier0_rows=(\d+) tier0_only=(\d+) expected=(\d+) after=(\d+)")
PSQL_SCRIPT_ERROR = 3  # ON_ERROR_STOP이 켜진 스크립트에서 오류가 났을 때의 psql 종료 코드

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


@pytest.fixture(scope="module")
def psql_bin():
    path = shutil.which("psql")
    if not path:
        pytest.skip("psql 클라이언트가 없어 병합 SQL 테스트를 건너뜁니다 (CI integration-test 잡은 설치돼 있는지 확인한다)")
    return path


@pytest.fixture
def tiers(pg_conn):
    """(원본 스키마, 대상 스키마). 테스트가 끝나면 둘 다 지운다."""
    suffix = uuid.uuid4().hex[:8]
    src, dst = f"tier0_src_{suffix}", f"a1_dst_{suffix}"
    with pg_conn.cursor() as cur:
        for schema in (src, dst):
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'CREATE TABLE "{schema}".press (LIKE public.press INCLUDING ALL)')
            cur.execute(f'CREATE TABLE "{schema}".news_raw (LIKE public.news_raw INCLUDING ALL)')
    try:
        yield src, dst
    finally:
        with pg_conn.cursor() as cur:
            for schema in (src, dst):
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _psql(psql_bin, database_url, schema, args, stdin=None):
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema},public", "PGCLIENTENCODING": "UTF8"}
    return subprocess.run(
        [psql_bin, "-X", "-v", "ON_ERROR_STOP=1", *args, database_url],
        input=stdin, capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )


def _export_csv(psql_bin, database_url, schema):
    done = _psql(psql_bin, database_url, schema, ["-c", EXPORT_SQL])
    assert done.returncode == 0, done.stderr
    return done.stdout


def _merge(psql_bin, database_url, schema, csv_text):
    return _psql(psql_bin, database_url, schema, ["-1", "-f", MERGE_SQL], stdin=csv_text)


def _add_press(pg_conn, schema, name):
    with pg_conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{schema}".press (press_name) VALUES (%s) RETURNING press_id', (name,))
        return cur.fetchone()[0]


def _add_news(pg_conn, schema, press_id, url, *, title, content, status, sha=None, attempts=1,
              crawled_at="2026-09-26 03:05:00", with_embedding=False):
    with pg_conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{schema}".news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, '
            "raw_news_created_at, raw_news_crawled_at, raw_news_extract_status, raw_news_extracted_at, "
            "raw_news_extract_attempts, raw_news_content_sha256) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (press_id, title, content, url, "2026-09-26 11:40:00+09", crawled_at, status,
             None if status is None else "2026-09-26 03:06:00+00", attempts, sha),
        )
        if with_embedding:
            cur.execute(
                f'UPDATE "{schema}".news_raw SET embedding_result = %s::vector WHERE raw_news_url = %s',
                ("[" + ",".join(["0.5"] * 1024) + "]", url),
            )


def _rows(pg_conn, schema):
    """대상 스키마의 news_raw를 URL -> 행(dict)으로. 언론사는 id가 아니라 이름으로 본다."""
    with pg_conn.cursor() as cur:
        cur.execute(
            f"SELECT n.raw_news_url, p.press_name, n.raw_news_title, n.raw_news_content, "
            f"n.raw_news_extract_status, n.raw_news_extract_attempts, n.raw_news_content_sha256, "
            f"n.raw_news_created_at, n.raw_news_crawled_at, n.embedding_result IS NOT NULL "
            f'FROM "{schema}".news_raw n LEFT JOIN "{schema}".press p USING (press_id)'
        )
        keys = ("press", "title", "content", "status", "attempts", "sha", "created_at", "crawled_at", "embedded")
        return {row[0]: dict(zip(keys, row[1:])) for row in cur.fetchall()}


def _seed_two_collectors(pg_conn, src, dst, tag):
    """두 수집기가 겹쳐 돈 상황. 반환값은 URL 이름표 -> URL.

    대상(A1에 복원한 Mac 덤프 역할): shared(ok, 임베딩 있음), only_dst(ok)
    원본(Tier 0): shared(같은 URL, 다른 제목), new_ok, same_body(대상 shared와 본문 해시가 같은 새 URL),
                  failed(fetch_failed, 3회 시도), pending(추출 전, 상태 NULL)
    언론사 id는 두 쪽에서 서로 다르게 만든다 - 병합은 이름으로 찾아야 한다.
    """
    u = {k: f"https://example.com/{tag}/{k}" for k in ("shared", "only_dst", "new_ok", "same_body", "failed", "pending")}
    dst_a = _add_press(pg_conn, dst, f"언론사A-{tag}")
    dst_b = _add_press(pg_conn, dst, f"언론사B-{tag}")
    src_b = _add_press(pg_conn, src, f"언론사B-{tag}")
    src_a = _add_press(pg_conn, src, f"언론사A-{tag}")
    assert (dst_a, dst_b) != (src_a, src_b)

    _add_news(pg_conn, dst, dst_a, u["shared"], title="대상 제목", content="본문 A", status="ok", sha=SHA_A,
              with_embedding=True)
    _add_news(pg_conn, dst, dst_b, u["only_dst"], title="대상에만", content="본문 B", status="ok", sha=SHA_B)

    _add_news(pg_conn, src, src_a, u["shared"], title="원본 제목", content="본문 A 수정", status="ok", sha="d" * 64)
    _add_news(pg_conn, src, src_b, u["new_ok"], title="새 기사", content='본문 C, "따옴표"와\n줄바꿈', status="ok",
              sha=SHA_C, crawled_at="2026-09-26 08:05:00")
    _add_news(pg_conn, src, src_a, u["same_body"], title="같은 본문 다른 URL", content="본문 A", status="ok",
              sha=SHA_A, crawled_at="2026-09-26 08:05:01")
    _add_news(pg_conn, src, src_b, u["failed"], title="추출 실패", content="", status="fetch_failed", attempts=3,
              crawled_at="2026-09-26 08:05:02")
    _add_news(pg_conn, src, src_a, u["pending"], title="추출 전", content="", status=None, attempts=0,
              crawled_at="2026-09-26 08:05:03")
    return u


def test_merge_adds_only_new_urls_and_keeps_target_rows(database_url, pg_conn, psql_bin, tiers):
    src, dst = tiers
    tag = uuid.uuid4().hex[:8]
    u = _seed_two_collectors(pg_conn, src, dst, tag)

    done = _merge(psql_bin, database_url, dst, _export_csv(psql_bin, database_url, src))

    assert done.returncode == 0, done.stderr
    notice = NOTICE_RE.search(done.stderr)
    assert notice, done.stderr
    assert tuple(map(int, notice.groups())) == (2, 5, 4, 6, 6)

    rows = _rows(pg_conn, dst)
    assert set(rows) == set(u.values())

    # 같은 URL: 대상 행(임베딩 있음)이 그대로 남는다.
    assert rows[u["shared"]]["title"] == "대상 제목"
    assert rows[u["shared"]]["content"] == "본문 A"
    assert rows[u["shared"]]["embedded"] is True
    assert rows[u["only_dst"]]["status"] == "ok"

    # 새 URL: 본문(따옴표·줄바꿈 포함)·해시·시각이 CSV 왕복 뒤에도 같고, 언론사는 이름으로 매핑된다.
    new_ok = rows[u["new_ok"]]
    assert (new_ok["status"], new_ok["sha"], new_ok["press"]) == ("ok", SHA_C, f"언론사B-{tag}")
    assert new_ok["content"] == '본문 C, "따옴표"와\n줄바꿈'
    assert new_ok["embedded"] is False
    assert new_ok["created_at"] == datetime(2026, 9, 26, 2, 40, tzinfo=timezone.utc)  # 11:40 KST
    assert new_ok["crawled_at"] == datetime(2026, 9, 26, 8, 5)  # 시간대 없는 컬럼(UTC로 기록)

    # 새 URL인데 본문 해시가 대상의 ok 행과 같다: duplicate로 넣고 본문을 비운다(해시는 남긴다).
    same_body = rows[u["same_body"]]
    assert (same_body["status"], same_body["content"], same_body["sha"]) == ("duplicate", "", SHA_A)
    assert same_body["press"] == f"언론사A-{tag}"

    # 추출 실패 행은 시도 횟수를, 추출 전 행은 NULL 상태를 그대로 가져간다(빈 본문은 NULL이 아니라 '').
    assert (rows[u["failed"]]["status"], rows[u["failed"]]["attempts"], rows[u["failed"]]["content"]) == (
        "fetch_failed", 3, "")
    assert (rows[u["pending"]]["status"], rows[u["pending"]]["attempts"], rows[u["pending"]]["content"]) == (
        None, 0, "")


def test_merge_is_idempotent(database_url, pg_conn, psql_bin, tiers):
    src, dst = tiers
    _seed_two_collectors(pg_conn, src, dst, uuid.uuid4().hex[:8])
    csv_text = _export_csv(psql_bin, database_url, src)

    first = _merge(psql_bin, database_url, dst, csv_text)
    assert first.returncode == 0, first.stderr
    before = _rows(pg_conn, dst)

    second = _merge(psql_bin, database_url, dst, csv_text)

    assert second.returncode == 0, second.stderr
    notice = NOTICE_RE.search(second.stderr)
    assert notice, second.stderr
    assert tuple(map(int, notice.groups())) == (6, 5, 0, 6, 6)
    assert _rows(pg_conn, dst) == before


def test_merge_rolls_back_when_a_press_is_missing_in_target(database_url, pg_conn, psql_bin, tiers):
    src, dst = tiers
    tag = uuid.uuid4().hex[:8]
    _seed_two_collectors(pg_conn, src, dst, tag)
    only_src = _add_press(pg_conn, src, f"원본에만 있는 언론사-{tag}")
    _add_news(pg_conn, src, only_src, f"https://example.com/{tag}/unmapped", title="매핑 없음", content="본문 E",
              status="ok", sha="e" * 64)
    before = _rows(pg_conn, dst)

    done = _merge(psql_bin, database_url, dst, _export_csv(psql_bin, database_url, src))

    assert done.returncode == PSQL_SCRIPT_ERROR, (done.returncode, done.stderr)
    assert "대상 press 테이블에 없다" in done.stderr
    assert _rows(pg_conn, dst) == before  # 매핑되는 행도 하나도 들어가지 않는다(단일 트랜잭션)


def test_merge_rolls_back_when_target_press_name_is_ambiguous(database_url, pg_conn, psql_bin, tiers):
    src, dst = tiers
    tag = uuid.uuid4().hex[:8]
    _seed_two_collectors(pg_conn, src, dst, tag)
    _add_press(pg_conn, dst, f"언론사B-{tag}")  # 같은 이름이 대상에 두 번
    before = _rows(pg_conn, dst)

    done = _merge(psql_bin, database_url, dst, _export_csv(psql_bin, database_url, src))

    assert done.returncode == PSQL_SCRIPT_ERROR, (done.returncode, done.stderr)
    assert "둘 이상 있다" in done.stderr
    assert _rows(pg_conn, dst) == before
