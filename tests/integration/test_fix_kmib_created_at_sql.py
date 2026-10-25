"""scripts/oci/build_kmib_created_at_fix.py가 만든 SQL과 되돌리기를 실제 PostgreSQL에서 돌린다 (ADR 0008 2026-10-25, 런북 6.10).

fix_aitimes_created_at_kst_sql 테스트와 같은 방식(스키마 복제 + search_path). 지어낸 id·URL·제목만 쓴다.
"""
import csv
import importlib.util
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REVERT_SQL = REPO_ROOT / "scripts" / "oci" / "fix_kmib_created_at_revert.sql"
PSQL_SCRIPT_ERROR = 3
UTC = timezone.utc

spec = importlib.util.spec_from_file_location("build_kmib_created_at_fix", REPO_ROOT / "scripts" / "oci" / "build_kmib_created_at_fix.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


@pytest.fixture(scope="module")
def psql_bin():
    path = shutil.which("psql")
    if not path:
        pytest.skip("psql 클라이언트가 없어 SQL 테스트를 건너뜁니다")
    return path


@pytest.fixture
def schema(pg_conn):
    name = f"fix_kmib_ts_{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{name}"')
        cur.execute(f'CREATE TABLE "{name}".press (LIKE public.press INCLUDING ALL)')
        cur.execute(f'CREATE TABLE "{name}".news_raw (LIKE public.news_raw INCLUDING ALL)')
    try:
        yield name
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def _run(psql_bin, database_url, schema, sql_file, *variables):
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema}", "PGCLIENTENCODING": "UTF8"}
    args = [psql_bin, "-X", "-v", "ON_ERROR_STOP=1"]
    for v in variables:
        args += ["-v", v]
    return subprocess.run([*args, "-f", str(sql_file), database_url], capture_output=True, text=True, encoding="utf-8",
                          env=env, timeout=120)


def _seed(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(f'INSERT INTO "{schema}".press (press_name) VALUES (%s) RETURNING press_id', ("국민일보",))
        kmib = cur.fetchone()[0]
        cur.execute(f'INSERT INTO "{schema}".press (press_name) VALUES (%s) RETURNING press_id', ("경향신문",))
        other = cur.fetchone()[0]
        rows = [  # (press, title, stored created_at, crawled_at UTC naive)
            (kmib, "k1", datetime(2026, 10, 23, 10, 38, tzinfo=UTC), datetime(2026, 10, 22, 19, 7)),
            (kmib, "k2", datetime(2026, 10, 22, 9, 29, tzinfo=UTC), datetime(2026, 10, 21, 19, 6)),
            (other, "o1", datetime(2026, 10, 22, 9, 29, tzinfo=UTC), datetime(2026, 10, 21, 19, 6)),
        ]
        ids = []
        for pid, title, created, crawled in rows:
            cur.execute(f'INSERT INTO "{schema}".news_raw (press_id, raw_news_title, raw_news_url, raw_news_content, '
                        "raw_news_created_at, raw_news_crawled_at) VALUES (%s,%s,%s,'',%s,%s) RETURNING raw_news_id",
                        (pid, title, f"https://example.test/{schema}/{title}", created, crawled))
            ids.append(cur.fetchone()[0])
    return ids


def _created(pg_conn, schema):
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT raw_news_title, raw_news_created_at FROM "{schema}".news_raw')
        return dict(cur.fetchall())


def _build_sql(tmp_path, ids, capsys):
    src = tmp_path / "pub.csv"
    with open(src, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([ids[0], "u1", "2026-10-23T10:38:00Z", "2026-10-22T19:07:00Z", "2026-10-23T03:05:00+09:00", ""])
        w.writerow([ids[1], "u2", "2026-10-22T09:29:00Z", "2026-10-21T19:06:00Z", "2026-10-22T03:04:00+09:00", ""])
        w.writerow([ids[2], "u3", "2026-10-22T09:29:00Z", "2026-10-21T19:06:00Z", "2026-10-22T03:04:00+09:00", ""])  # 경향신문 - 건너뛰어야 한다
    out = tmp_path / "fix.sql"
    import sys as _sys
    old = _sys.stdout
    with open(out, "w", encoding="utf-8") as f:
        _sys.stdout = f
        try:
            assert gen.main(str(src)) == 0
        finally:
            _sys.stdout = old
    capsys.readouterr()
    return out


def test_rehearsal_apply_guard_and_revert(psql_bin, database_url, pg_conn, schema, tmp_path, capsys):
    ids = _seed(pg_conn, schema)
    before = _created(pg_conn, schema)
    fix = _build_sql(tmp_path, ids, capsys)
    # 경향신문 행은 VALUES에 있어도 press 조건에서 빠져 기대 행 수(3)와 달라진다 - 멈춰야 한다
    refused = _run(psql_bin, database_url, schema, fix, "apply=1")
    assert refused.returncode == PSQL_SCRIPT_ERROR and "원래 값이 그대로였다" in refused.stderr
    assert _created(pg_conn, schema) == before


def test_apply_fixes_only_kmib_rows_and_revert_restores(psql_bin, database_url, pg_conn, schema, tmp_path, capsys):
    ids = _seed(pg_conn, schema)
    before = _created(pg_conn, schema)
    src = tmp_path / "pub.csv"
    with open(src, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([ids[0], "u1", "2026-10-23T10:38:00Z", "2026-10-22T19:07:00Z", "2026-10-23T03:05:00+09:00", ""])
        w.writerow([ids[1], "u2", "2026-10-22T09:29:00Z", "2026-10-21T19:06:00Z", "2026-10-22T03:04:00+09:00", ""])
    out = tmp_path / "fix.sql"
    import sys as _sys
    old = _sys.stdout
    with open(out, "w", encoding="utf-8") as f:
        _sys.stdout = f
        try:
            assert gen.main(str(src)) == 0
        finally:
            _sys.stdout = old
    capsys.readouterr()

    rehearsal = _run(psql_bin, database_url, schema, out)
    assert rehearsal.returncode == 0, rehearsal.stderr
    assert "fixed=2" in rehearsal.stderr and _created(pg_conn, schema) == before

    done = _run(psql_bin, database_url, schema, out, "apply=1")
    assert done.returncode == 0, done.stderr
    after = _created(pg_conn, schema)
    assert after["k1"] == datetime(2026, 10, 22, 18, 5, tzinfo=UTC)
    assert after["k2"] == datetime(2026, 10, 21, 18, 4, tzinfo=UTC)
    assert after["o1"] == before["o1"]

    again = _run(psql_bin, database_url, schema, out, "apply=1")
    assert again.returncode == PSQL_SCRIPT_ERROR and "already exists" in again.stderr

    reverted = _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1")
    assert reverted.returncode == 0, reverted.stderr
    assert _created(pg_conn, schema) == before


def _generated(tmp_path, capsys, name, *cli_args):
    """생성기의 명령줄(런북 6.10)과 같은 경로로 SQL 파일을 만든다."""
    capsys.readouterr()
    assert gen.cli(list(cli_args)) == 0
    out = tmp_path / name
    out.write_text(capsys.readouterr().out, encoding="utf-8")
    return out


def _audit_ids(pg_conn, schema, table):
    with pg_conn.cursor() as cur:
        cur.execute(f'SELECT raw_news_id FROM "{schema}".{table} ORDER BY 1')
        return [r[0] for r in cur.fetchall()]


def test_second_pass_for_all_presses_has_its_own_audit_table_and_reverts_without_touching_the_first(
        psql_bin, database_url, pg_conn, schema, tmp_path, capsys):
    """런북 6.10의 2차 보정: 1차 보관 표가 남아 있는 DB에서 다른 이름의 보관 표로, 언론사를 가리지 않고 보정한다."""
    ids = _seed(pg_conn, schema)  # k1(국민일보) k2(국민일보) o1(경향신문)
    before = _created(pg_conn, schema)

    first_csv = tmp_path / "first.csv"
    first_csv.write_text(f"{ids[0]},u1,2026-10-23T10:38:00Z,2026-10-22T19:07:00Z,2026-10-23T03:05:00+09:00,\n", encoding="utf-8")
    first = _generated(tmp_path, capsys, "fix1.sql", str(first_csv))
    assert _run(psql_bin, database_url, schema, first, "apply=1").returncode == 0
    after_first = _created(pg_conn, schema)
    assert after_first["k1"] != before["k1"]

    second_csv = tmp_path / "second.csv"
    second_csv.write_text(
        f"{ids[1]},u2,2026-10-22T09:29:00Z,2026-10-21T19:06:00Z,2026-10-22T03:04:00+09:00,\n"
        f"{ids[2]},u3,2026-10-22T09:29:00Z,2026-10-21T19:06:00Z,2026-10-22T03:04:00+09:00,\n", encoding="utf-8")
    second = _generated(tmp_path, capsys, "fix2.sql", str(second_csv), "--all-presses", "--table", "news_raw_created_at_fix_2")

    rehearsal = _run(psql_bin, database_url, schema, second)
    assert rehearsal.returncode == 0, rehearsal.stderr
    assert "fixed=2" in rehearsal.stderr and _created(pg_conn, schema) == after_first

    done = _run(psql_bin, database_url, schema, second, "apply=1")
    assert done.returncode == 0, done.stderr
    after_second = _created(pg_conn, schema)
    assert after_second["k2"] == datetime(2026, 10, 21, 18, 4, tzinfo=UTC)
    assert after_second["o1"] == datetime(2026, 10, 21, 18, 4, tzinfo=UTC)  # 경향신문 행도 바뀐다
    assert after_second["k1"] == after_first["k1"]  # 1차에 고친 행은 그대로
    assert _audit_ids(pg_conn, schema, "news_raw_kmib_created_at_fix") == [ids[0]]
    assert _audit_ids(pg_conn, schema, "news_raw_created_at_fix_2") == [ids[1], ids[2]]

    again = _run(psql_bin, database_url, schema, second, "apply=1")
    assert again.returncode == PSQL_SCRIPT_ERROR and "already exists" in again.stderr

    revert = _generated(tmp_path, capsys, "revert2.sql", "--revert", "--table", "news_raw_created_at_fix_2")
    reverted = _run(psql_bin, database_url, schema, revert, "apply=1")
    assert reverted.returncode == 0 and "restore=2" in reverted.stderr, reverted.stderr
    assert _created(pg_conn, schema) == after_first  # 2차만 되돌아가고 1차 보정은 남는다
    assert _audit_ids(pg_conn, schema, "news_raw_kmib_created_at_fix") == [ids[0]]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f'"{schema}".news_raw_created_at_fix_2',))
        assert cur.fetchone()[0] is None  # 2차 보관 표는 지워졌다

    assert _run(psql_bin, database_url, schema, REVERT_SQL, "apply=1").returncode == 0
    assert _created(pg_conn, schema) == before


def test_press_limit_still_holds_for_the_first_pass_when_a_row_of_another_press_is_listed(
        psql_bin, database_url, pg_conn, schema, tmp_path, capsys):
    """--all-presses를 쓰지 않으면 국민일보 밖의 행이 CSV에 있어도 바꾸지 못하고 멈춘다(예전과 같다)."""
    ids = _seed(pg_conn, schema)
    before = _created(pg_conn, schema)
    csv_path = tmp_path / "mixed.csv"
    csv_path.write_text(f"{ids[2]},u3,2026-10-22T09:29:00Z,2026-10-21T19:06:00Z,2026-10-22T03:04:00+09:00,\n", encoding="utf-8")
    fix = _generated(tmp_path, capsys, "fix.sql", str(csv_path))

    refused = _run(psql_bin, database_url, schema, fix, "apply=1")

    assert refused.returncode == PSQL_SCRIPT_ERROR and "원래 값이 그대로였다" in refused.stderr
    assert _created(pg_conn, schema) == before
