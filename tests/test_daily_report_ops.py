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


def _daily_cron_time(path, marker):
    """(시, 분) - 매일 한 번 도는 줄. 시각은 UTC(호스트·컨테이너 TZ)."""
    text = (REPO / path).read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#") and marker in l]
    assert len(lines) == 1, lines
    minute, hour, *rest = lines[0].split()
    assert rest[:3] == ["*", "*", "*"] and minute.isdigit() and hour.isdigit()  # 매일 한 번
    return int(hour), int(minute)


@pytest.mark.parametrize("duration", [timedelta(seconds=40), timedelta(minutes=3), timedelta(hours=1)])
def test_one_skipped_backup_warns_at_the_very_next_daily_report_and_a_normal_one_does_not(duration):
    """백업(18:10 UTC)과 리포트(00:10 UTC)의 위상을 두 crontab에서 읽는다. 정상이면 리포트 시점의 경과는 약 6시간,
    한 번 거르면 약 30시간이다 - 기준이 그 사이여야 한 번 거른 것을 바로 다음 리포트가 잡는다(기준 30시간이면 29.97시간이라
    놓치고 하루 뒤에야 경고했다). duration: 백업 시작에서 job_runs.finished_at까지(실측 약 1분, 넉넉하게 1시간까지)."""
    bh, bm = _daily_cron_time("scripts/oci/tier0-backup.cron", "tier0_backup.py")
    rh, rm = _daily_cron_time("docker/crontab.micro", "jobs.run daily_report")
    report = _t(18, rh, rm)
    last_start = report.replace(hour=bh, minute=bm)
    if last_start >= report:
        last_start -= timedelta(days=1)
    normal = (report - (last_start + duration)).total_seconds() / 3600
    skipped_once = normal + 24
    assert normal <= dr.BACKUP_OVERDUE_HOURS < skipped_once

    def overdue(hours):
        return [w for w in dr.ops_warnings(_backup(hours_since=hours)) if "호스트 밖 백업이" in w]

    assert overdue(normal) == []
    assert overdue(skipped_once) and f"{skipped_once:.0f}시간째 없다" in overdue(skipped_once)[0]
    # 유휴 회수 위험도 같은 기준이다: 한 번 거르면 '높음'
    idle = {"measured_at": report.isoformat(), "hours_since": 6.0, "verdict": "idle"}
    assert dr.reclaim_risk(idle, _backup(hours_since=normal))[0] == "주의"
    assert dr.reclaim_risk(idle, _backup(hours_since=skipped_once))[0] == "높음"


def test_runs_left_running_count_as_failures_with_their_own_note():
    warnings = dr.ops_warnings(_backup(failed_24h=1, stuck_24h=1, restore_failed_24h=2, restore_stuck_24h=2))
    backup = next(w for w in warnings if "호스트 밖 백업 실패" in w)
    assert "1회 - 그중 1회는 1시간 넘게 'running'으로 남은 행" in backup
    restore = next(w for w in warnings if "복원 검증 실패" in w)
    assert "2회 - 그중 2회는" in restore and "'restore_check'" in restore
    line = dr.format_backup(_backup(failed_24h=1, stuck_24h=1, restore_failed_24h=1))
    assert "최근 24시간 실패 1(그중 끝나지 않은 실행 1)" in line
    assert line.endswith("최근 24시간 복원 검증 실패 1")
    assert dr.HOST_RUN_STALE_HOURS <= 1  # 정상 실행은 백업 약 1분·복원 검증 약 20초


def test_backup_line_says_when_the_body_copy_is_switched_off():
    line = dr.format_backup(_backup(full_disabled=True, full_bytes=None, full_body_rows=None))
    assert "본문 없는 사본 3.8 MB · 본문 사본 꺼짐(full_enabled 아님)" in line
    assert "최근 본문 사본" not in line


def test_schedule_status_counts_utc_slots_even_when_the_db_session_is_not_utc():
    """DB의 now()는 세션 TimeZone의 aware 시각이다. KST로 받아도 하루 한 번 도는 잡의 회차는 crontab 그대로 UTC다."""
    kst = timezone(timedelta(hours=9))
    crontab = ("crontab.micro", "10 0 * * * python -m jobs.run daily_report\n50 17 * * * python -m jobs.run retention\n")
    now = _t(18, 6, 50).astimezone(kst)  # 15:50 KST
    runs = [("daily_report", _t(18, 0, 10, 2).astimezone(kst), "succeeded"),
            ("retention", _t(17, 17, 50, 1).astimezone(kst), "succeeded")]
    status = dr.schedule_status(_Cur(runs), now, crontab)
    assert status["jobs"]["daily_report"] == {"scheduled": 1, "started": 1, "succeeded": 1, "missed": []}
    assert status["jobs"]["retention"] == {"scheduled": 1, "started": 1, "succeeded": 1, "missed": []}
    since = dr.schedule_status(_Cur(runs), now, crontab, since=_t(17, 20, 0).astimezone(kst))
    assert since["jobs"]["daily_report"]["scheduled"] == 1 and since["jobs"]["retention"]["scheduled"] == 0
    assert since["counted_since"] == _t(17, 20, 0).isoformat()


class _RecordingCur:
    def __init__(self, row):
        self.row, self.calls = row, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row


def test_image_first_run_only_looks_at_the_scheduler_jobs_of_the_crontab():
    """같은 커밋으로 깐 호스트 스크립트('backup')나 일회성 잡이 먼저 돌았어도 스케줄러의 첫 잡을 기준으로 삼는다."""
    crontab = ("crontab.micro", "5 * * * * python -m jobs.run ingest --stages rss,extract\n"
                                "35 * * * * python -m jobs.run popularity\n50 17 * * 1 python -m jobs.run retention\n")
    assert dr.crontab_jobs(crontab) == ["ingest", "popularity", "retention"]
    assert dr.crontab_jobs(None) == []
    cur = _RecordingCur((_t(16, 19, 35, 1),))
    assert dr.image_first_run(cur, "abc1234", dr.crontab_jobs(crontab)) == _t(16, 19, 35, 1)
    sql, params = cur.calls[0]
    assert "job = ANY(%s)" in sql and params == ("abc1234", ["ingest", "popularity", "retention"])
    assert dr.image_first_run(_RecordingCur(None), "abc1234", []) is None  # crontab이 없으면 세지 않는다
    assert dr.image_first_run(_RecordingCur(None), None, ["ingest"]) is None


# ----------------------------------------------------------------------------- 같은 본문 반복 (ADR 0026 결정 9, 2026-10-25 절)
#
# 국민일보 2026-10-19: 페이지 템플릿이 바뀌어 모든 기사가 같은 특집 글을 받았고 duplicate로 지워졌다. 다운로드는 정상이라
# ingest는 매시 succeeded였고 리포트에도 본문 수만 줄었다. 리포트가 같은 본문 반복을 직접 보여 준다.


def _press(total, ok=0, dropped=0, empty=0, duplicate=0, embedded=0):
    return {"total": total, "ok": ok, "dropped": dropped, "empty": empty, "fetch_failed": 0, "error": 0,
            "duplicate": duplicate, "embedded": embedded}


def test_same_body_suspects_by_the_stored_rows_of_the_window():
    """2026-10-22 국민일보: 249건 중 duplicate 244건."""
    per_press = {"국민일보": _press(249, ok=5, duplicate=244), "세계일보": _press(200, ok=185, dropped=6, duplicate=9)}

    found = dr.same_body_suspects(per_press, {})

    assert found == {"국민일보": {"parsed": 249, "duplicate": 244, "ingest_runs": 0}}


def test_same_body_suspects_threshold_matches_the_ingest_job():
    from jobs.tasks.ingest import SAME_BODY_MIN_ARTICLES, SAME_BODY_RATIO_LIMIT

    assert (SAME_BODY_MIN_ARTICLES, SAME_BODY_RATIO_LIMIT) == (5, 0.5)
    assert dr.same_body_suspects({"A": _press(4, duplicate=4)}, {}) == {}  # 4건: 기준 미만
    assert dr.same_body_suspects({"A": _press(10, ok=6, duplicate=4)}, {}) == {}  # 40%
    assert "A" in dr.same_body_suspects({"A": _press(10, ok=5, duplicate=5)}, {})  # 정확히 절반
    # 다운로드 실패·추출 오류는 본문을 받은 기사가 아니다
    per_press = {"A": {**_press(30, ok=2, duplicate=2), "fetch_failed": 26}}
    assert dr.same_body_suspects(per_press, {}) == {}


def test_same_body_suspects_also_by_ingest_runs_that_already_warned():
    """하루로 보면 절반 아래여도(기사가 많은 날) 한 회차 안에서 절반을 넘은 실행이 있었으면 걸린다."""
    per_press = {"국민일보": _press(300, ok=250, duplicate=40)}

    found = dr.same_body_suspects(per_press, {"국민일보": 3, "세계일보": 1})

    assert found == {
        "국민일보": {"parsed": 290, "duplicate": 40, "ingest_runs": 3},
        "세계일보": {"parsed": 0, "duplicate": 0, "ingest_runs": 1},
    }


def test_report_marks_the_suspect_press_and_adds_a_warning_line():
    per_press = {"국민일보": _press(249, ok=5, duplicate=244), "세계일보": _press(150, ok=140)}
    suspect = dr.same_body_suspects(per_press, {"국민일보": 22})
    report = _base_report(suspect_press=suspect)
    report["news_raw_24h"] = {**report["news_raw_24h"], "per_press": per_press}

    lines = dr.format_report(report).splitlines()

    assert "  · 국민일보: 249건 (본문 5, 임베딩 0) ⚠ 같은 본문 반복 - 추출 이상 의심" in lines
    assert "  · 세계일보: 150건 (본문 140, 임베딩 0)" in lines  # 정상 언론사는 그대로
    assert ("- ⚠ 국민일보: 같은 본문 반복 - 추출 이상 의심 - 최근 24시간 본문을 받은 기사 249건 중 중복 244건, "
            "ingest 실행 22회가 같은 경고를 남겼다 (docs/runbook-hosting.md 6.8)") in lines


def test_report_without_suspects_keeps_the_old_layout():
    per_press = {"세계일보": _press(150, ok=140)}
    report = _base_report(suspect_press={})
    report["news_raw_24h"] = {**report["news_raw_24h"], "per_press": per_press}
    text = dr.format_report(report)
    assert "⚠" not in text and "  · 세계일보: 150건 (본문 140, 임베딩 0)" in text
    assert "⚠" not in dr.format_report(_base_report())  # suspect_press 키가 없는 예전 모양도 그대로


# ----------------------------------------------------------------------------- 새 상태 집계 (rss_summary·blocked_origin, ADR 0026 2026-10-25)


def _with_new_statuses(total, ok=0, rss_summary=0, blocked_origin=0, dropped=0, duplicate=0, embedded=0):
    return {**_press(total, ok=ok, dropped=dropped, duplicate=duplicate, embedded=embedded),
            "rss_summary": rss_summary, "blocked_origin": blocked_origin}


def test_press_line_shows_summary_and_blocked_rows_so_the_press_does_not_look_like_zero_body_failure():
    per_press = {
        "동아일보": _with_new_statuses(200, rss_summary=186, blocked_origin=4, dropped=6, duplicate=4, embedded=186),
        "세계일보": _with_new_statuses(150, ok=140, embedded=120),
    }
    news = {"total": 350, "ok": 140, "dropped": 6, "empty": 0, "fetch_failed": 0, "error": 0, "duplicate": 4,
            "rss_summary": 186, "blocked_origin": 4, "embedded": 306, "per_press": per_press}
    report = _base_report()
    report["news_raw_24h"] = news

    lines = dr.format_report(report).splitlines()

    assert "  · 동아일보: 200건 (본문 0, 요약 186, 출발지 거절 4, 임베딩 186)" in lines
    assert "  · 세계일보: 150건 (본문 140, 임베딩 120)" in lines  # 요약·거절이 없는 언론사의 줄은 그대로
    assert ("- 신규 기사 350건 (본문 140, 요약 186, 필터 6, 중복 4, 본문없음 0, 다운로드 실패 0, 추출 오류 0, "
            "출발지 거절 4, 임베딩 306)") in lines


def test_report_of_the_old_shape_without_the_new_keys_keeps_its_layout():
    report = _base_report()
    report["news_raw_24h"] = {**report["news_raw_24h"], "per_press": {"세계일보": _press(150, ok=140)}}

    lines = dr.format_report(report).splitlines()

    assert "- 신규 기사 0건 (본문 0, 필터 0, 중복 0, 본문없음 0, 다운로드 실패 0, 추출 오류 0, 임베딩 0)" in lines
    assert "  · 세계일보: 150건 (본문 140, 임베딩 0)" in lines


def test_summary_rows_count_as_received_text_for_the_same_body_check():
    """요약이 대부분인 하루(요약 190·필터 3·중복 3)가 '같은 본문 반복'으로 걸리면 안 된다 - 분모에 요약을 넣지 않으면
    필터 3 + 중복 3 = 6건 중 중복 3건(50%)으로 걸린다."""
    per_press = {"동아일보": _with_new_statuses(196, rss_summary=190, dropped=3, duplicate=3)}

    assert dr.same_body_suspects(per_press, {}) == {}


def test_a_summary_feed_repeating_one_summary_is_still_caught():
    per_press = {"동아일보": _with_new_statuses(50, rss_summary=1, duplicate=49)}

    assert dr.same_body_suspects(per_press, {}) == {
        "동아일보": {"parsed": 50, "duplicate": 49, "ingest_runs": 0}}


def test_news_status_keys_follow_the_column_order_of_the_report_query():
    """집계 SQL의 열 순서와 NEWS_STATUS_KEYS가 어긋나면 상태 건수가 엉뚱한 이름으로 들어간다 - DB 없이 쿼리 글자에서 순서를 읽어 대조한다."""
    import inspect
    import re

    source = inspect.getsource(dr.run)
    select = source[source.index("SELECT P.press_name"):source.index("FROM news_raw N JOIN press P")]
    columns = re.findall(r"COUNT\(\*\)(?: FILTER \(WHERE N\.(\w+) (?:= '(\w+)'|IS NOT NULL)\))?", select)
    names = []
    for column, status in columns:
        if column == "raw_news_extract_status":
            names.append(status)
        elif column == "embedding_result":
            names.append("embedded")
        else:
            names.append("total")
    assert tuple(names) == dr.NEWS_STATUS_KEYS


# ----------------------------------------------------------------------------- 본문 길이 급변 (ADR 0026 결정 6의 2026-10-25 절, 증거 14)
#
# 국민일보(2026-10-19)는 모든 기사가 특집 묶음 글을 받아 ok 본문 길이가 10배 넘게 부풀었고, 매일경제는 해설 모달이 본문에 섞여 일부가 3~4배로
# 부풀었다. 24시간 창의 건수로는 보이지 않아 언론사별 최근 7일과 그 앞 21일의 ok 본문 길이(p75)를 견준다.


def _win(recent_rows, recent, base_rows, base):
    return {"recent_rows": recent_rows, "recent": recent, "base_rows": base_rows, "base": base}


class _WindowsCur:
    """length_window_stats가 부르는 cursor 흉내: 질의와 인자를 기록하고 언론사별 창 요약 행을 돌려준다."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


def test_length_drift_thresholds_are_the_backtest_values():
    """ADR 0026 증거 14의 백테스트로 정한 값이다 - 바꾸려면 백테스트를 다시 하고 ADR의 표와 상수 옆 주석을 함께 고친다."""
    assert (dr.LENGTH_RECENT_DAYS, dr.LENGTH_BASE_DAYS, dr.LENGTH_QUANTILE) == (7, 21, 0.75)
    assert (dr.LENGTH_MIN_ROWS, dr.LENGTH_RATIO_LOW, dr.LENGTH_RATIO_HIGH) == (50, 0.5, 2.0)


def test_length_drift_flags_a_ratio_outside_the_band_in_both_directions():
    """2026-10-25 VM의 실제 값: 매일경제는 최근 7일 p75가 앞 21일의 0.28배(해설 모달 섞임이 줄어듦), 다른 언론사는 1.0 안팎.
    국민일보는 10-21 00:10 UTC에 되돌린 값(특집 묶음 글이 ok로 저장돼 있던 때) - 9.6배."""
    windows = {
        "매일경제": _win(2088, 1835.0, 5362, 6596.75),
        "경향신문": _win(1325, 1574.0, 3970, 1561.0),
        "국민일보": _win(118, 13160.0, 560, 1370.0),
        "AI타임스": _win(203, 1991.0, 565, 1850.0),
    }

    found = dr.length_drift_suspects(windows)

    assert sorted(found) == ["국민일보", "매일경제"]
    assert found["매일경제"] == {"recent_rows": 2088, "recent": 1835.0, "base_rows": 5362, "base": 6596.75, "ratio": 0.278}
    assert found["국민일보"]["ratio"] == 9.606


def test_length_drift_band_edges_are_inside_and_just_outside_is_flagged():
    inside = {"A": _win(60, 500.0, 60, 1000.0), "B": _win(60, 2000.0, 60, 1000.0)}  # 정확히 0.5배, 2배
    assert dr.length_drift_suspects(inside) == {}
    outside = {"A": _win(60, 499.0, 60, 1000.0), "B": _win(60, 2001.0, 60, 1000.0)}
    assert sorted(dr.length_drift_suspects(outside)) == ["A", "B"]


def test_length_drift_needs_enough_rows_in_both_windows_and_no_division_by_zero():
    huge = 10_000.0  # 비율은 대역 밖이지만 표본이 모자라면 말하지 않는다
    assert dr.length_drift_suspects({"A": _win(49, huge, 500, 1000.0)}) == {}  # 최근 창 49건
    assert dr.length_drift_suspects({"A": _win(500, huge, 49, 1000.0)}) == {}  # 앞 창 49건
    assert sorted(dr.length_drift_suspects({"A": _win(50, huge, 50, 1000.0)})) == ["A"]  # 정확히 50건씩은 센다
    # ok 본문이 사라진 언론사(동아일보가 RSS 요약으로 바뀐 뒤)나 앞 창이 비었거나 0인 언론사
    assert dr.length_drift_suspects({"동아일보": _win(0, None, 2742, 1451.5)}) == {}
    assert dr.length_drift_suspects({"A": _win(60, 900.0, 0, None)}) == {}
    assert dr.length_drift_suspects({"A": _win(60, 900.0, 60, 0.0)}) == {}
    assert dr.length_drift_suspects({}) == {}


def test_length_windows_query_reads_stored_lengths_so_purged_rows_still_count():
    """본문을 비운 행(raw_news_content NULL, 길이는 남음)도 센다: 질의는 길이 칸만 읽고 본문 칸·비운 시각으로 거르지 않는다."""
    import re

    cur = _WindowsCur([("경향신문", 1325, 1574.0, 3970, 1561.0), ("국민일보", 0, None, 12, 1380.75)])

    windows = dr.length_window_stats(cur)

    assert windows == {
        "경향신문": {"recent_rows": 1325, "recent": 1574.0, "base_rows": 3970, "base": 1561.0},
        "국민일보": {"recent_rows": 0, "recent": None, "base_rows": 12, "base": 1380.75},
    }
    sql, params = cur.calls[0]
    assert "raw_news_extract_status = 'ok'" in sql and "raw_news_content_length IS NOT NULL" in sql
    assert not re.search(r"\braw_news_content\b", sql) and "purged_at" not in sql
    assert "now() AT TIME ZONE 'UTC'" in sql  # raw_news_crawled_at은 시간대 없는 UTC 값이다
    assert params == {"quantile": 0.75, "recent_days": 7, "base_days": 21}
    assert dr.length_window_stats(_WindowsCur([])) == {}


def test_the_retention_job_keeps_the_length_of_the_bodies_it_purges():
    """길이 감시가 비운 행을 세는 근거: 보존 잡이 본문을 NULL로 바꿀 때 길이를 char_length로 다시 쓰고 지우지 않는다.
    이 가정이 깨지면(길이를 0·NULL로 두면) 30일이 지난 ok 행이 창에서 빠지거나 0으로 끌어내린다 - 질의를 고쳐야 한다."""
    from jobs.tasks.retention import PURGE_BATCH_SQL

    assert "raw_news_content_length = char_length(n.raw_news_content)" in PURGE_BATCH_SQL
    assert "raw_news_content_length = NULL" not in PURGE_BATCH_SQL and "raw_news_content_length = 0" not in PURGE_BATCH_SQL


def test_length_window_is_shorter_than_the_retention_period():
    """창(7+21=28일)이 보존 기한(30일)보다 짧아서 정상이면 비운 행은 창에 아예 들어오지 않는다. 보존 잡이 밀리거나 기한이
    짧아져도 위 테스트대로 길이가 남아 있어 센다."""
    from config.retention import BODY_RETENTION_DAYS

    assert dr.LENGTH_RECENT_DAYS + dr.LENGTH_BASE_DAYS <= BODY_RETENTION_DAYS


def test_length_drift_warning_text_names_the_press_both_windows_and_the_direction():
    shrunk = dr.length_drift_suspects({"매일경제": _win(2088, 1835.0, 5362, 6596.75)})["매일경제"]
    grown = dr.length_drift_suspects({"국민일보": _win(118, 13160.0, 560, 1370.0)})["국민일보"]

    assert dr.length_drift_warning("매일경제", shrunk) == (
        "매일경제: 본문 길이 급변 - ok 본문 길이 p75가 최근 7일 1,835자(2088건), 그 앞 21일 6,597자(5362건)의 0.28배로 "
        "짧아졌다 (기준 0.5~2배, docs/runbook-hosting.md 6.5 매일 확인)")
    assert "9.61배로 길어졌다" in dr.length_drift_warning("국민일보", grown)


def _report_with(per_press, **extra):
    report = _base_report(**extra)
    report["news_raw_24h"] = {**report["news_raw_24h"], "per_press": per_press}
    return report


def test_report_marks_the_press_whose_body_length_drifted_and_adds_a_warning_line():
    per_press = {"매일경제": _press(212, ok=200), "세계일보": _press(150, ok=140)}
    drift = dr.length_drift_suspects({"매일경제": _win(2088, 1835.0, 5362, 6596.75),
                                      "세계일보": _win(1639, 1541.0, 4330, 1555.0)})

    lines = dr.format_report(_report_with(per_press, length_drift=drift)).splitlines()

    assert "  · 매일경제: 212건 (본문 200, 임베딩 0) ⚠ 본문 길이 급변" in lines
    assert "  · 세계일보: 150건 (본문 140, 임베딩 0)" in lines  # 정상 언론사의 줄은 그대로
    warning = [l for l in lines if l.startswith("- ⚠ ")]
    assert warning == ["- ⚠ " + dr.length_drift_warning("매일경제", drift["매일경제"])]


def test_report_shows_both_marks_when_the_same_body_check_and_the_length_check_both_fire():
    per_press = {"국민일보": _press(249, ok=5, duplicate=244)}
    suspect = dr.same_body_suspects(per_press, {"국민일보": 22})
    drift = dr.length_drift_suspects({"국민일보": _win(118, 13160.0, 560, 1370.0)})

    lines = dr.format_report(_report_with(per_press, suspect_press=suspect, length_drift=drift)).splitlines()

    assert "  · 국민일보: 249건 (본문 5, 임베딩 0) ⚠ 같은 본문 반복 - 추출 이상 의심 ⚠ 본문 길이 급변" in lines
    assert sum(1 for l in lines if l.startswith("- ⚠ ")) == 2  # 경고 줄은 검사마다 하나


def test_report_warns_about_a_press_that_had_no_new_rows_today():
    """길이 감시는 7일·28일 창이라 오늘 새 기사가 없는 언론사도 걸린다 - 언론사 줄이 없어도 경고 줄은 나온다."""
    drift = dr.length_drift_suspects({"매일경제": _win(2088, 1835.0, 5362, 6596.75)})

    lines = dr.format_report(_report_with({"세계일보": _press(150, ok=140)}, length_drift=drift)).splitlines()

    assert not any(l.startswith("  · 매일경제") for l in lines)
    assert any(l.startswith("- ⚠ 매일경제: 본문 길이 급변") for l in lines)


def test_report_without_length_drift_keeps_the_old_layout():
    per_press = {"세계일보": _press(150, ok=140)}
    for report in (_report_with(per_press), _report_with(per_press, length_drift={})):  # 키가 없는 예전 모양도
        text = dr.format_report(report)
        assert "⚠" not in text and "본문 길이 급변" not in text


class _ScriptedCur:
    """run()이 보내는 질의에 글자 조각으로 답하는 cursor 흉내. 모르는 질의는 실패시킨다."""

    def __init__(self, script):
        self.script, self.rows, self.sql = script, [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        for needle, rows in self.script:
            if needle in sql:
                self.rows = rows
                return
        raise AssertionError(f"예상 밖의 질의: {' '.join(sql.split())[:100]}")

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Ctx:
    def __init__(self):
        self.warnings = []

    def warn(self, message):
        self.warnings.append(message)


def _run_report(monkeypatch, length_rows):
    """run()을 가짜 DB로 끝까지 돌린다: 언론사 둘, 길이 창 요약은 length_rows."""
    import contextlib

    import db.connection
    import jobs.notify

    cur = _ScriptedCur([
        ("SELECT job, status, COUNT(*) FROM job_runs", [("ingest", "succeeded", 24)]),
        ("'dropped'", [("매일경제", 212, 200, 3, 0, 0, 0, 5, 0, 0, 180), ("세계일보", 150, 140, 2, 0, 0, 0, 8, 0, 0, 120)]),
        ("SELECT (SELECT COUNT(*) FROM news_raw)", [(43000, 36000, 3)]),
        ("raw_news_content_purged_at >=", [(0, 0, 0, 0, None)]),
        ("suspect_press", []),
        ("percentile_cont", length_rows),
        ("recommendation_impression_log", []),
        ("SELECT now()", [(_t(25, 0, 10),)]),
        ("status = 'succeeded'", []),
        ("status IN ('failed', 'abandoned')", [(0, 0)]),
    ])

    class _Conn:
        def cursor(self):
            return cur

    @contextlib.contextmanager
    def fake_connection():
        yield _Conn()

    posted = []
    monkeypatch.setattr(db.connection, "get_db_connection", fake_connection)
    monkeypatch.setattr(jobs.notify, "post_message", lambda text: posted.append(text) or False)
    monkeypatch.delenv("SCHEDULER_CRONTAB", raising=False)
    ctx = _Ctx()
    return dr.run(ctx), ctx, cur, posted


def test_run_warns_once_per_press_whose_body_length_drifted_and_reports_it(monkeypatch, capsys):
    length_rows = [("매일경제", 2088, 1835.0, 5362, 6596.75), ("세계일보", 1639, 1541.0, 4330, 1555.0)]

    result, ctx, cur, posted = _run_report(monkeypatch, length_rows)

    expected = dr.length_drift_warning("매일경제", result["report"]["length_drift"]["매일경제"])
    assert ctx.warnings == [expected]
    assert list(result["report"]["length_drift"]) == ["매일경제"] and result["report"]["length_drift"]["매일경제"]["ratio"] == 0.278
    printed = capsys.readouterr().out
    assert "  · 매일경제: 212건 (본문 200, 임베딩 180) ⚠ 본문 길이 급변" in printed and f"- ⚠ {expected}" in printed
    assert posted == [printed.rstrip("\n")]  # Slack 웹훅으로 가는 글은 로그에 찍힌 리포트와 같다
    assert sum("percentile_cont" in sql for sql in cur.sql) == 1  # 하루 한 번, 질의 하나


def test_run_stays_quiet_when_no_press_drifted_or_the_samples_are_short(monkeypatch, capsys):
    length_rows = [("세계일보", 1639, 1541.0, 4330, 1555.0), ("동아일보", 30, 9000.0, 2742, 1451.5)]  # 정상 / 표본 부족

    result, ctx, _, _ = _run_report(monkeypatch, length_rows)

    assert ctx.warnings == [] and result["report"]["length_drift"] == {}
    assert "본문 길이 급변" not in capsys.readouterr().out
