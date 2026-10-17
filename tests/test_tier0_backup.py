"""scripts/oci/tier0_backup.py - DB·OCI 없이 검증하는 규칙(ADR 0026 결정 6, ADR 0023). 실제 덤프·복원은
tests/integration/test_tier0_backup_restore.py가 CI의 PostgreSQL에서 본다."""
import gzip
import importlib.util
import json
import os
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("tier0_backup", REPO / "scripts" / "oci" / "tier0_backup.py")
tb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tb)

UTC = timezone.utc


def test_retention_constants_match_the_repo_rules():
    from config.retention import BODY_RETENTION_DAYS
    from jobs.tasks.retention import EXEMPT_SQL

    assert tb.BODY_RETENTION_DAYS == BODY_RETENTION_DAYS == 30
    assert tb.EXEMPT_SQL_TEMPLATE == EXEMPT_SQL
    assert tb.BODY_BACKUP_MAX_AGE_DAYS == 27


def test_exempt_presses_are_the_redistributable_sources():
    from config.retention import exempt_press_names

    assert sorted(tb.EXEMPT_PRESS_NAMES) == sorted(exempt_press_names())


def test_exempt_sql_quotes_press_names():
    assert tb.exempt_sql(["정책브리핑", "a'b"]) == (
        "(p.press_name = ANY(ARRAY['정책브리핑', 'a''b']::text[]) AND COALESCE(n.raw_news_kogl_type, 1) = 1)")


def test_nobody_copy_never_selects_the_body_or_search_vector():
    cols = ["raw_news_id", "press_id", "raw_news_title", "raw_news_content", "raw_news_url", "embedding_result",
            "search_vector", "raw_news_content_sha256", "raw_news_content_length"]
    sql = tb.nobody_copy_sql(cols)
    assert "search_vector" not in sql
    assert "n.\"raw_news_content\"" not in sql
    assert "CASE WHEN n.raw_news_content = '' THEN '' END AS raw_news_content" in sql
    assert tb.nobody_copy_columns(cols) == [c for c in cols if c != "search_vector"]
    with pytest.raises(tb.BackupError):
        tb.nobody_copy_sql(["raw_news_id"])


def test_body_copy_takes_recent_or_redistributable_bodies_only():
    sql = tb.bodies_copy_sql("2026-09-20T06:00:00")
    assert "n.raw_news_crawled_at > '2026-09-20T06:00:00'::timestamp" in sql
    assert tb.exempt_sql() in sql
    assert "n.raw_news_content IS NOT NULL AND n.raw_news_content <> ''" in sql


def test_full_expiry_is_oldest_commercial_body_plus_30_days():
    snap = datetime(2026, 10, 17, 18, 10, tzinfo=UTC)
    oldest = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
    assert tb.full_expiry(oldest, snap) == datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
    assert tb.full_expiry(None, snap) is None


def test_full_copy_is_refused_when_a_body_would_outlive_its_30_days_before_the_lifecycle_rule_deletes_it():
    snap = datetime(2026, 10, 17, 18, 10, tzinfo=UTC)
    # 만료까지 3일이 안 남은 본문이 들어 있으면 1일 삭제 규칙(최대 약 2.x일)이 지우기 전에 30일을 넘길 수 있다
    with pytest.raises(tb.BackupError):
        tb.full_expiry(snap - timedelta(days=27, seconds=1), snap)
    assert tb.full_expiry(snap - timedelta(days=27) + timedelta(seconds=1), snap)


def test_object_names_carry_kind_time_and_expiry():
    snap = datetime(2026, 10, 17, 18, 10, 5, tzinfo=UTC)
    names = tb.object_names(snap, datetime(2026, 11, 13, 2, 0, tzinfo=UTC))
    assert names == {
        "nobody": "nobody/daily/tier0-20261017T181005Z.nobody.tar",
        "monthly": "nobody/monthly/tier0-2026-10.nobody.tar",
        "full": "full/tier0-20261017T181005Z.expires-2026-11-13.full.tar",
    }
    assert tb.object_names(snap, None)["full"].endswith(".no-commercial-bodies.full.tar")
    # 수명 주기 규칙의 접두어와 맞아야 지워진다
    assert names["full"].startswith(tb.FULL_PREFIX) and names["nobody"].startswith(tb.NOBODY_DAILY_PREFIX)


def test_archive_round_trip_checks_member_names_and_hashes(tmp_path):
    files = {}
    for name in ("db.dump", "news_raw.nobody.copy.gz"):
        p = tmp_path / name
        p.write_bytes(os.urandom(64))
        files[name] = str(p)
    manifest = {"format": tb.FORMAT, "kind": "nobody",
                "files": {n: {k: v for k, v in tb.file_digests(p).items() if k != "md5_b64"} for n, p in files.items()}}
    mpath = tmp_path / "manifest.json"
    mpath.write_text(tb.manifest_text(manifest))
    files["manifest.json"] = str(mpath)
    archive = tmp_path / "a.tar"
    tb.write_archive(str(archive), "nobody", files)
    assert tb.extract_archive(str(archive), str(tmp_path / "out"))["kind"] == "nobody"

    with pytest.raises(tb.BackupError):  # full 구성에 본문 파일이 빠짐
        tb.write_archive(str(tmp_path / "b.tar"), "full", files)

    # 내용을 바꾸면 sha256이 맞지 않는다
    Path(files["db.dump"]).write_bytes(b"tampered")
    tampered = tmp_path / "c.tar"
    tb.write_archive(str(tampered), "nobody", files)
    with pytest.raises(tb.BackupError):
        tb.extract_archive(str(tampered), str(tmp_path / "out2"))


def test_extract_refuses_paths_links_and_unknown_members(tmp_path):
    evil = tmp_path / "evil.tar"
    payload = tmp_path / "x"
    payload.write_bytes(b"x")
    with tarfile.open(evil, "w") as tar:
        tar.add(payload, arcname="manifest.json")
        tar.add(payload, arcname="db.dump")
        tar.add(payload, arcname="../news_raw.nobody.copy.gz")
    with pytest.raises(tb.BackupError):
        tb.extract_archive(str(evil), str(tmp_path / "out"))
    assert not (tmp_path / "news_raw.nobody.copy.gz").exists()


def test_snapshot_and_scratch_names_are_validated():
    assert tb.validate_snapshot_id("00000003-00013E37-1") == "00000003-00013E37-1"
    for bad in ("", "x'; DROP", "00000003-00013E37-1'"):
        with pytest.raises(tb.BackupError):
            tb.validate_snapshot_id(bad)
    assert tb.validate_scratch_name("restore_check", "newsletter") == "restore_check"
    for bad in ("newsletter", "restore-check", "Restore_check", "postgres", "restore_check; drop"):
        with pytest.raises(tb.BackupError):
            tb.validate_scratch_name(bad, "newsletter")
    with pytest.raises(tb.BackupError):  # 운영 DB 이름과 같으면 거절
        tb.validate_scratch_name("restore_check", "restore_check")


def test_restore_check_is_due_every_28_days():
    now = datetime(2026, 11, 14, tzinfo=UTC)
    assert tb.restore_check_due(None, now)
    assert not tb.restore_check_due(now - timedelta(days=27), now)
    assert tb.restore_check_due(now - timedelta(days=28), now)


def test_bucket_summary_groups_by_prefix_and_flags_a_stuck_lifecycle_rule():
    now = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
    objects = [
        {"name": "nobody/daily/a.tar", "size": 10, "time-created": "2026-10-19T18:11:00+00:00"},
        {"name": "nobody/monthly/b.tar", "size": 20, "time-created": "2026-10-01T18:11:00+00:00"},
        {"name": "full/c.tar", "size": 100, "time-created": "2026-10-19T18:11:00+00:00"},
        {"name": "full/d.tar", "size": 100, "time-created": "2026-10-16T18:11:00+00:00"},
    ]
    s = tb.bucket_summary(objects, now)
    assert s["total_bytes"] == 230 and s["full"] == {"objects": 2, "bytes": 200}
    assert s["oldest_full_age_hours"] == pytest.approx(95.8, abs=0.1)
    assert tb.lifecycle_warnings(s) and "full/" in tb.lifecycle_warnings(s)[0]
    assert tb.lifecycle_warnings(tb.bucket_summary(objects[:3], now)) == []


def test_clear_dir_removes_leftover_files_but_keeps_the_directory(tmp_path):
    (tmp_path / "full.tar").write_bytes(b"x")
    (tmp_path / "restore").mkdir()
    (tmp_path / "restore" / "news_raw.bodies.copy.gz").write_bytes(b"x")
    assert tb.clear_dir(str(tmp_path)) == ["full.tar", "restore"]
    assert list(tmp_path.iterdir()) == []


def test_copy_out_counts_rows_and_gzip_round_trips(tmp_path):
    db = tb.Db(exec_prefix=[], user=None, name="x")
    path = tmp_path / "out.gz"
    rows = tb.copy_out_gzip(db, ["printf", "1\\ta\\n2\\tb\\n"], str(path))
    assert rows == 2
    assert gzip.open(path).read() == b"1\ta\n2\tb\n"
    with pytest.raises(tb.BackupError):
        tb.copy_out_gzip(db, ["sh", "-c", "echo boom >&2; exit 3"], str(tmp_path / "bad.gz"))


class FakeUploader:
    def __init__(self, existing=(), corrupt=False):
        self.objects = {name: {"name": name, "size": 1, "md5": "x", "time-created": "2026-10-17T00:00:00+00:00"}
                        for name in existing}
        self.puts = []
        self.corrupt = corrupt

    def put(self, name, path, metadata):
        d = tb.file_digests(path)
        self.puts.append((name, metadata))
        self.objects[name] = {"name": name, "size": d["bytes"], "md5": "bad" if self.corrupt else d["md5_b64"],
                              "time-created": datetime.now(UTC).isoformat()}

    def list(self, prefix=""):
        return [o for n, o in sorted(self.objects.items()) if n.startswith(prefix)]


def _fake_build(tmp_path, monkeypatch):
    def build(db, work, **kw):
        archives = {}
        for kind in ("nobody", "full"):
            p = Path(work) / f"{kind}.tar"
            p.write_bytes(kind.encode() * 10)
            archives[kind] = str(p)
        snap = datetime(2026, 10, 17, 18, 10, tzinfo=UTC)
        stats = {"snapshot_at_utc": snap.isoformat(), "names": tb.object_names(snap, snap + timedelta(days=26)),
                 "nobody": {"bytes": 60}, "full": {"bytes": 40, "body_rows": 3}}
        return tb.BackupResult(stats=stats, archives=archives)
    monkeypatch.setattr(tb, "build_archives", build)


def test_run_backup_uploads_three_objects_once_a_month_and_removes_local_archives(tmp_path, monkeypatch):
    _fake_build(tmp_path, monkeypatch)
    up = FakeUploader()
    result = tb.run_backup(None, up, str(tmp_path))
    assert [n for n, _ in up.puts] == [
        "nobody/daily/tier0-20261017T181000Z.nobody.tar", "nobody/monthly/tier0-2026-10.nobody.tar",
        "full/tier0-20261017T181000Z.expires-2026-11-12.full.tar"]
    assert all(meta["sha256"] and meta["kind"] in ("nobody", "full") for _, meta in up.puts)
    assert result.stats["bucket"]["total_bytes"] == 60 + 60 + 40
    assert list(tmp_path.iterdir()) == []

    up2 = FakeUploader(existing=["nobody/monthly/tier0-2026-10.nobody.tar"])
    result = tb.run_backup(None, up2, str(tmp_path), keep=("nobody",))
    assert len(up2.puts) == 2 and "monthly" not in result.stats
    assert [p.name for p in tmp_path.iterdir()] == ["nobody.tar"]


def test_run_backup_fails_on_a_size_or_md5_mismatch_and_still_removes_the_body_archive(tmp_path, monkeypatch):
    _fake_build(tmp_path, monkeypatch)
    with pytest.raises(tb.BackupError):
        tb.run_backup(None, FakeUploader(corrupt=True), str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_main_clears_leftovers_and_records_a_failed_run(tmp_path, monkeypatch, capsys):
    work_root = tmp_path / "root"
    (work_root / "work").mkdir(parents=True)
    (work_root / "work" / "full.tar").write_bytes(b"left over from a crashed run")
    recorded = []

    class Recorder:
        def __init__(self, db, job, sha):
            self.job = job

        def start(self):
            recorded.append((self.job, "running"))

        def finish(self, status, stats, error=None):
            recorded.append((self.job, status, error))

    monkeypatch.setattr(tb, "JobRecorder", Recorder)

    def boom(*a, **kw):
        raise tb.BackupError("pg_dump 실패")
    monkeypatch.setattr(tb, "run_backup", boom)
    rc = tb.main(["--work-dir", str(work_root), "--namespace", "ns", "--db-exec", "", "backup"])
    assert rc == tb.EXIT_FAILED
    assert recorded == [("backup", "running"), ("backup", "failed", "BackupError: pg_dump 실패")]
    assert list((work_root / "work").iterdir()) == []
    out = capsys.readouterr().out
    assert "removed_leftovers" in out and "full.tar" in out


def test_main_refuses_a_second_concurrent_run(tmp_path):
    import fcntl

    (tmp_path / "work").mkdir()
    with open(tmp_path / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert tb.main(["--work-dir", str(tmp_path), "--db-exec", "", "backup", "--dry-run"]) == tb.EXIT_LOCKED


def test_cron_template_runs_daily_as_root_after_retention_and_before_the_reboot_window():
    cron = (REPO / "scripts" / "oci" / "tier0-backup.cron").read_text(encoding="utf-8")
    line = next(l for l in cron.splitlines() if "tier0_backup.py" in l and not l.startswith("#"))
    minute, hour, *_rest = line.split()
    assert (int(hour), int(minute)) == (18, 10)  # retention 17:50 UTC 뒤, 재부팅 창 18:40 UTC 앞
    assert line.split()[5] == "root" and line.rstrip().endswith(">> /var/log/newsletter-backup.log 2>&1")
    assert " daily" in line and "--config /etc/newsletter-backup.conf" in line
    micro = (REPO / "docker" / "crontab.micro").read_text(encoding="utf-8")
    assert "50 17 * * * python -m jobs.run retention" in micro


def test_backup_output_never_contains_body_text(tmp_path, monkeypatch, capsys):
    """표준출력(cron 로그)에 싣는 것은 이름·건수·크기·해시·시각뿐이다."""
    _fake_build(tmp_path, monkeypatch)

    class NullRecorder:
        def __init__(self, *a):
            pass

        def start(self):
            pass

        def finish(self, *a, **kw):
            pass
    monkeypatch.setattr(tb, "JobRecorder", NullRecorder)
    monkeypatch.setattr(tb, "Uploader", lambda **kw: FakeUploader())
    root = tmp_path / "r"
    assert tb.main(["--work-dir", str(root), "--namespace", "ns", "--db-exec", "", "backup"]) == 0
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event["status"] == "succeeded"
    assert set(event["stats"]) <= {"snapshot_at_utc", "names", "nobody", "full", "monthly", "bucket", "warnings"}


def test_restore_into_an_empty_db_records_its_run_only_after_the_schema_exists(tmp_path, monkeypatch):
    events = []

    class Recorder:
        def __init__(self, db, job, sha):
            events.append(("init", db.name, job))

        def start(self):
            events.append(("start",))

        def record(self, status, stats, error=None):
            events.append(("record", status))

        def finish(self, *a, **kw):
            events.append(("finish",))

    def fake_restore(db, archive, work, **kw):
        events.append(("restore", kw["target_db"], kw["scratch"], kw["with_bodies"]))
        return {"kind": "full"}

    monkeypatch.setattr(tb, "JobRecorder", Recorder)
    monkeypatch.setattr(tb, "restore_archive", fake_restore)
    rc = tb.main(["--work-dir", str(tmp_path), "--db-exec", "", "--db-name", "newsletter", "restore",
                  "--archive", "x.tar", "--target-db", "newsletter", "--with-bodies"])
    assert rc == tb.EXIT_OK
    # 빈 DB에는 job_runs가 없다 - 복원이 끝난 뒤 대상 DB에 한 행으로 남긴다(시작 행을 먼저 쓰지 않는다)
    assert events == [("init", "newsletter", "restore"), ("restore", "newsletter", False, True), ("record", "succeeded")]
