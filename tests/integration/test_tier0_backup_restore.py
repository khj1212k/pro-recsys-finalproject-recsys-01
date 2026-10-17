"""scripts/oci/tier0_backup.py의 덤프·복원을 실제 PostgreSQL(CI 서비스 컨테이너)에서 돌린다 (ADR 0026 결정 6, ADR 0023).

VM에서는 DB 도구를 `docker compose exec -T db`로 부르지만 여기서는 러너의 pg_dump·pg_restore·psql을 직접 부른다
(Db.exec_prefix = [], 접속은 PG* 환경변수). 원본은 이 테스트가 만드는 DB 하나다 - 테스트 DB의 스키마(Alembic head)를
`pg_dump --schema-only`로 복제하고 합성 행만 넣는다(기사 문장은 지어낸 표식). 끝나면 만든 DB를 모두 지운다.
"""
import gzip
import importlib.util
import os
import shutil
import subprocess
import tarfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("tier0_backup_it", REPO / "scripts" / "oci" / "tier0_backup.py")
tb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tb)

RECENT = "SENTINEL-recent-commercial-body"
OLD = "SENTINEL-28-days-old-commercial-body"
PUBLIC = "SENTINEL-redistributable-body"
UTC = timezone.utc


@pytest.fixture(scope="module")
def pg_tools():
    tools = {name: shutil.which(name) for name in ("pg_dump", "pg_restore", "psql")}
    if not all(tools.values()):
        pytest.skip("pg_dump·pg_restore·psql이 없어 백업 복원 테스트를 건너뜁니다 (CI integration-test 잡은 설치를 확인한다)")
    return tools


def _pg_env(database_url):
    from psycopg2.extensions import parse_dsn

    p = parse_dsn(database_url)
    env = {**os.environ, "PGHOST": p.get("host") or "localhost", "PGPORT": str(p.get("port") or 5432),
           "PGUSER": p.get("user") or "", "PGPASSWORD": p.get("password") or "", "PGCLIENTENCODING": "UTF8"}
    return env, p["dbname"]


def _connect(database_url, dbname):
    import psycopg2

    conn = psycopg2.connect(database_url, dbname=dbname)
    conn.autocommit = True
    return conn


@pytest.fixture
def source(database_url, pg_conn, pg_tools):
    """(Db, 원본 DB 이름, psycopg2 연결, 스냅숏 무렵 시각). 테스트 DB의 스키마를 복제한 새 DB에 합성 행을 넣는다."""
    env, test_db = _pg_env(database_url)
    suffix = uuid.uuid4().hex[:8]
    src = f"tier0_backup_src_{suffix}"
    created = [src]
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{src}"')
    schema = subprocess.run([pg_tools["pg_dump"], "--schema-only", "--no-owner", "--no-privileges", "-d", test_db],
                            capture_output=True, env=env, check=True).stdout
    subprocess.run([pg_tools["psql"], "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", src], input=schema,
                   capture_output=True, env=env, check=True)
    conn = _connect(database_url, src)
    now = datetime.now(UTC).replace(tzinfo=None)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        versions = [r[0] for r in cur.fetchall()]
    with conn.cursor() as cur:
        for v in versions:
            cur.execute("INSERT INTO alembic_version (version_num) VALUES (%s)", (v,))
        cur.execute("INSERT INTO press (press_name) VALUES ('언론사A') RETURNING press_id")
        commercial = cur.fetchone()[0]
        cur.execute("INSERT INTO press (press_name) VALUES ('정책브리핑') RETURNING press_id")
        public = cur.fetchone()[0]

        def news(press, key, content, status, crawled_days_ago, **extra):
            cols = {"press_id": press, "raw_news_title": f"제목 {key}", "raw_news_url": f"https://example.com/{key}",
                    "raw_news_content": content, "raw_news_extract_status": status,
                    "raw_news_crawled_at": now - timedelta(days=crawled_days_ago),
                    "raw_news_created_at": datetime.now(UTC) - timedelta(days=crawled_days_ago, hours=1),
                    "raw_news_content_sha256": (key * 64)[:64] if content else None,
                    "raw_news_content_length": len(content) if content else (10 if status == "ok" else None),
                    **extra}
            names = ", ".join(cols)
            cur.execute(f"INSERT INTO news_raw ({names}) VALUES ({', '.join(['%s'] * len(cols))})", list(cols.values()))

        news(commercial, "a", RECENT, "ok", 1)
        news(commercial, "b", OLD, "ok", 28)
        news(commercial, "c", "", "dropped", 2)
        news(public, "d", PUBLIC, "ok", 40, raw_news_kogl_type=1)
        news(commercial, "e", None, "ok", 35, raw_news_content_purged_at=datetime.now(UTC) - timedelta(days=4))
        news(commercial, "f", "", None, 0)
        cur.execute("UPDATE news_raw SET embedding_result = %s::vector WHERE raw_news_url = 'https://example.com/a'",
                    ("[" + ",".join(["0.25"] * 1024) + "]",))
        cur.execute("INSERT INTO job_runs (job, status, finished_at, stats) VALUES ('ingest', 'succeeded', now(), "
                    "'{\"rss\": {\"inserted\": 3}}')")
    db = tb.Db(exec_prefix=[], user=None, name=src, env=env)
    try:
        yield db, src, conn, created, database_url
    finally:
        conn.close()
        with pg_conn.cursor() as cur:
            for name in created:
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _members(archive, dest):
    with tarfile.open(archive) as tar:
        tar.extractall(dest)
    return {p.name: p for p in Path(dest).iterdir()}


def _sql_of_dump(pg_tools, path):
    return subprocess.run([pg_tools["pg_restore"], "-f", "-", str(path)], capture_output=True, check=True).stdout


def test_nobody_archive_has_every_row_and_no_body_and_full_archive_has_only_recent_or_public_bodies(
        source, tmp_path, pg_tools):
    db, src, conn, _, url = source
    result = tb.build_archives(db, str(tmp_path / "work"))
    nobody, full = result.archives["nobody"], result.archives["full"]

    files = _members(nobody, tmp_path / "nobody")
    assert sorted(files) == sorted(tb.ARCHIVE_MEMBERS["nobody"])
    copy = gzip.open(files["news_raw.nobody.copy.gz"]).read().decode()
    dump_sql = _sql_of_dump(pg_tools, files["db.dump"]).decode()
    manifest = files["manifest.json"].read_text(encoding="utf-8")
    for text in (copy, dump_sql, manifest):
        for sentinel in (RECENT, OLD, PUBLIC):
            assert sentinel not in text
    assert len(copy.splitlines()) == 6
    assert "COPY public.job_runs" in dump_sql and "COPY public.news_raw " not in dump_sql

    full_files = _members(full, tmp_path / "full")
    bodies = gzip.open(full_files["news_raw.bodies.copy.gz"]).read().decode()
    assert RECENT in bodies and PUBLIC in bodies and OLD not in bodies
    assert len(bodies.splitlines()) == 2

    stats = result.stats
    assert stats["nobody"]["news_raw_rows"] == 6 and stats["full"]["body_rows"] == 2
    with conn.cursor() as cur:
        cur.execute("SELECT raw_news_crawled_at + interval '30 days' FROM news_raw WHERE raw_news_url LIKE '%/a'")
        (expected_expiry,) = cur.fetchone()
    assert stats["full"]["expires_at_utc"] == expected_expiry.replace(tzinfo=UTC).isoformat()
    assert stats["names"]["full"].endswith(f".expires-{expected_expiry.date().isoformat()}.full.tar")
    assert stats["table_rows"]["news_raw"] == 6 and stats["table_rows"]["job_runs"] == 1
    assert sorted(os.listdir(tmp_path / "work")) == sorted(os.path.basename(p) for p in (nobody, full))


def test_restore_check_reproduces_counts_schema_and_sequence_then_drops_the_scratch_db(source, tmp_path):
    db, src, conn, created, url = source
    work = str(tmp_path / "work")
    archive = tb.build_archives(db, work).archives["nobody"]
    scratch = f"restore_check_{uuid.uuid4().hex[:8]}"
    created.append(scratch)

    out = tb.restore_archive(db, archive, work, target_db=scratch, scratch=True, keep_target=True)
    checks = out["checks"]
    assert checks["table_rows"]["news_raw"] == 6 and checks["news_raw"]["bodies"] == 0
    assert checks["schema_objects"]["not_valid"] == 0

    restored = _connect(url, scratch)
    try:
        with restored.cursor() as cur:
            cur.execute("SELECT raw_news_url, raw_news_content, raw_news_content_purged_at IS NOT NULL, "
                        "raw_news_content_length, embedding_result IS NOT NULL, raw_news_content_sha256 "
                        "FROM news_raw ORDER BY raw_news_url")
            rows = {r[0].rsplit("/", 1)[1]: r[1:] for r in cur.fetchall()}
            # 본문은 없다: 본문이 있던 행은 NULL, '추출했지만 본문 없음'은 '' 그대로. 해시·길이·임베딩·비운 표시는 남는다
            assert rows["a"][0] is None and rows["a"][2] == len(RECENT) and rows["a"][3] is True
            assert rows["c"][0] == "" and rows["f"][0] == ""
            assert rows["e"][1] is True and rows["e"][0] is None
            assert all(r[4] for k, r in rows.items() if k in "abd")
            # 시퀀스가 복원된 최대 id 뒤에서 이어진다 - 복원한 DB에 수집을 이어 써도 id가 겹치지 않는다
            cur.execute("SELECT max(raw_news_id) FROM news_raw")
            (max_id,) = cur.fetchone()
            cur.execute("SELECT nextval(pg_get_serial_sequence('news_raw', 'raw_news_id'))")
            assert cur.fetchone()[0] > max_id
    finally:
        restored.close()

    # keep_target 없이 다시: 대조 뒤 일회용 DB를 지운다
    tb.restore_archive(db, archive, work, target_db=scratch, scratch=True)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_database WHERE datname = %s", (scratch,))
        assert cur.fetchone()[0] == 0
    assert not os.path.exists(os.path.join(work, "restore"))


def test_full_restore_into_an_empty_db_brings_back_recent_bodies_and_marks_the_rest_purged(source, tmp_path, pg_conn):
    db, src, conn, created, url = source
    work = str(tmp_path / "work")
    archive = tb.build_archives(db, work).archives["full"]
    target = f"tier0_restore_target_{uuid.uuid4().hex[:8]}"
    created.append(target)
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{target}"')

    out = tb.restore_archive(db, archive, work, target_db=target, scratch=False, with_bodies=True)
    assert out["checks"]["news_raw"]["bodies"] == 2

    restored = _connect(url, target)
    try:
        with restored.cursor() as cur:
            cur.execute("SELECT raw_news_url, raw_news_content, raw_news_content_purged_at IS NOT NULL FROM news_raw")
            rows = {r[0].rsplit("/", 1)[1]: r[1:] for r in cur.fetchall()}
    finally:
        restored.close()
    assert rows["a"] == (RECENT, False) and rows["d"] == (PUBLIC, False)
    # 27일보다 오래돼 사본에 없던 본문은 "비운 행"이 된다(본문 NULL·길이 있음 - 보존 잡·추출기가 본문 있는 행으로 보지 않는다)
    assert rows["b"] == (None, True)
    assert rows["c"] == ("", False) and rows["e"] == (None, True)

    # 이미 테이블이 있는 DB에는 복원하지 않는다
    with pytest.raises(tb.BackupError):
        tb.restore_archive(db, archive, work, target_db=target, scratch=False)
    # 본문 없는 사본으로 --with-bodies는 거절
    nobody = tb.build_archives(db, str(tmp_path / "w2")).archives["nobody"]
    with pytest.raises(tb.BackupError):
        tb.restore_archive(db, nobody, str(tmp_path / "w2"), target_db="restore_check_x", scratch=True,
                           with_bodies=True)


def test_restore_check_refuses_the_operational_db_name(source, tmp_path):
    db, src, conn, _, url = source
    archive = tb.build_archives(db, str(tmp_path / "w")).archives["nobody"]
    db_named_like_scratch = tb.Db(exec_prefix=[], user=None, name="restore_check", env=db.env)
    with pytest.raises(tb.BackupError):
        tb.restore_archive(db_named_like_scratch, archive, str(tmp_path / "w"), target_db="restore_check", scratch=True)
    with pytest.raises(tb.BackupError):
        tb.restore_archive(db, archive, str(tmp_path / "w"), target_db=src, scratch=True)


def test_job_recorder_writes_a_row_daily_report_can_read(source):
    db, src, conn, _, url = source
    rec = tb.JobRecorder(db, tb.HOST_BACKUP_JOB, "abc123")
    rec.start()
    with conn.cursor() as cur:
        cur.execute("SELECT status, git_sha FROM job_runs WHERE id = %s", (rec.run_id,))
        assert cur.fetchone() == ("running", "abc123")
    rec.finish("succeeded", {"snapshot_at_utc": "2026-10-17T18:10:00+00:00", "nobody": {"bytes": 5},
                             "note": "it's quoted"})
    with conn.cursor() as cur:
        cur.execute("SELECT status, finished_at IS NOT NULL, stats->'nobody'->>'bytes', stats->>'note', error "
                    "FROM job_runs WHERE id = %s", (rec.run_id,))
        assert cur.fetchone() == ("succeeded", True, "5", "it's quoted", None)
    assert tb.last_success(db, tb.HOST_BACKUP_JOB) is not None
    assert tb.last_success(db, tb.RESTORE_CHECK_JOB) is None
