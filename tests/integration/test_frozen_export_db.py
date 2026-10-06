"""동결 반출(evaluation/llm/frozen_export.py)과 파일 대역 로더(evaluation/llm/e0_store.py)를 실제 Postgres에서 확인한다.

Alembic head까지 올린 테스트 DB 안에 스키마를 하나 만들고 `public.press` / `public.news_raw`를
`LIKE ... INCLUDING ALL`로 복제해 합성 행을 넣는다(public.news_raw에는 아무것도 넣지 않는다 - 테이블 전체를 도는
다른 integration 테스트와 섞이지 않는다). 반출 SQL은 테이블 이름에 스키마를 붙이지 않으므로 search_path로 고른다.

확인하는 것:
- 읽기 전용: 반출 트랜잭션 안의 쓰기(UPDATE·INSERT·DELETE·nextval)가 서버에서 거절되고, 반출 뒤 테이블이 그대로다.
- 스냅숏: 반출 도중 다른 세션이 커밋한 행이 섞이지 않는다. 문장 시간 제한이 실제로 걸린다.
- 매니페스트: 창 안의 ok 행만, 본문은 글자 그대로, 해시는 DB의 SQL 식과 같은 값, Alembic 리비전, 만료일.
- psql 경로(런북의 경로)와 연결 경로가 같은 행을 낸다.
- 임베딩 팩: DB의 벡터로 만든 팩은 통과하고, 어긋난 팩은 사유와 함께 거절된다.
- 파일 대역 로더가 같은 DB 행에 대해 운영 `NewsClusterer._load_data_from_db`와 같은 dict를 낸다.

기사 문장은 전부 지어낸 것이다.
"""
import hashlib
import io
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import numpy as np
import psycopg2
import psycopg2.errors
import pytest

from evaluation.llm import e0_store as e0
from evaluation.llm import embedding_pack as ep
from evaluation.llm import frozen_export as fx

WINDOW = fx.Window.from_iso("2026-10-02T21:00:00", "2026-10-05T21:00:00")
CODE = {"git_sha": "c" * 40, "dirty": False}
READ_AT = datetime(2026, 10, 7, tzinfo=timezone.utc)  # 만료 전의 "지금"
TRICKY = (
    "가람시청은 새 도서관 계획을 내놓았다. \"따옴표\"와 역슬래시 \\ 가 있다.\n"
    "둘째 줄에는 탭\t과 \\N 과 \\. 이 있고\r\n셋째 줄에는 줄 구분 문자   와 그림 문자 📚, 한자 漢字가 있다."
)
SHA_SQL = "encode(sha256(convert_to(%s, 'UTF8')), 'hex')"  # 마이그레이션 d48994e9d26e와 같은 식


@pytest.fixture
def schema(pg_conn):
    """합성 행을 담을 스키마. press·news_raw는 Alembic head의 정의 그대로다(제약·인덱스 포함)."""
    name = f"frozen_export_{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'CREATE TABLE "{name}".press (LIKE public.press INCLUDING ALL)')
        cur.execute(f'CREATE TABLE "{name}".news_raw (LIKE public.news_raw INCLUDING ALL)')
    try:
        yield name
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def _connect(database_url, schema):
    return psycopg2.connect(database_url, options=f"-c search_path={schema},public")


@pytest.fixture
def conn(database_url, schema):
    connection = _connect(database_url, schema)
    try:
        yield connection
    finally:
        connection.close()


def _press(pg_conn, schema, name):
    with pg_conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{schema}".press (press_name) VALUES (%s) RETURNING press_id', (name,))
        return cur.fetchone()[0]


def _news(pg_conn, schema, press_id, slug, *, crawled, status="ok", body=None, created="2026-10-03 09:30:00+09",
          extracted="2026-10-03 01:05:00+00", vector=None):
    body = f"합성 본문 {slug}. 관계자는 계획을 차례로 진행한다고 밝혔다." if body is None else body
    with pg_conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO "{schema}".news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, '
            "raw_news_created_at, raw_news_crawled_at, raw_news_extract_status, raw_news_extracted_at, "
            "raw_news_extract_attempts, raw_news_content_sha256) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, CASE WHEN %s = 'ok' THEN {SHA_SQL} END) "
            "RETURNING raw_news_id",
            (press_id, f"합성 제목 {slug}", body, f"https://news.example/{schema}/{slug}", created, crawled,
             status, extracted, status, body),
        )
        row_id = cur.fetchone()[0]
        if vector is not None:
            literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
            cur.execute(f'UPDATE "{schema}".news_raw SET embedding_result = %s::vector WHERE raw_news_id = %s',
                        (literal, row_id))
    return row_id


@pytest.fixture
def seeded(pg_conn, schema):
    """창 [10-02 21:00, 10-05 21:00) UTC를 기준으로 안팎의 행을 넣는다. 돌려주는 것: 이름 -> raw_news_id."""
    daily = _press(pg_conn, schema, "가람일보")
    policy = _press(pg_conn, schema, "정책브리핑")
    ids = {
        "at_start": _news(pg_conn, schema, daily, "at-start", crawled="2026-10-02 21:00:00"),
        "tricky": _news(pg_conn, schema, daily, "tricky", crawled="2026-10-03 12:34:56.789012", body=TRICKY),
        "no_dates": _news(pg_conn, schema, daily, "no-dates", crawled="2026-10-04 00:00:00", created=None,
                          extracted=None),
        "policy": _news(pg_conn, schema, policy, "policy", crawled="2026-10-04 06:00:00"),
        "before_end": _news(pg_conn, schema, daily, "before-end", crawled="2026-10-05 20:59:59.999999"),
        # 아래는 반출되면 안 되는 행
        "at_end": _news(pg_conn, schema, daily, "at-end", crawled="2026-10-05 21:00:00"),
        "before_start": _news(pg_conn, schema, daily, "before-start", crawled="2026-10-02 20:59:59.999999"),
        "dropped": _news(pg_conn, schema, daily, "dropped", crawled="2026-10-03 03:00:00", status="dropped", body=""),
        "failed": _news(pg_conn, schema, daily, "failed", crawled="2026-10-03 04:00:00", status="fetch_failed", body=""),
        "pending": _news(pg_conn, schema, daily, "pending", crawled="2026-10-03 05:00:00", status=None, body="",
                         extracted=None),
    }
    return ids


IN_WINDOW = ("at_start", "tricky", "no_dates", "policy", "before_end")


def _table_fingerprint(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT count(*), md5(string_agg(n::text, \'|\' ORDER BY n.raw_news_id)) FROM "{schema}".news_raw n')
        return cur.fetchone()


# ---------------------------------------------------------------- 매니페스트와 본문


def test_export_writes_exactly_the_ok_rows_of_the_window_and_leaves_the_table_untouched(
        pg_conn, conn, schema, seeded, tmp_path):
    before = _table_fingerprint(pg_conn, schema)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        (revision,) = cur.fetchone()

    summary = fx.export_news_raw(WINDOW, root=tmp_path / "data" / "exports", conn=conn, code=CODE)

    assert _table_fingerprint(pg_conn, schema) == before
    assert conn.autocommit is False and conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE
    loaded = fx.load_export(summary["dir"], now=READ_AT)
    by_id = {r["id"]: r for r in loaded.rows}
    assert sorted(by_id) == sorted(seeded[name] for name in IN_WINDOW)
    assert loaded.ids == sorted(by_id)

    tricky = by_id[seeded["tricky"]]
    assert tricky["body"] == TRICKY  # COPY·json을 거쳐도 글자 그대로다
    assert tricky["content_sha256"] == hashlib.sha256(TRICKY.encode("utf-8")).hexdigest()
    assert tricky["crawled_at"] == "2026-10-03T12:34:56.789012Z"
    assert tricky["created_at"] == "2026-10-03T00:30:00.000000Z"  # +09:00으로 넣은 값이 UTC로 나온다
    assert tricky["body_expires_at"] == "2026-11-02T12:34:56.789012Z"
    assert tricky["press"] == "가람일보" and tricky["title"] == "합성 제목 tricky"
    assert by_id[seeded["no_dates"]]["created_at"] is None and by_id[seeded["no_dates"]]["extracted_at"] is None
    assert by_id[seeded["policy"]]["redistributable"] is True and by_id[seeded["policy"]]["body_expires_at"] is None

    identity = loaded.identity
    assert identity["rows"] == 5 and identity["by_press"] == {"가람일보": 4, "정책브리핑": 1}
    assert identity["source"]["alembic_revision"] == revision
    assert identity["source"]["transaction_read_only"] == "on"
    assert identity["source"]["transaction_isolation"] == "repeatable read"
    assert identity["source"]["statement_timeout"] == "2min"
    assert identity["db_hash_check"] == {"mismatch": 0, "missing": 0, "ids": []}  # 파이썬 해시 = DB의 SQL 식
    assert identity["retention"]["first_body_expiry_utc"] == "2026-11-01T21:00:00.000000Z"
    assert identity["retention"]["exempt_rows"] == 1
    assert identity["code"] == CODE
    snapshot = datetime.strptime(identity["source"]["snapshot_at_utc"], "%Y-%m-%dT%H:%M:%S.%fZ")
    assert abs(snapshot - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(minutes=5)
    assert summary["identity_sha256"] == loaded.identity_sha256 and summary["rows"] == 5


def test_export_reports_rows_whose_stored_hash_does_not_match_the_body(pg_conn, conn, schema, seeded, tmp_path):
    with pg_conn.cursor() as cur:
        cur.execute(f'UPDATE "{schema}".news_raw SET raw_news_content_sha256 = %s WHERE raw_news_id = %s',
                    ("f" * 64, seeded["at_start"]))
        cur.execute(f'UPDATE "{schema}".news_raw SET raw_news_content_sha256 = NULL WHERE raw_news_id = %s',
                    (seeded["before_end"],))
    summary = fx.export_news_raw(WINDOW, root=tmp_path / "data" / "exports", conn=conn, code=CODE)
    assert summary["db_hash_check"] == {"mismatch": 1, "missing": 1}


# ---------------------------------------------------------------- 읽기 전용, 스냅숏, 시간 제한


@pytest.mark.parametrize("statement", [
    "UPDATE news_raw SET raw_news_title = 'x'",
    "INSERT INTO press (press_name) VALUES ('x')",
    "DELETE FROM news_raw",
    "SELECT nextval(pg_get_serial_sequence('public.news_raw', 'raw_news_id'))",
    "CREATE TABLE frozen_export_should_not_exist (x int)",
])
def test_writes_inside_the_export_transaction_are_refused_by_the_server(pg_conn, conn, schema, seeded, statement):
    before = _table_fingerprint(pg_conn, schema)
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        with fx.read_only_snapshot(conn) as cur:
            cur.execute("SELECT current_setting('transaction_read_only')")
            assert cur.fetchone() == ("on",)
            cur.execute(statement)
    assert _table_fingerprint(pg_conn, schema) == before
    assert conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE  # ROLLBACK까지 끝났다


def test_rows_committed_by_another_session_during_the_export_are_not_mixed_in(pg_conn, conn, schema, seeded):
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT press_id FROM "{schema}".press WHERE press_name = %s', ("가람일보",))
        (press_id,) = cur.fetchone()
    header_sql, rows_sql, trailer_sql = fx.copy_statements(WINDOW)
    buffer = io.BytesIO()
    with fx.read_only_snapshot(conn) as cur:
        cur.copy_expert(header_sql, buffer)  # 첫 문장에서 스냅숏이 잡힌다
        late = _news(pg_conn, schema, press_id, "late", crawled="2026-10-04 12:00:00")  # 다른 세션, 자동 커밋
        cur.copy_expert(rows_sql, buffer)
        cur.copy_expert(trailer_sql, buffer)
    raw = fx.parse_copy_stream(buffer.getvalue().split(b"\n"))
    assert len(raw.rows) == raw.trailer["rows"] == 5
    assert late not in {r["id"] for r in raw.rows}


def test_statement_timeout_cancels_a_slow_statement_and_the_connection_stays_usable(conn, schema, seeded):
    with pytest.raises(psycopg2.errors.QueryCanceled):
        with fx.read_only_snapshot(conn, statement_timeout_s=1) as cur:
            cur.execute("SELECT pg_sleep(5)")
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)
    conn.rollback()


# ---------------------------------------------------------------- psql 경로 (런북의 경로)


@pytest.fixture(scope="module")
def psql_bin():
    path = shutil.which("psql")
    if not path:
        pytest.skip("psql 클라이언트가 없어 psql 경로 테스트를 건너뜁니다 (CI integration-test 잡은 설치돼 있는지 확인한다)")
    return path


def _psql_command(psql_bin, database_url, schema):
    separator = "&" if "?" in database_url else "?"
    return [psql_bin, "-X", f"{database_url}{separator}options={quote(f'-c search_path={schema},public')}"]


def test_psql_path_exports_the_same_rows_as_the_connection_path(pg_conn, conn, schema, seeded, tmp_path,
                                                                 database_url, psql_bin):
    before = _table_fingerprint(pg_conn, schema)
    via_conn = fx.export_news_raw(WINDOW, root=tmp_path / "a" / "data" / "exports", conn=conn, code=CODE)
    via_psql = fx.export_news_raw(WINDOW, root=tmp_path / "b" / "data" / "exports",
                                  psql_command=_psql_command(psql_bin, database_url, schema), code=CODE)

    assert _table_fingerprint(pg_conn, schema) == before
    first = fx.load_export(via_conn["dir"], now=READ_AT)
    second = fx.load_export(via_psql["dir"], now=READ_AT)
    assert second.identity["rows_sha256"] == first.identity["rows_sha256"]
    assert [r["body"] for r in second.rows] == [r["body"] for r in first.rows]
    assert second.identity["source"]["transaction_read_only"] == "on"
    assert second.identity["source"]["transaction_isolation"] == "repeatable read"


def test_a_write_added_to_the_psql_script_makes_psql_fail_and_changes_nothing(pg_conn, schema, seeded,
                                                                              database_url, psql_bin):
    before = _table_fingerprint(pg_conn, schema)
    script = fx.build_psql_script(WINDOW).replace("ROLLBACK;", "UPDATE news_raw SET raw_news_title = 'x';\nROLLBACK;")
    done = subprocess.run(_psql_command(psql_bin, database_url, schema), input=script.encode("utf-8"),
                          capture_output=True, timeout=120)
    assert done.returncode == 3  # ON_ERROR_STOP이 켜진 스크립트의 오류
    assert b"read-only transaction" in done.stderr
    assert _table_fingerprint(pg_conn, schema) == before


def test_cli_exports_with_a_dsn_taken_from_the_environment(schema, seeded, tmp_path, database_url, monkeypatch, capsys):
    separator = "&" if "?" in database_url else "?"
    monkeypatch.setenv("FROZEN_EXPORT_TEST_DSN",
                       f"{database_url}{separator}options={quote(f'-c search_path={schema},public')}")
    code = fx.main(["export", "--start-utc", "2026-10-02T21:00:00", "--end-utc", "2026-10-05T21:00:00",
                    "--root", str(tmp_path / "data" / "exports"), "--any-minute",
                    "--dsn-env", "FROZEN_EXPORT_TEST_DSN"])
    shown = capsys.readouterr()
    assert code == 0, shown.err
    summary = json.loads(shown.out)
    assert summary["rows"] == 5
    assert "가람시청" not in shown.out + shown.err and "합성 제목" not in shown.out + shown.err


# ---------------------------------------------------------------- 임베딩 팩과 파일 대역 로더


def _unit_vectors(n, seed):
    rng = np.random.default_rng(seed)
    vectors = rng.normal(size=(n, ep.EMBEDDING_DIM)).astype(np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


@pytest.fixture
def recent(pg_conn, schema):
    """지금 기준 1~20시간 전에 수집된 임베딩 있는 ok 행 8건, 그리고 운영 쿼리가 빼야 하는 행들."""
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    press_id = _press(pg_conn, schema, "누리신문")
    vectors = _unit_vectors(12, seed=5)

    def at(hours):
        return (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    inside = [_news(pg_conn, schema, press_id, f"recent-{k}", crawled=at(1 + 2.5 * k), vector=vectors[k],
                    body=TRICKY + f" 일련번호 {k}") for k in range(8)]
    _news(pg_conn, schema, press_id, "old-30h", crawled=at(30), vector=vectors[8])        # lookback 밖
    _news(pg_conn, schema, press_id, "no-vector", crawled=at(3))                          # 임베딩 없음
    _news(pg_conn, schema, press_id, "dropped-recent", crawled=at(4), status="dropped", body="")
    return {"now": now, "inside": inside}


def _pack_from_database(pg_conn, schema, export, directory, **over):
    """원격 잡이 한 일을 흉내 낸다: 반출본의 id마다 벡터를 갖다 붙인다. 벡터는 DB에 넣어 둔 값을 읽는다."""
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT raw_news_id, embedding_result::text FROM "{schema}".news_raw '
                    "WHERE raw_news_id = ANY(%s) ORDER BY raw_news_id", (export.ids,))
        stored = {row_id: np.array(json.loads(text), dtype=np.float32) for row_id, text in cur.fetchall()}
    args = dict(ids=export.ids, vectors=np.vstack([stored[i] for i in export.ids]),
                content_sha256=[r["content_sha256"] for r in export.rows],
                export_identity_sha256=export.identity_sha256, model=dict(ep.EXPECTED_MODEL), dtype="float32")
    args.update(over)
    ep.write_embedding_pack(directory, **args)
    return directory


def _recent_export(pg_conn, conn, schema, recent, tmp_path):
    """최근 48시간을 반출한다. 실험에서는 반출한 ok 행 전부에 임베딩을 계산하므로, 임베딩이 없는 채 남겨 둔
    행("아직 임베딩되지 않은 기사"를 흉내 낸 것)은 반출 전에 지운다."""
    with pg_conn.cursor() as cur:
        cur.execute(f'DELETE FROM "{schema}".news_raw WHERE raw_news_url LIKE %s', ("%/no-vector",))
    window = fx.Window(recent["now"] - timedelta(hours=48), recent["now"] + timedelta(hours=1))
    summary = fx.export_news_raw(window, root=tmp_path / "data" / "exports", conn=conn, code=CODE)
    return fx.load_export(summary["dir"])


def test_pack_built_for_the_database_export_validates_and_wrong_packs_are_refused(pg_conn, conn, schema, recent,
                                                                                  tmp_path):
    export = _recent_export(pg_conn, conn, schema, recent, tmp_path)
    assert len(export.rows) == 9  # 최근 8건 + 30시간 전 1건

    good = ep.load_embedding_pack(_pack_from_database(pg_conn, schema, export, tmp_path / "good"), export)
    assert good.ids.tolist() == export.ids and good.vectors.shape == (9, 1024)

    hashes = [r["content_sha256"] for r in export.rows]
    cases = {
        "ids": dict(ids=export.ids[1:], vectors=good.vectors[1:], content_sha256=hashes[1:]),
        "content_sha256": dict(content_sha256=["0" * 64] + hashes[1:]),
        "dim": dict(vectors=good.vectors[:, :512] / np.linalg.norm(good.vectors[:, :512], axis=1, keepdims=True)),
        "norm": dict(vectors=good.vectors * 1.5),
        "export_identity": dict(export_identity_sha256="e" * 64),
        "model": dict(model={**ep.EXPECTED_MODEL, "name": "other/model"}),
    }
    for expected_code, over in cases.items():
        directory = _pack_from_database(pg_conn, schema, export, tmp_path / f"bad-{expected_code}", **over)
        with pytest.raises(ep.EmbeddingPackError) as err:
            ep.load_embedding_pack(directory, export)
        assert err.value.code == expected_code


def test_file_loader_returns_what_the_production_loader_reads_from_the_same_database_rows(
        pg_conn, conn, schema, recent, tmp_path, monkeypatch):
    from core.clustering.hdbscan_clusterer import NewsClusterer
    from db import connection

    # 운영 로더를 먼저 돌린다(임베딩 없는 ok 행, 30시간 전 행, dropped 행이 테이블에 있는 상태에서).
    # 운영 로더는 db.connection의 풀로 읽는다. 접속 설정에 search_path만 더해 이 테스트의 스키마를 보게 한다.
    original_config = connection._build_db_config
    monkeypatch.setattr(connection, "_build_db_config", lambda: {
        **original_config(), "options": f"-c client_encoding=UTF8 -c search_path={schema},public"})
    connection.close_pool()
    try:
        expected = NewsClusterer()._load_data_from_db(lookback_hours=24)
    finally:
        connection.close_pool()

    export = _recent_export(pg_conn, conn, schema, recent, tmp_path)
    pack_dir = _pack_from_database(pg_conn, schema, export, tmp_path / "pack")
    source = e0.FileArticleSource.open(export.dir, pack_dir)
    got = source.load(pseudo_now=datetime.now(timezone.utc).replace(tzinfo=None), lookback_hours=24)

    assert expected["ids"].tolist() == sorted(recent["inside"])  # 운영 쿼리가 고른 것: 최근 24시간, 임베딩·본문 있음
    assert list(got) == list(expected)
    assert got["ids"].dtype == expected["ids"].dtype and got["ids"].tolist() == expected["ids"].tolist()
    assert got["embeddings"].dtype == expected["embeddings"].dtype == np.float32
    np.testing.assert_array_equal(got["embeddings"], expected["embeddings"])
    assert got["titles"] == expected["titles"] and got["press_names"] == expected["press_names"]
    assert got["contents"] == expected["contents"] and got["contents"][0].startswith("가람시청은")
