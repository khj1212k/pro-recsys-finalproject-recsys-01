"""evaluation/llm/frozen_export.py — news_raw의 동결 반출(읽기 전용), 매니페스트, 30일 정리.

DB 없이 도는 단위 테스트다. COPY 출력은 PostgreSQL 텍스트 형식을 흉내 낸 합성 줄로 만든다(실제 서버의
출력은 tests/integration/test_frozen_export_db.py가 CI의 Postgres에서 확인한다). 기사 본문은 전부
tests/frozen_news_fakes.py에서 지어낸 문장이다.
"""
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evaluation.llm import frozen_export as fx
from tests.frozen_news_fakes import BODY_MARK, CODE, TITLE_MARK
from tests.frozen_news_fakes import body as _body
from tests.frozen_news_fakes import header as _header
from tests.frozen_news_fakes import server_row as _server_row
from tests.frozen_news_fakes import sha as _sha
from tests.frozen_news_fakes import stream as _stream
from tests.frozen_news_fakes import use_tmp_main_checkout

KST = timezone(timedelta(hours=9))
WINDOW = fx.Window.from_iso("2026-10-02T21:00:00", "2026-10-05T21:00:00")


# 테스트의 "지금"(시계를 읽는 기본값들: CLI의 T0, verify의 만료 판단). 합성 머리말의 스냅숏(10-06 07:21:30 UTC)과
# 같은 KST 날짜다. 실제 날짜가 지나도 결과가 바뀌지 않게 시계를 고정한다.
CLOCK = datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _main_checkout(monkeypatch, tmp_path):
    """반출본은 메인 체크아웃의 data/exports 아래에만 쓰인다 - 테스트에서는 tmp_path/data가 그 자리다."""
    monkeypatch.setattr(fx, "_utcnow", lambda: CLOCK)
    return use_tmp_main_checkout(monkeypatch, tmp_path)


def _bundle(rows=None, **kwargs):
    rows = [_server_row(k) for k in (3, 1, 2)] if rows is None else rows
    return fx.build_export(fx.parse_copy_stream(_stream(rows)), WINDOW, code=CODE, **kwargs)


def _written(tmp_path, rows=None, **kwargs):
    dest = tmp_path / "data" / "exports" / "20261006T072130Z"
    fx.write_export(dest, _bundle(rows, **kwargs))
    return dest


# ---------------------------------------------------------------- SQL


def test_script_is_one_read_only_snapshot_transaction_with_timeouts_and_ends_in_rollback():
    script = fx.build_psql_script(WINDOW, statement_timeout_s=90, lock_timeout_s=3)
    sql = "\n".join(line for line in script.splitlines() if not line.startswith("\\"))  # psql 메타 명령 제외
    statements = [s.strip() for s in sql.split(";") if s.strip()]

    begins = [s for s in statements if s.upper().startswith("BEGIN")]
    assert begins == ["BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"]
    assert statements[-1].upper() == "ROLLBACK"
    assert "COMMIT" not in script.upper()
    assert "SET LOCAL statement_timeout = '90s'" in script
    assert "SET LOCAL lock_timeout = '3s'" in script
    assert "SET LOCAL idle_in_transaction_session_timeout" in script
    assert script.count("TO STDOUT") == 3  # 머리말, 행, 꼬리말 - 같은 스냅숏
    # psql이 오류 뒤에 다음 문장으로 넘어가지 않고, 명령 태그(BEGIN, SET)를 출력에 섞지 않는다
    assert "\\set ON_ERROR_STOP on" in script and "\\set QUIET on" in script
    # SET LOCAL은 트랜잭션 안에서만 효력이 있다: BEGIN이 그보다 먼저다
    assert script.index("BEGIN TRANSACTION") < script.index("SET LOCAL statement_timeout")


def test_script_contains_no_statement_that_could_write():
    script = fx.build_psql_script(WINDOW)
    words = set(re.findall(r"[A-Za-z_]+", script.upper()))
    assert not words & {"INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "GRANT",
                        "VACUUM", "REINDEX", "LOCK", "NEXTVAL", "SETVAL", "INTO"}


def test_script_limits_rows_to_ok_bodies_inside_the_half_open_window():
    script = fx.build_psql_script(WINDOW)
    assert "n.raw_news_extract_status = 'ok'" in script
    assert "n.raw_news_content <> ''" in script
    assert "n.raw_news_crawled_at >= TIMESTAMP '2026-10-02 21:00:00.000000'" in script
    assert "n.raw_news_crawled_at < TIMESTAMP '2026-10-05 21:00:00.000000'" in script
    assert "embedding_result" not in script  # 벡터는 반출 대상이 아니다


def test_window_rejects_reversed_aware_and_longer_than_five_days():
    with pytest.raises(fx.WindowRefused):
        fx.Window.from_iso("2026-10-05T21:00:00", "2026-10-02T21:00:00")
    with pytest.raises(fx.WindowRefused):
        fx.Window(datetime(2026, 10, 2, 21, tzinfo=timezone.utc), datetime(2026, 10, 3, 21, tzinfo=timezone.utc))
    with pytest.raises(fx.WindowRefused, match="120"):
        fx.Window.from_iso("2026-10-01T00:00:00", "2026-10-06T00:00:01")
    assert fx.Window.from_iso("2026-10-01T00:00:00", "2026-10-06T00:00:00").hours == 120


def test_registered_day_windows_end_at_0600_kst_of_completed_days_before_t0():
    t0 = datetime(2026, 10, 8, 14, 0, tzinfo=KST)
    windows = fx.registered_day_windows(t0, days=3)
    assert [(w.start.isoformat(), w.end.isoformat()) for w in windows] == [
        ("2026-10-03T21:00:00", "2026-10-04T21:00:00"),  # t_1 = 10-05 06:00 KST
        ("2026-10-04T21:00:00", "2026-10-05T21:00:00"),  # t_2 = 10-06 06:00 KST
        ("2026-10-05T21:00:00", "2026-10-06T21:00:00"),  # t_3 = 10-07 06:00 KST (T0 직전 완결된 날짜)
    ]
    # 자정 직후에 반출해도 마지막 의사 시각은 T0보다 앞이다
    early = fx.registered_day_windows(datetime(2026, 10, 8, 0, 30, tzinfo=KST), days=5)
    assert early[-1].end == datetime(2026, 10, 6, 21, 0) and len(early) == 5
    union = fx.union_window(early)
    assert (union.start, union.end, union.hours) == (datetime(2026, 10, 1, 21, 0), datetime(2026, 10, 6, 21, 0), 120)
    with pytest.raises(fx.WindowRefused):
        fx.registered_day_windows(datetime(2026, 10, 8, 14, 0), days=3)  # 시간대 없는 T0는 받지 않는다
    with pytest.raises(fx.WindowRefused):
        fx.registered_day_windows(t0, days=6)


def test_plan_windows_is_the_registered_rule_and_its_union():
    union, plan = fx.plan_windows(datetime(2026, 10, 6, 16, 0, tzinfo=KST), 3)
    assert (union.start, union.end) == (datetime(2026, 10, 1, 21, 0), datetime(2026, 10, 4, 21, 0))
    assert plan["day_windows_utc"] == [
        ["2026-10-01T21:00:00.000000Z", "2026-10-02T21:00:00.000000Z"],
        ["2026-10-02T21:00:00.000000Z", "2026-10-03T21:00:00.000000Z"],
        ["2026-10-03T21:00:00.000000Z", "2026-10-04T21:00:00.000000Z"],
    ]
    assert (plan["t0_kst"], plan["days"], plan["hour_kst"], plan["lookback_hours"]) == (
        "2026-10-06T16:00:00+09:00", 3, 6, 24)


# ---------------------------------------------------------------- COPY 출력 읽기


def test_parse_copy_stream_restores_text_exactly():
    rows = [_server_row(1), _server_row(2)]
    raw = fx.parse_copy_stream(_stream(rows))
    assert raw.header["alembic_revision"] == "d48994e9d26e"
    assert [r["body"] for r in raw.rows] == [_body(1), _body(2)]
    assert raw.trailer["rows"] == 2


def test_parse_refuses_unexpected_line_without_echoing_it():
    lines = _stream([_server_row(1)])
    lines.insert(1, f"NOTICE {BODY_MARK} 본문 조각".encode("utf-8"))
    with pytest.raises(fx.ExportError) as err:
        fx.parse_copy_stream(lines)
    assert BODY_MARK not in str(err.value)
    assert "2번째 줄" in str(err.value)


def test_parse_refuses_truncated_stream():
    rows = [_server_row(1), _server_row(2)]
    with pytest.raises(fx.ExportError, match="꼬리말"):
        fx.parse_copy_stream(_stream(rows, trailer=None))
    with pytest.raises(fx.ExportError, match="행 수"):
        fx.parse_copy_stream(_stream(rows, trailer={"kind": "trailer", "rows": 3, "min_id": 1001, "max_id": 1003}))


@pytest.mark.parametrize("override", [
    {"transaction_read_only": "off"},
    {"transaction_isolation": "read committed"},
    {"statement_timeout": "0"},
])
def test_parse_refuses_header_that_does_not_prove_a_bounded_read_only_snapshot(override):
    with pytest.raises(fx.ExportError, match="읽기 전용 스냅숏"):
        fx.parse_copy_stream(_stream([_server_row(1)], head=_header(**override)))


# ---------------------------------------------------------------- 행과 매니페스트


def test_build_export_sorts_rows_hashes_bodies_and_sets_thirty_day_expiry():
    bundle = _bundle()
    assert [r["id"] for r in bundle.rows] == [1001, 1002, 1003]
    first = bundle.rows[0]
    assert first["content_sha256"] == _sha(_body(1)) and first["body_chars"] == len(_body(1))
    assert first["body_expires_at"] == "2026-11-02T01:00:00.000000Z"  # 수집 시각 + 30일
    assert first["body_purged_at"] is None and first["redistributable"] is False

    identity = bundle.manifest["identity"]
    assert identity["rows"] == 3
    assert identity["window_utc"] == {"start": "2026-10-02T21:00:00.000000Z", "end": "2026-10-05T21:00:00.000000Z",
                                      "bound": "start <= raw_news_crawled_at < end"}
    assert identity["source"]["alembic_revision"] == "d48994e9d26e"
    assert identity["source"]["snapshot_at_utc"] == "2026-10-06T07:21:30.123456Z"
    assert identity["source"]["transaction_read_only"] == "on"
    assert identity["code"] == CODE
    assert identity["by_press"] == {"가람일보": 3}
    assert identity["retention"]["days"] == 30
    assert identity["retention"]["first_body_expiry_utc"] == "2026-11-02T01:00:00.000000Z"
    assert identity["db_hash_check"] == {"mismatch": 0, "missing": 0, "ids": []}
    assert bundle.manifest["identity_sha256"] == _sha(fx.canonical_json(identity))


def test_identity_does_not_depend_on_server_row_order_but_does_on_content():
    same = _bundle([_server_row(k) for k in (1, 2, 3)])
    changed = _bundle([_server_row(1), _server_row(2), _server_row(3, text=_body(3) + " 한 글자 더")])
    assert _bundle().manifest["identity_sha256"] == same.manifest["identity_sha256"]
    assert _bundle().manifest["identity"]["rows_sha256"] != changed.manifest["identity"]["rows_sha256"]


def test_build_export_counts_rows_whose_stored_hash_disagrees_with_the_exported_body():
    rows = [_server_row(1), _server_row(2, db_sha="f" * 64), _server_row(3, db_sha=None)]
    check = _bundle(rows).manifest["identity"]["db_hash_check"]
    assert check == {"mismatch": 1, "missing": 1, "ids": [1002, 1003]}


def test_build_export_refuses_rows_outside_the_window_duplicates_and_empty_bodies():
    with pytest.raises(fx.ExportError, match="창 밖"):
        _bundle([_server_row(1, crawled="2026-10-05T21:00:00.000000Z")])  # 상한은 열려 있다
    with pytest.raises(fx.ExportError, match="중복"):
        _bundle([_server_row(1), _server_row(1)])
    with pytest.raises(fx.ExportError, match="본문"):
        _bundle([_server_row(1, text="")])


def test_redistributable_sources_are_exempt_from_expiry_and_unknown_sources_are_not():
    rows = [_server_row(1, press="정책브리핑"), _server_row(2, press="처음 보는 출처")]
    bundle = _bundle(rows)
    by_id = {r["id"]: r for r in bundle.rows}
    assert by_id[1001]["redistributable"] is True and by_id[1001]["body_expires_at"] is None
    assert by_id[1002]["redistributable"] is False and by_id[1002]["body_expires_at"] is not None
    assert bundle.manifest["identity"]["retention"]["exempt_rows"] == 1


# ---------------------------------------------------------------- 쓰기와 읽기


def test_write_then_load_round_trip_with_private_permissions(tmp_path):
    dest = _written(tmp_path)
    assert stat.S_IMODE(os.stat(dest).st_mode) == 0o700
    for name in (fx.ARTICLES_FILE, fx.MANIFEST_FILE):
        assert stat.S_IMODE(os.stat(dest / name).st_mode) == 0o600

    manifest = json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))
    entry = manifest["files"][fx.ARTICLES_FILE]
    assert entry["sha256"] == hashlib.sha256((dest / fx.ARTICLES_FILE).read_bytes()).hexdigest()
    assert entry["rows"] == 3 and entry["rows_with_body"] == 3
    assert entry["next_body_expiry_utc"] == "2026-11-02T01:00:00.000000Z"
    assert BODY_MARK not in json.dumps(manifest, ensure_ascii=False)  # 매니페스트에는 본문·제목이 없다
    assert TITLE_MARK not in json.dumps(manifest, ensure_ascii=False)

    loaded = fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc))
    assert loaded.identity_sha256 == manifest["identity_sha256"]
    assert [r["body"] for r in loaded.rows] == [_body(1), _body(2), _body(3)]


def test_write_refuses_to_replace_an_existing_export(tmp_path):
    dest = _written(tmp_path)
    with pytest.raises(fx.DestinationRefused, match="이미"):
        fx.write_export(dest, _bundle())


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("/data/\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "note.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "docs/note.md")
    return root


def test_body_files_are_written_only_under_the_main_checkout_exports_directory(tmp_path):
    """이름만 data/exports인 다른 곳(동기화 폴더, 임시 디렉터리)은 받지 않는다."""
    for refused in (tmp_path / "elsewhere" / "data" / "exports" / "x", tmp_path / "data" / "x",
                    tmp_path / "data" / "exports-old" / "x"):
        with pytest.raises(fx.DestinationRefused, match="아래에만"):
            fx.write_export(refused, _bundle())
        assert not refused.exists()
    with pytest.raises(fx.DestinationRefused, match="아래에만"):
        fx.export_news_raw(WINDOW, root=tmp_path / "elsewhere" / "data" / "exports", psql_command=["false"], code=CODE)
    assert fx.ensure_export_location(tmp_path / "data" / "exports") == (tmp_path / "data" / "exports").resolve()
    assert fx.ensure_export_location(tmp_path / "data" / "exports" / "x" / "..") == (tmp_path / "data" / "exports").resolve()
    with pytest.raises(fx.DestinationRefused):
        fx.ensure_export_location(tmp_path / "data" / "exports" / ".." / "elsewhere")


def test_destination_must_not_be_trackable_by_git(repo):
    assert fx.ensure_private_destination(repo / "data" / "exports" / "x") == (repo / "data" / "exports" / "x").resolve()
    for refused in (repo / "exports" / "x", repo / "docs" / "exports", repo / ".git" / "exports"):
        with pytest.raises(fx.DestinationRefused):
            fx.ensure_private_destination(refused)


def test_destination_with_a_tracked_file_under_an_ignored_directory_is_refused(repo):
    forced = repo / "data" / "exports" / "x"
    forced.mkdir(parents=True)
    (forced / "keep.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-f", "data/exports/x/keep.txt")  # gitignore를 무시하고 추적시킨 경우
    with pytest.raises(fx.DestinationRefused, match="추적"):
        fx.ensure_private_destination(forced)


def test_write_export_checks_the_destination(repo, monkeypatch):
    monkeypatch.setattr(fx, "main_checkout_data", lambda: repo / "data")
    with pytest.raises(fx.DestinationRefused, match="아래에만"):
        fx.write_export(repo / "exports" / "x", _bundle())
    assert not (repo / "exports").exists()
    (repo / ".gitignore").write_text("", encoding="utf-8")  # data/를 무시하지 않는 저장소라면
    with pytest.raises(fx.DestinationRefused, match="추적"):
        fx.write_export(repo / "data" / "exports" / "x", _bundle())
    assert not (repo / "data").exists()


def test_load_refuses_a_file_whose_body_no_longer_matches_its_hash(tmp_path):
    dest = _written(tmp_path)
    path = dest / fx.ARTICLES_FILE
    path.write_text(path.read_text(encoding="utf-8").replace("도서관", "체육관", 1), encoding="utf-8")
    with pytest.raises(fx.ExportIntegrityError):
        fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc))


def test_load_refuses_a_file_with_a_row_removed(tmp_path):
    dest = _written(tmp_path)
    path = dest / fx.ARTICLES_FILE
    lines = path.read_bytes().split(b"\n")  # str.splitlines는 본문 안의 U+2028에서도 끊는다
    path.write_bytes(b"\n".join(lines[:-2]) + b"\n")
    with pytest.raises(fx.ExportIntegrityError):
        fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc))


@pytest.mark.parametrize("old, new", [
    ('"body_expires_at":"2026-11-02T01:00:00.000000Z"', '"body_expires_at":"2027-11-02T01:00:00.000000Z"'),
    ('"body_expires_at":"2026-11-02T01:00:00.000000Z"', '"body_expires_at":null'),
    ('"redistributable":false', '"redistributable":true'),
])
def test_load_refuses_a_file_whose_expiry_was_edited_to_dodge_the_purge(tmp_path, old, new):
    dest = _written(tmp_path)
    path = dest / fx.ARTICLES_FILE
    data = path.read_bytes()
    assert old.encode() in data
    path.write_bytes(data.replace(old.encode(), new.encode(), 1))
    with pytest.raises(fx.ExportIntegrityError):
        fx.load_export(dest, now=datetime(2026, 12, 1, tzinfo=timezone.utc))
    with pytest.raises(fx.ExportIntegrityError):
        fx.purge_export(dest, now=datetime(2026, 12, 1, tzinfo=timezone.utc))


# ---------------------------------------------------------------- 30일: 만료와 정리


def _mixed_ages(tmp_path):
    rows = [
        _server_row(1, crawled="2026-10-02T21:00:00.000000Z"),  # 가장 오래된 행
        _server_row(2, crawled="2026-10-03T12:00:00.000000Z"),
        _server_row(3, crawled="2026-10-05T20:59:59.000000Z"),
    ]
    return _written(tmp_path, rows)


def test_loading_removes_expired_bodies_from_disk_and_keeps_hashes(tmp_path):
    dest = _mixed_ages(tmp_path)
    before = json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)  # 1번 행은 11-01 21:00, 2번 행은 11-02 12:00에 만료

    loaded = fx.load_export(dest, now=now)

    assert [r["body"] is None for r in loaded.rows] == [True, True, False]
    assert [r["content_sha256"] for r in loaded.rows] == [_sha(_body(1)), _sha(_body(2)), _sha(_body(3))]
    on_disk = (dest / fx.ARTICLES_FILE).read_text(encoding="utf-8")
    assert on_disk.count(BODY_MARK) == 1  # 디스크에서도 사라졌다
    after = json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert after["identity_sha256"] == before["identity_sha256"]  # 정리는 반출본의 신원을 바꾸지 않는다
    assert after["files"][fx.ARTICLES_FILE]["rows_with_body"] == 1
    assert after["files"][fx.ARTICLES_FILE]["sha256"] == hashlib.sha256(on_disk.encode("utf-8")).hexdigest()
    assert after["files"][fx.ARTICLES_FILE]["sha256_at_export"] == before["files"][fx.ARTICLES_FILE]["sha256"]
    assert after["purges"][-1]["rows_blanked"] == 2 and after["purges"][-1]["reason"] == "expired"
    # 제목·URL은 남는다(ADR 0023: 제목·URL·발행시각은 유지)
    assert loaded.rows[0]["title"].startswith(TITLE_MARK) and loaded.rows[0]["url"].endswith("/1")


def test_loader_never_returns_an_expired_body_even_when_it_may_not_rewrite_the_file(tmp_path):
    dest = _mixed_ages(tmp_path)
    before = (dest / fx.ARTICLES_FILE).read_bytes()
    loaded = fx.load_export(dest, now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc), purge_expired=False)
    assert [r["body"] is None for r in loaded.rows] == [True, True, False]
    assert loaded.expired_unpurged == 2
    assert (dest / fx.ARTICLES_FILE).read_bytes() == before


def test_purge_is_idempotent_and_reports_what_it_did(tmp_path):
    dest = _mixed_ages(tmp_path)
    now = datetime(2026, 11, 1, 21, 0, tzinfo=timezone.utc)  # 만료 시각과 같은 순간은 만료다
    first = fx.purge_export(dest, now=now)
    second = fx.purge_export(dest, now=now)
    assert (first["rows_blanked"], first["rows_with_body"]) == (1, 2)
    assert (second["rows_blanked"], second["rows_with_body"]) == (0, 2)
    manifest = json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert len(manifest["purges"]) == 1
    assert manifest["files"][fx.ARTICLES_FILE]["next_body_expiry_utc"] == "2026-11-02T12:00:00.000000Z"


def test_purge_everything_blanks_all_bodies_before_expiry(tmp_path):
    dest = _mixed_ages(tmp_path)
    stats = fx.purge_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc), everything=True)
    assert stats["rows_blanked"] == 3 and stats["rows_with_body"] == 0
    assert BODY_MARK not in (dest / fx.ARTICLES_FILE).read_text(encoding="utf-8")
    loaded = fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc))
    assert all(r["body"] is None and r["body_purged_at"] for r in loaded.rows)
    assert json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))["purges"][-1]["reason"] == "everything"


def _stop_before_writing(monkeypatch, name):
    """`_write_private`가 `name` 파일을 쓰기 직전에 프로세스가 죽은 것처럼 멈추게 한다."""
    original = fx._write_private

    def write(path, data):
        if path.name == name:
            raise KeyboardInterrupt("여기서 끊겼다")
        original(path, data)

    monkeypatch.setattr(fx, "_write_private", write)


def test_a_purge_cut_off_after_the_manifest_is_finished_on_the_next_open(tmp_path, monkeypatch):
    dest = _mixed_ages(tmp_path)
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    before = (dest / fx.ARTICLES_FILE).read_bytes()
    with monkeypatch.context() as m:
        _stop_before_writing(m, fx.ARTICLES_FILE)
        with pytest.raises(KeyboardInterrupt):
            fx.purge_export(dest, now=now)
    assert (dest / fx.ARTICLES_FILE).read_bytes() == before  # 매니페스트만 바뀌었다
    manifest = json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["purges"][-1]["sha256_before"] == hashlib.sha256(before).hexdigest()

    stale = fx.load_export(dest, now=now, purge_expired=False)  # 고치지 않고 열어도 지워졌어야 할 본문은 안 나온다
    assert [r["body"] is None for r in stale.rows] == [True, True, False] and stale.expired_unpurged == 2
    assert (dest / fx.ARTICLES_FILE).read_bytes() == before

    loaded = fx.load_export(dest, now=now)

    assert [r["body"] is None for r in loaded.rows] == [True, True, False]
    on_disk = (dest / fx.ARTICLES_FILE).read_bytes()
    assert on_disk.decode("utf-8").count(BODY_MARK) == 1
    after = json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert after == manifest  # 같은 정리를 끝냈을 뿐 새 기록은 없다
    assert after["files"][fx.ARTICLES_FILE]["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert {r["body_purged_at"] for r in loaded.rows if r["body"] is None} == {"2026-11-02T12:00:00.000000Z"}


def test_a_file_ahead_of_its_manifest_is_refused_instead_of_adopted(tmp_path):
    """정리는 매니페스트를 먼저 쓰므로 "본문 파일만 바뀐" 상태는 끊긴 정리로 생기지 않는다 - 손으로 고친 것이다."""
    dest = _mixed_ages(tmp_path)
    manifest_before = (dest / fx.MANIFEST_FILE).read_bytes()
    fx.purge_export(dest, now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))
    (dest / fx.MANIFEST_FILE).write_bytes(manifest_before)
    for opened in (lambda: fx.load_export(dest, now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)),
                   lambda: fx.purge_export(dest, now=datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc))):
        with pytest.raises(fx.ExportIntegrityError, match="설명되지 않는다"):
            opened()
    assert (dest / fx.MANIFEST_FILE).read_bytes() == manifest_before  # 매니페스트를 새 파일에 맞춰 고쳐 쓰지 않는다


def test_bodies_restored_from_before_an_earlier_purge_are_refused(tmp_path):
    dest = _mixed_ages(tmp_path)
    original = (dest / fx.ARTICLES_FILE).read_bytes()
    fx.purge_export(dest, now=datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc))   # 1번 행
    fx.purge_export(dest, now=datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc))   # 2번 행
    (dest / fx.ARTICLES_FILE).write_bytes(original)
    with pytest.raises(fx.ExportIntegrityError, match="설명되지 않는다"):
        fx.load_export(dest, now=datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc))


def _edit_row(dest, row_id, **changes):
    """본문 파일의 한 행을 고친다. `sync_manifest`면 매니페스트의 파일 sha256도 새 파일에 맞춘다(꼼꼼한 변조)."""
    sync = changes.pop("sync_manifest", False)
    path = dest / fx.ARTICLES_FILE
    rows = [json.loads(line) for line in path.read_bytes().split(b"\n") if line.strip()]
    for row in rows:
        if row["id"] == row_id:
            for key, value in changes.items():
                if value is _DROP:
                    row.pop(key)
                else:
                    row[key] = value
    data = "".join(fx.canonical_json(r) + "\n" for r in rows).encode("utf-8")
    path.write_bytes(data)
    if sync:
        manifest = json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))
        manifest["files"][fx.ARTICLES_FILE]["sha256"] = hashlib.sha256(data).hexdigest()
        (dest / fx.MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


_DROP = object()


@pytest.mark.parametrize("sync_manifest", [False, True])
@pytest.mark.parametrize("changes, reason", [
    ({"title": f"{TITLE_MARK} 다른 제목"}, "제목"),                       # 제목은 클러스터 분할·프롬프트·임베딩 텍스트에 들어간다
    ({"note": "덧붙인 열"}, "열 구성"),
    ({"body_purged_at": _DROP}, "열 구성"),
    ({"body": None}, "정리 기록"),                                        # 정리 시각 없이 비운 본문
    ({"body_purged_at": "2026-10-07T00:00:00.000000Z"}, "정리 기록"),    # 본문이 있는데 정리됐다고 적은 행
])
def test_load_refuses_a_row_edited_outside_the_tool(tmp_path, changes, reason, sync_manifest):
    dest = _written(tmp_path)
    identity = json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))["identity_sha256"]
    _edit_row(dest, 1002, sync_manifest=sync_manifest, **changes)
    for opened in (lambda: fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc)),
                   lambda: fx.purge_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc), everything=True)):
        with pytest.raises(fx.ExportIntegrityError, match=reason):
            opened()
    assert json.loads((dest / fx.MANIFEST_FILE).read_text(encoding="utf-8"))["identity_sha256"] == identity


def test_a_body_blanked_by_hand_with_a_purge_time_is_not_adopted(tmp_path):
    """정리 기록 없이 본문만 비운 파일(매니페스트의 sha256과 다르다)은 끊긴 정리가 아니다."""
    dest = _written(tmp_path)
    _edit_row(dest, 1002, body=None, body_purged_at="2026-10-07T00:00:00.000000Z")
    with pytest.raises(fx.ExportIntegrityError, match="설명되지 않는다"):
        fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc))


def test_an_export_cut_off_before_the_body_file_never_opens_and_is_flagged_by_purge_all(tmp_path, monkeypatch, capsys):
    root = tmp_path / "data" / "exports"
    with monkeypatch.context() as m:
        _stop_before_writing(m, fx.ARTICLES_FILE)
        with pytest.raises(KeyboardInterrupt):
            fx.write_export(root / "cut", _bundle())
    assert (root / "cut" / fx.MANIFEST_FILE).exists()  # 매니페스트가 먼저라 정리가 이 디렉터리를 찾는다
    assert not (root / "cut" / fx.ARTICLES_FILE).exists()
    with pytest.raises(fx.ExportIntegrityError, match="읽을 수 없다"):
        fx.load_export(root / "cut", now=datetime(2026, 10, 7, tzinfo=timezone.utc))

    orphan = root / "orphan"  # 매니페스트 없이 본문 파일만 남은 디렉터리
    orphan.mkdir()
    (orphan / fx.ARTICLES_FILE).write_text("{}\n", encoding="utf-8")
    assert fx.main(["purge", "--all", "--root", str(root)]) == 1
    swept = {Path(r["dir"]).name: r for r in json.loads(capsys.readouterr().out)["exports"]}
    assert set(swept) == {"cut", "orphan"} and all("error" in r for r in swept.values())


def test_opening_removes_temporary_files_left_by_an_interrupted_write(tmp_path):
    dest = _written(tmp_path)
    leftover = dest / (fx.ARTICLES_FILE + ".tmp")
    leftover.write_text(_body(1), encoding="utf-8")
    fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc))
    assert not leftover.exists()


def test_redistributable_rows_keep_their_bodies_after_thirty_days(tmp_path):
    dest = _written(tmp_path, [_server_row(1, press="정책브리핑"), _server_row(2)])
    loaded = fx.load_export(dest, now=datetime(2027, 1, 1, tzinfo=timezone.utc))
    assert [r["body"] is None for r in loaded.rows] == [False, True]


# ---------------------------------------------------------------- E0 날짜 창: 계획과 서버 스냅숏


def test_planned_export_records_the_plan_and_the_runner_gets_the_same_day_windows(tmp_path):
    window, plan = fx.plan_windows(datetime(2026, 10, 6, 16, 0, tzinfo=KST), 3)  # 스냅숏 10-06 16:21 KST와 같은 날
    bundle = fx.build_export(fx.parse_copy_stream(_stream([_server_row(1)])), window, code=CODE, plan=plan)
    assert bundle.manifest["identity"]["plan"] == plan
    dest = tmp_path / "data" / "exports" / "planned"
    fx.write_export(dest, bundle)

    loaded = fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc))
    assert [(w.start, w.end) for w in loaded.day_windows()] == [
        (datetime(2026, 10, 1, 21, 0), datetime(2026, 10, 2, 21, 0)),
        (datetime(2026, 10, 2, 21, 0), datetime(2026, 10, 3, 21, 0)),
        (datetime(2026, 10, 3, 21, 0), datetime(2026, 10, 4, 21, 0)),
    ]
    assert fx.summarize(dest, loaded.manifest)["plan"]["t0_kst"] == "2026-10-06T16:00:00+09:00"
    unplanned = fx.load_export(_written(tmp_path), now=datetime(2026, 10, 7, tzinfo=timezone.utc))
    with pytest.raises(fx.ExportError, match="--days"):
        unplanned.day_windows()


def test_export_planned_before_kst_midnight_and_run_after_it_is_refused_before_writing(tmp_path):
    """런북 9.1·9.3대로면 생길 수 있던 일: KST 자정 전에 계획하고 자정 뒤에 반출하면 러너가 T0로 다시 계산한 마지막 창이 반출 밖에 놓인다."""
    window, plan = fx.plan_windows(datetime(2026, 10, 5, 23, 50, tzinfo=KST), 3)  # 스냅숏은 10-06 16:21 KST
    command = _fake_psql(tmp_path, _stream([_server_row(1)]))
    with pytest.raises(fx.WindowRefused, match="KST 날짜가 다르다"):
        fx.export_news_raw(window, root=tmp_path / "data" / "exports", psql_command=command, code=CODE, plan=plan)
    assert not (tmp_path / "data" / "exports").exists() or not any((tmp_path / "data" / "exports").iterdir())
    with pytest.raises(fx.WindowRefused):  # 계획과 다른 창을 함께 주는 것도 받지 않는다
        fx.build_export(fx.parse_copy_stream(_stream([_server_row(1)])), WINDOW, code=CODE,
                        plan=fx.plan_windows(datetime(2026, 10, 6, 16, 0, tzinfo=KST), 3)[1])


# ---------------------------------------------------------------- 실행 경로: psql 명령, CLI


def _fake_psql(tmp_path, lines, *, exit_code=0, stderr=""):
    """표준입력의 스크립트를 파일에 남기고 준비한 COPY 출력을 내보내는 가짜 psql."""
    out = tmp_path / "copy_out.bin"
    out.write_bytes(b"\n".join(lines) + b"\n")
    script = tmp_path / "fake_psql.py"
    script.write_text(textwrap.dedent(f"""
        import sys
        open({str(tmp_path / 'received.sql')!r}, "w", encoding="utf-8").write(sys.stdin.read())
        sys.stdout.buffer.write(open({str(out)!r}, "rb").read())
        sys.stderr.write({stderr!r})
        sys.exit({exit_code})
    """), encoding="utf-8")
    return [sys.executable, str(script)]


def test_export_through_a_psql_command_sends_the_read_only_script_and_prints_no_article_text(tmp_path, capsys, caplog):
    command = _fake_psql(tmp_path, _stream([_server_row(k) for k in (2, 1)]))
    root = tmp_path / "data" / "exports"
    caplog.set_level("DEBUG")

    summary = fx.export_news_raw(WINDOW, root=root, psql_command=command, code=CODE)

    dest = root / "20261006T072130Z"  # 서버 스냅숏 시각(T0)이 디렉터리 이름이다
    assert summary["dir"] == str(dest) and summary["rows"] == 2
    assert (tmp_path / "received.sql").read_text(encoding="utf-8") == fx.build_psql_script(WINDOW)
    assert fx.load_export(dest, now=datetime(2026, 10, 7, tzinfo=timezone.utc)).rows[0]["body"] == _body(1)
    shown = capsys.readouterr()
    for text in (shown.out, shown.err, caplog.text, json.dumps(summary, ensure_ascii=False)):
        assert BODY_MARK not in text and TITLE_MARK not in text


def test_export_reports_psql_failure_without_writing_anything(tmp_path):
    command = _fake_psql(tmp_path, _stream([_server_row(1)]), exit_code=3,
                         stderr="ERROR:  cannot execute UPDATE in a read-only transaction\n")
    root = tmp_path / "data" / "exports"
    with pytest.raises(fx.ExportError, match="read-only transaction"):
        fx.export_news_raw(WINDOW, root=root, psql_command=command, code=CODE)
    assert not root.exists() or not any(root.iterdir())


def test_export_needs_exactly_one_way_to_reach_the_database(tmp_path):
    with pytest.raises(fx.ExportError, match="하나"):
        fx.export_news_raw(WINDOW, root=tmp_path / "data" / "exports", code=CODE)


@pytest.mark.parametrize("where", [("anywhere",), ("Dropbox", "x", "data", "exports")])
def test_cli_refuses_a_root_that_is_not_the_main_checkout_exports_directory(tmp_path, capsys, where):
    command = _fake_psql(tmp_path, _stream([_server_row(1)]))
    root = tmp_path.joinpath(*where)
    argv = ["export", "--start-utc", "2026-10-02T21:00:00", "--end-utc", "2026-10-05T21:00:00",
            "--root", str(root), "--any-minute", "--psql-command", shlex.join(command)]
    assert fx.main(argv) == 2
    assert str((tmp_path / "data" / "exports").resolve()) in capsys.readouterr().err
    assert not root.exists() and not (tmp_path / "received.sql").exists()  # DB에 닿기 전에 멈춘다
    assert fx.main(["purge", "--all", "--root", str(root)]) == 2


def test_cli_export_verify_and_purge(tmp_path, capsys):
    command = _fake_psql(tmp_path, _stream([_server_row(k) for k in (1, 2)]))
    root = tmp_path / "data" / "exports"
    argv = ["export", "--start-utc", "2026-10-02T21:00:00", "--end-utc", "2026-10-05T21:00:00",
            "--root", str(root), "--any-minute", "--psql-command", shlex.join(command)]
    assert fx.main(argv) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"] == 2 and summary["body_expires_at_utc"] == "2026-11-02T01:00:00.000000Z"
    assert len(summary["identity_sha256"]) == 64

    assert fx.main(["verify", "--dir", summary["dir"]]) == 0
    assert json.loads(capsys.readouterr().out)["identity_sha256"] == summary["identity_sha256"]

    assert fx.main(["purge", "--dir", summary["dir"], "--everything"]) == 0
    assert json.loads(capsys.readouterr().out)["rows_with_body"] == 0
    assert BODY_MARK not in (root / "20261006T072130Z" / fx.ARTICLES_FILE).read_text(encoding="utf-8")


def test_cli_export_outside_the_gap_between_ingest_runs_is_refused_unless_overridden(tmp_path, capsys, monkeypatch):
    command = _fake_psql(tmp_path, _stream([_server_row(1)]))
    argv = ["export", "--start-utc", "2026-10-02T21:00:00", "--end-utc", "2026-10-05T21:00:00",
            "--root", str(tmp_path / "data" / "exports"), "--psql-command", shlex.join(command)]
    monkeypatch.setattr(fx, "_minute_now", lambda: 5)  # 정시 ingest가 도는 시간대
    assert fx.main(argv) == 2
    assert ":20" in capsys.readouterr().err
    monkeypatch.setattr(fx, "_minute_now", lambda: 35)
    assert fx.main(argv) == 0


def test_cli_export_with_days_computes_the_e0_windows_itself(tmp_path, capsys):
    command = _fake_psql(tmp_path, _stream([_server_row(k) for k in (1, 2)]))
    base = ["export", "--any-minute", "--psql-command", shlex.join(command)]

    assert fx.main(base + ["--days", "3"]) == 0  # T0 = 지금(시계 10-06 16:00 KST)
    summary = json.loads(capsys.readouterr().out)
    assert summary["window_utc"] == ["2026-10-01T21:00:00.000000Z", "2026-10-04T21:00:00.000000Z"]
    assert summary["plan"]["days"] == 3 and summary["plan"]["t0_kst"] == "2026-10-06T16:00:00+09:00"
    assert summary["dir"] == str((tmp_path / "data" / "exports" / "20261006T072130Z").resolve())

    assert fx.main(base + ["--days", "3", "--t0-kst", "2026-10-05T23:50"]) == 2
    assert "KST 날짜가 다르다" in capsys.readouterr().err
    for wrong in (["--days", "3", "--start-utc", "2026-10-02T21:00:00", "--end-utc", "2026-10-05T21:00:00"],
                  ["--t0-kst", "2026-10-06T16:00", "--start-utc", "2026-10-02T21:00:00", "--end-utc", "2026-10-05T21:00:00"],
                  ["--start-utc", "2026-10-02T21:00:00"], []):
        assert fx.main(base + wrong) == 2
        assert "거절" in capsys.readouterr().err


def test_cli_plan_window_prints_the_union_and_the_day_windows(capsys):
    assert fx.main(["plan-window", "--t0-kst", "2026-10-08T14:00", "--days", "3"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["start_utc"] == "2026-10-03T21:00:00" and plan["end_utc"] == "2026-10-06T21:00:00"
    assert plan["pseudo_now_kst"] == ["2026-10-05T06:00:00+09:00", "2026-10-06T06:00:00+09:00",
                                      "2026-10-07T06:00:00+09:00"]


def test_cli_purge_all_sweeps_every_export_under_the_root_and_flags_unreadable_ones(tmp_path, capsys):
    root = tmp_path / "data" / "exports"
    old = _server_row(1, crawled="2026-10-02T21:00:00.000000Z")
    fx.write_export(root / "a", _bundle([old]))
    fx.write_export(root / "b", _bundle([old, _server_row(2, crawled="2026-10-05T12:00:00.000000Z")]))

    assert fx.main(["purge", "--all", "--root", str(root), "--everything"]) == 0
    swept = json.loads(capsys.readouterr().out)["exports"]
    assert [(r["rows_blanked"], r["rows_with_body"]) for r in swept] == [(1, 0), (2, 0)]

    (root / "b" / fx.ARTICLES_FILE).write_text("깨진 파일\n", encoding="utf-8")
    assert fx.main(["purge", "--all", "--root", str(root)]) == 1
    swept = json.loads(capsys.readouterr().out)["exports"]
    assert "error" in swept[1] and "error" not in swept[0]
