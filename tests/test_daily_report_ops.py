"""daily_report의 운영 줄 세 개(ADR 0026 결정 6) - 정시 회차, 호스트 밖 백업 경과, 유휴 회수 위험. DB 없이."""
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobs.schedule import expected_slots, match_slots, parse_crontab
from jobs.tasks import daily_report as dr

REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def _t(day, hour, minute=0, second=0):
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


# ----------------------------------------------------------------------------- crontab 해석


def test_parse_crontab_reads_only_jobs_run_lines_and_flags_unsupported_fields():
    text = """
# 주석
SHELL=/bin/sh
CRON_TZ=UTC
5 * * * * python -m jobs.run ingest --stages rss,extract --workers 2
*/15 2-4 * * * python -m jobs.run popularity
10 0 * * * python -m jobs.run daily_report
50 17 * * 1 python -m jobs.run retention
0 0 * * * echo not-a-job
"""
    entries, unsupported = parse_crontab(text)
    assert [(e.job, e.minutes, e.hours) for e in entries] == [
        ("ingest", (5,), tuple(range(24))),
        ("popularity", (0, 15, 30, 45), (2, 3, 4)),
        ("daily_report", (10,), (0,)),
    ]
    assert unsupported == ["50 17 * * 1 python -m jobs.run retention"]


@pytest.mark.parametrize("path", ["docker/crontab", "docker/crontab.micro"])
def test_every_jobs_run_line_in_the_repo_crontabs_is_countable(path):
    entries, unsupported = parse_crontab((REPO / path).read_text(encoding="utf-8"))
    assert unsupported == []
    jobs = {e.job for e in entries}
    assert {"ingest", "popularity", "daily_report", "retention"} <= jobs
    hourly = [e for e in entries if e.job == "ingest"]
    assert hourly and all(len(e.hours) == 24 for e in hourly)


def test_expected_slots_counts_24_hourly_ingest_runs_in_a_day_and_skips_disabled_jobs():
    entries, _ = parse_crontab("5 * * * * python -m jobs.run ingest\n10 0 * * * python -m jobs.run daily_report\n"
                               "20 * * * * python -m jobs.run embed\n")
    now = _t(18, 0, 10, 3)  # daily_report 시각
    slots = expected_slots(entries, now - timedelta(hours=24), now, disabled=["embed"])
    assert len(slots["ingest"]) == 24
    assert slots["ingest"][0] == _t(17, 1, 5) and slots["ingest"][-1] == _t(18, 0, 5)
    assert slots["daily_report"] == [_t(18, 0, 10)]
    assert "embed" not in slots


def test_match_slots_counts_started_runs_within_tolerance_and_lists_skipped_slots():
    slots = [_t(17, h, 5) for h in range(1, 6)]
    runs = [
        (_t(17, 1, 5, 1), "succeeded"),
        (_t(17, 2, 5, 0), "failed"),
        # 03:05 회차는 앞 회차가 길어 시작되지 않았다(행 없음)
        (_t(17, 4, 30), "succeeded"),  # 손으로 돌린 실행 - 04:05 회차로 치지 않는다
        (_t(17, 5, 5, 30), "succeeded"),
    ]
    m = match_slots(slots, runs)
    assert (m["scheduled"], m["started"], m["succeeded"]) == (5, 3, 2)
    assert m["missed"] == [_t(17, 3, 5), _t(17, 4, 5)]


# ----------------------------------------------------------------------------- 줄 문구


def _base_report(**extra):
    return {
        "job_runs": {"ingest": {"succeeded": 20}},
        "news_raw_24h": {"total": 0, "ok": 0, "dropped": 0, "empty": 0, "fetch_failed": 0, "error": 0,
                         "duplicate": 0, "embedded": 0, "per_press": {}},
        "totals": {"news_raw": 10, "embedded": 0, "newsletters_24h": 0},
        **extra,
    }


def test_schedule_line_shows_started_over_scheduled_and_skipped_hours():
    schedule = {"crontab": "crontab.micro", "unsupported": [], "disabled": [], "jobs": {
        "popularity": {"scheduled": 24, "started": 24, "succeeded": 24, "missed": []},
        "ingest": {"scheduled": 24, "started": 21, "succeeded": 19,
                   "missed": [_t(17, 3, 5).isoformat(), _t(17, 7, 5).isoformat(), _t(17, 11, 5).isoformat()]},
    }}
    text = dr.format_report(_base_report(schedule=schedule))
    assert ("- 정시 회차(최근 24시간, crontab.micro 기준 시작/예정): ingest 21/24(건너뜀 3: 03:05·07:05·11:05 UTC, "
            "성공 19), popularity 24/24") in text


def test_schedule_line_names_the_deploy_time_when_counting_started_after_it():
    line = dr.format_schedule({"crontab": "crontab.micro", "unsupported": [], "disabled": [],
                               "counted_since": _t(16, 19, 35, 1).isoformat(),
                               "jobs": {"retention": {"scheduled": 0, "started": 0, "succeeded": 0, "missed": []}}})
    assert line.startswith("- 정시 회차(최근 24시간, 이 코드가 처음 돈 10-16 19:35 UTC부터, crontab.micro 기준")
    assert "retention" not in line  # 창 안에 예정 회차가 없는 잡은 적지 않는다


class _Cur:
    """schedule_status가 부르는 cursor 흉내: job_runs 조회 결과만 돌려준다."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        self.params = params

    def fetchall(self):
        return self.rows


def test_schedule_status_does_not_count_slots_before_this_image_first_ran():
    """배포로 생긴 retention 줄(17:50)의 배포 전 회차를 '건너뜀'으로 세지 않는다(2026-10-17 VM에서 본 경우)."""
    crontab = ("crontab.micro", "50 17 * * * python -m jobs.run retention\n35 * * * * python -m jobs.run popularity\n")
    now = _t(17, 6, 50)
    deployed = _t(16, 19, 35, 1)
    runs = [("popularity", _t(16, h, 35, 1) if h >= 7 else _t(17, h, 35, 1), "succeeded") for h in range(24)]
    unbounded = dr.schedule_status(_Cur(runs), now, crontab)
    assert unbounded["jobs"]["retention"]["missed"] and unbounded["counted_since"] is None
    bounded = dr.schedule_status(_Cur(runs), now, crontab, since=deployed)
    assert bounded["jobs"]["retention"]["scheduled"] == 0
    assert bounded["jobs"]["popularity"]["scheduled"] == 12  # 19:35 ~ 06:35
    assert bounded["counted_since"] == deployed.isoformat()


def test_schedule_line_without_crontab_says_it_did_not_count():
    assert "세지 않음(SCHEDULER_CRONTAB 없음" in dr.format_schedule({"crontab": None})


def test_schedule_line_caps_the_list_of_skipped_hours():
    missed = [_t(17, h, 5).isoformat() for h in range(10)]
    line = dr.format_schedule({"crontab": "c", "unsupported": [], "disabled": ["embed"], "jobs": {
        "ingest": {"scheduled": 24, "started": 14, "succeeded": 14, "missed": missed}}})
    assert "건너뜀 10: 00:05·01:05·02:05·03:05·04:05·05:05 외 4 UTC" in line
    assert line.endswith("꺼 둔 잡: embed")


def _backup(hours_since=6.1, **extra):
    now = _t(18, 0, 10)
    base = {
        "last_success_at": (now - timedelta(hours=hours_since)).isoformat(), "hours_since": hours_since,
        "failed_24h": 0, "rows_since": 412, "nobody_bytes": 3_768_320, "full_bytes": 42_311_680,
        "full_body_rows": 27427, "warnings": [],
        "restore_check_at": (now - timedelta(days=3)).isoformat(), "restore_check_days": 3.0,
    }
    base.update(extra)
    return base


def test_backup_line_shows_age_sizes_rows_since_and_last_restore_check():
    line = dr.format_backup(_backup())
    assert line.startswith("- 호스트 밖 백업(OCI Object Storage, 런북 6.5): 마지막 성공 6.1시간 전(10-17 18:04 UTC")
    assert "본문 없는 사본 3.8 MB · 최근 본문 사본 42.3 MB/27427건" in line
    assert "그 뒤 수집 412건, 최근 24시간 실패 0 / 복원 검증 마지막 10-15 00:10 UTC(3일 전)" in line


def test_backup_line_without_any_backup_says_so():
    assert "기록 없음(job_runs에 'backup' 성공이 없다" in dr.format_backup({"last_success_at": None, "failed_24h": 0})
    assert "실패 2" in dr.format_backup({"last_success_at": None, "failed_24h": 2})


@pytest.mark.parametrize("verdict,hours_since,expected", [
    ("idle", 6.0, "주의"),
    ("at_risk", 6.0, "주의"),
    ("idle", 40.0, "높음"),
    ("safe", 40.0, "낮음"),
])
def test_reclaim_risk_combines_the_idle_verdict_with_backup_age(verdict, hours_since, expected):
    idle = {"measured_at": _t(17, 18, 12).isoformat(), "hours_since": 6.0, "verdict": verdict,
            "cpu_p95": 7.89, "net_p95": 0.076, "window_days": 7.0}
    assert dr.reclaim_risk(idle, _backup(hours_since=hours_since))[0] == expected


def test_reclaim_risk_is_high_without_any_backup_and_unknown_without_a_fresh_measurement():
    idle = {"measured_at": _t(17, 18, 12).isoformat(), "hours_since": 6.0, "verdict": "idle"}
    assert dr.reclaim_risk(idle, {"last_success_at": None})[0] == "높음"
    assert dr.reclaim_risk(dict(idle, hours_since=49.0), _backup())[0] == "모름"
    assert dr.reclaim_risk({"measured_at": None}, _backup())[0] == "모름"
    assert dr.reclaim_risk(None, None)[0] == "모름"


def test_idle_line_names_the_measurement_and_what_a_reclaim_would_lose():
    idle = {"measured_at": _t(17, 18, 12).isoformat(), "hours_since": 6.0, "verdict": "idle",
            "cpu_p95": 7.89, "net_p95": 0.076, "window_days": 7.0}
    line = dr.format_idle(idle, _backup())
    assert line.startswith("- 유휴 회수 위험(ADR 0026 결정 6): 주의 - 기준상 유휴 - 손실은 마지막 백업 뒤 수집분으로")
    assert "(측정 10-17 18:12 UTC, 7.0일 창 CPU p95 7.89% · 네트워크 p95 0.076%, 기준 20%)" in line
    assert line.endswith("지금 회수되면 마지막 백업 뒤 수집 412건을 잃는다")


def test_report_without_ops_sections_keeps_the_old_layout():
    text = dr.format_report(_base_report())
    assert "정시 회차" not in text and "호스트 밖 백업" not in text and "유휴 회수" not in text


# ----------------------------------------------------------------------------- 경고


def test_no_warning_on_a_host_that_never_had_a_backup():
    assert dr.ops_warnings({"last_success_at": None, "failed_24h": 0}) == []
    assert dr.ops_warnings(None) == []


def test_warns_when_backup_is_overdue_failed_or_restore_check_is_missing():
    assert dr.ops_warnings(_backup()) == []
    overdue = dr.ops_warnings(_backup(hours_since=31))
    assert len(overdue) == 1 and "31시간째 없다" in overdue[0]
    assert any("실패 1회" in w for w in dr.ops_warnings(_backup(failed_24h=1)))
    assert any("한 번도 없다" in w for w in dr.ops_warnings(_backup(restore_check_at=None, restore_check_days=None)))
    assert any("36일째" in w for w in dr.ops_warnings(_backup(restore_check_days=36.0)))
    lifecycle = "full/ 객체가 80.0시간째 남아 있다"
    assert lifecycle in dr.ops_warnings(_backup(warnings=[lifecycle]))


def test_read_crontab_uses_the_path_the_entrypoint_exported(tmp_path, monkeypatch):
    crontab = tmp_path / "crontab.micro"
    crontab.write_text("5 * * * * python -m jobs.run ingest\n")
    monkeypatch.setenv("SCHEDULER_CRONTAB", str(crontab))
    assert dr.read_crontab() == ("crontab.micro", "5 * * * * python -m jobs.run ingest\n")
    monkeypatch.setenv("SCHEDULER_CRONTAB", str(tmp_path / "missing"))
    assert dr.read_crontab() is None
    monkeypatch.delenv("SCHEDULER_CRONTAB")
    assert dr.read_crontab() is None


# ----------------------------------------------------------------------------- 호스트 스크립트와 같은 이름


def _host_script():
    path = REPO / "scripts" / "oci" / "tier0_backup.py"
    spec = importlib.util.spec_from_file_location("tier0_backup_for_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_reads_the_job_names_the_host_backup_script_writes():
    host = _host_script()
    assert (dr.HOST_BACKUP_JOB, dr.IDLE_CHECK_JOB, dr.RESTORE_CHECK_JOB) == (
        host.HOST_BACKUP_JOB, host.IDLE_CHECK_JOB, host.RESTORE_CHECK_JOB)
    # 복원 검증 주기(28일)가 경고 기준(35일)보다 짧아야 한 번 거른 것만 경고한다
    assert host.RESTORE_CHECK_EVERY_DAYS < dr.RESTORE_CHECK_OVERDUE_DAYS


def test_backup_overdue_threshold_covers_one_daily_cron_interval():
    cron = (REPO / "scripts" / "oci" / "tier0-backup.cron").read_text(encoding="utf-8")
    lines = [l for l in cron.splitlines() if l.strip() and not l.startswith("#") and "tier0_backup.py" in l]
    assert len(lines) == 1 and lines[0].split()[2:5] == ["*", "*", "*"]  # 매일
    assert 24 < dr.BACKUP_OVERDUE_HOURS < 48
