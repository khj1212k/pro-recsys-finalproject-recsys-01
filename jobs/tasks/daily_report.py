"""daily_report: 최근 24시간 잡 실행/수집/추천 응답 현황 요약을 로그(+Slack 웹훅)로 남긴다(읽기 전용).

운영 줄 세 개(ADR 0026 결정 6): 정시 회차(예정 vs 시작 - 건너뛴 회차는 job_runs에 행이 없다), 호스트 밖 백업의
경과 시간, 유휴 회수 위험. 뒤의 둘은 VM 호스트의 cron이 돌리는 scripts/oci/tier0_backup.py가 job_runs에 남긴
'backup'·'idle_check'·'restore_check' 행을 읽는다(그 잡들은 jobs.run이 아니다).
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from jobs.runtime import resolve_git_sha
from jobs.schedule import JOB_RE, SLOT_MATCH_TOLERANCE, expected_slots, match_slots, parse_crontab

WINDOW_HOURS = 24
# 본문 보존 잡(retention)은 하루 한 번 돈다. 만료된 지 이보다 오래된 본문이 남아 있으면 잡이 돌지 않은 것이다.
RETENTION_OVERDUE_HOURS = 26
# 호스트 밖 백업은 매일 18:10 UTC(scripts/oci/tier0-backup.cron)에 돌고 1~3분 걸린다. 이 리포트는 00:10 UTC
# (docker/crontab.micro)라 정상이면 리포트 시점의 경과는 약 6시간이고, 한 번 거르면 약 30시간이다. 기준은 하루 주기에
# 여유 2시간 - 한 번 거른 백업을 바로 다음 리포트가 경고한다(테스트가 두 crontab의 시각으로 확인한다).
BACKUP_OVERDUE_HOURS = 26
# 호스트 스크립트의 실행은 jobs.run의 잡이 아니어서 abandon_stale이 닿지 않는다. 이보다 오래 'running'인 'backup'·
# 'restore_check' 행은 끝나지 못한 실행(OOM·재부팅·kill)으로 보고 실패로 센다. 정상 실행은 백업 약 1분, 복원 검증 약 20초다.
HOST_RUN_STALE_HOURS = 1
# 복원 검증은 28일마다(tier0_backup.py daily). 한 번 거르면 경고한다.
RESTORE_CHECK_OVERDUE_DAYS = 35
# 유휴 지표는 백업과 같은 cron에서 매일 잰다. 이보다 오래된 측정은 "모름"으로 본다.
IDLE_CHECK_STALE_HOURS = 48
# scripts/oci/tier0_backup.py가 job_runs에 쓰는 잡 이름(테스트가 대조한다).
HOST_BACKUP_JOB = "backup"
IDLE_CHECK_JOB = "idle_check"
RESTORE_CHECK_JOB = "restore_check"
# 정시 회차 줄에 적는 건너뛴 회차 시각의 최대 개수
MISSED_SLOTS_SHOWN = 6


def format_report(report: Dict[str, Any]) -> str:
    lines: List[str] = [f"📊 뉴스레터 파이프라인 일일 리포트 (최근 {WINDOW_HOURS}시간)"]
    for job, by_status in sorted(report["job_runs"].items()):
        counts = ", ".join(f"{status} {n}" for status, n in sorted(by_status.items()))
        lines.append(f"- job {job}: {counts}")
    if not report["job_runs"]:
        lines.append("- 실행된 잡 없음 (스케줄러가 멈췄는지 확인)")
    news = report["news_raw_24h"]
    lines.append(
        f"- 신규 기사 {news['total']}건 (본문 {news['ok']}, 필터 {news['dropped']}, 중복 {news['duplicate']}, "
        f"본문없음 {news['empty']}, 다운로드 실패 {news['fetch_failed']}, 추출 오류 {news['error']}, "
        f"임베딩 {news['embedded']})"
    )
    for press, p in sorted(news["per_press"].items()):
        lines.append(f"  · {press}: {p['total']}건 (본문 {p['ok']}, 임베딩 {p['embedded']})")
    totals = report["totals"]
    lines.append(
        f"- 누적 기사 {totals['news_raw']}건 / 임베딩 {totals['embedded']}건 / "
        f"최근 {WINDOW_HOURS}시간 뉴스레터 {totals['newsletters_24h']}건"
    )
    retention = report.get("retention")
    if retention is not None:
        oldest = retention["oldest_body_crawled_at"]
        lines.append(
            f"- 본문 보존({retention['days']}일): 기한 지난 본문 {retention['expired_bodies']}건"
            f"(하루 넘게 밀린 것 {retention['overdue_bodies']}건), 최근 {WINDOW_HOURS}시간 비움 "
            f"{retention['purged_24h']}건 / 누적 {retention['purged_total']}건, 가장 오래된 본문 수집 "
            f"{oldest + ' UTC' if oldest else '없음'}"
        )
    schedule = report.get("schedule")
    if schedule is not None:
        lines.append(format_schedule(schedule))
    backup = report.get("backup")
    if backup is not None:
        lines.append(format_backup(backup))
    idle = report.get("idle")
    if idle is not None:
        lines.append(format_idle(idle, backup))
    recsys = report.get("recsys_24h")
    if recsys is not None:
        by_source = sorted(recsys["by_source"].items(), key=lambda kv: (-kv[1]["responses"], kv[0]))
        total = sum(v["responses"] for _, v in by_source)
        if total:
            detail = ", ".join(f"{source} {v['responses']}" for source, v in by_source)
            lines.append(f"- 추천 응답 {total}건 (노출 로그 기준: {detail})")
        else:
            lines.append("- 추천 응답 0건 (노출 로그 기준)")
    return "\n".join(lines)


def retention_warning(retention: Optional[Dict[str, Any]]) -> Optional[str]:
    """보존 잡이 밀렸으면 사람이 볼 경고 문장, 아니면 None."""
    if not retention or not retention.get("overdue_bodies"):
        return None
    return (
        f"본문 보존 기한({retention['days']}일)이 {RETENTION_OVERDUE_HOURS}시간 넘게 지난 본문이 "
        f"{retention['overdue_bodies']}건 남아 있다 - retention 잡이 돌지 않았거나 실패했다 "
        "(job_runs의 retention 행, docs/runbook-hosting.md 4.1)"
    )


def retention_status(cur, window: str) -> Dict[str, Any]:
    """본문 보존(ADR 0023) 상태. 기한·면제 규칙은 jobs/tasks/retention.py와 같은 식이다(읽기만 한다).
    기한은 설정이 아니라 ADR의 상수(30일)로 잰다 - 이 줄은 ADR을 지키는지 보는 것이다."""
    from config.retention import BODY_RETENTION_DAYS, exempt_press_names
    from jobs.tasks.retention import EXEMPT_SQL

    days = BODY_RETENTION_DAYS
    cutoff = "(now() AT TIME ZONE 'UTC') - make_interval(days => %(days)s)"
    overdue = f"{cutoff} - make_interval(hours => %(overdue_hours)s)"
    body = f"n.raw_news_content <> '' AND NOT {EXEMPT_SQL}"
    cur.execute(
        f"""
        SELECT count(*) FILTER (WHERE {body} AND n.raw_news_crawled_at <= {cutoff}),
               count(*) FILTER (WHERE {body} AND n.raw_news_crawled_at <= {overdue}),
               count(*) FILTER (WHERE n.raw_news_content_purged_at >= now() - %(window)s::interval),
               count(*) FILTER (WHERE n.raw_news_content_purged_at IS NOT NULL),
               min(n.raw_news_crawled_at) FILTER (WHERE {body})
        FROM news_raw n JOIN press p ON p.press_id = n.press_id
        """,
        {"days": days, "overdue_hours": RETENTION_OVERDUE_HOURS, "window": window,
         "exempt_presses": exempt_press_names()},
    )
    expired_bodies, overdue, purged_24h, purged_total, oldest = cur.fetchone()
    return {
        "days": days, "expired_bodies": expired_bodies, "overdue_bodies": overdue,
        "purged_24h": purged_24h, "purged_total": purged_total,
        "oldest_body_crawled_at": oldest.isoformat(sep=" ", timespec="minutes") if oldest else None,
    }


def impression_sources(cur, window: str) -> Dict[str, Dict[str, int]]:
    """노출 로그에서 출처(X-Rec-Source)별 응답 수와 노출 항목 수를 센다.

    GET /recsys/stats의 카운터는 프로세스 안의 값이라 재시작하면 사라지고 워커마다 따로다.
    기간별 출처 분포(실시간 계산이 얼마나 폴백으로 떨어졌는지)는 이 로그에서 본다. 다만 항목을
    하나 이상 보여 주고 로그 쓰기까지 성공한 응답만 여기 잡힌다 - 빈 응답과 유실된 쓰기는 없다.
    """
    cur.execute(
        """
        SELECT source, COUNT(DISTINCT request_id), COUNT(*)
        FROM recommendation_impression_log
        WHERE created_at >= now() - %s::interval
        GROUP BY source
        """,
        (window,),
    )
    return {source: {"responses": responses, "items": items} for source, responses, items in cur.fetchall()}

def _mb(n: Optional[int]) -> str:
    return "?" if n is None else f"{n / 1_000_000:.1f} MB"


def _utc_short(iso: Optional[str]) -> str:
    if not iso:
        return "?"
    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")


def format_schedule(schedule: Dict[str, Any]) -> str:
    if schedule.get("crontab") is None:
        return "- 정시 회차: 세지 않음(SCHEDULER_CRONTAB 없음 - 스케줄러 밖에서 돌린 리포트)"
    parts = []
    for job, s in sorted(schedule["jobs"].items(), key=lambda kv: (-len(kv[1]["missed"]), kv[0])):
        if not s["scheduled"]:
            continue  # 창 안에 예정 회차가 없는 잡(하루 한 번 도는 잡의 배포 직후 등)
        text = f"{job} {s['started']}/{s['scheduled']}"
        missed = s["missed"]
        if missed:
            shown = "·".join(datetime.fromisoformat(t).astimezone(timezone.utc).strftime("%H:%M")
                             for t in missed[:MISSED_SLOTS_SHOWN])
            more = f" 외 {len(missed) - MISSED_SLOTS_SHOWN}" if len(missed) > MISSED_SLOTS_SHOWN else ""
            text += f"(건너뜀 {len(missed)}: {shown}{more} UTC, 성공 {s['succeeded']})"
        parts.append(text)
    since = (f", 이 코드가 처음 돈 {_utc_short(schedule['counted_since'])}부터"
             if schedule.get("counted_since") else "")
    line = (f"- 정시 회차(최근 {WINDOW_HOURS}시간{since}, {schedule['crontab']} 기준 시작/예정): "
            + (", ".join(parts) if parts else "jobs.run 줄 없음"))
    if schedule.get("disabled"):
        line += f" / 꺼 둔 잡: {', '.join(schedule['disabled'])}"
    if schedule.get("unsupported"):
        line += f" / 세지 못한 줄 {len(schedule['unsupported'])}개"
    return line


def _failures(n: int, stuck: int) -> str:
    return f"{n}" + (f"(그중 끝나지 않은 실행 {stuck})" if stuck else "")


def format_backup(backup: Dict[str, Any]) -> str:
    failed = _failures(backup.get("failed_24h", 0), backup.get("stuck_24h", 0))
    if not backup.get("last_success_at"):
        tail = f", 최근 {WINDOW_HOURS}시간 실패 {failed}" if backup.get("failed_24h") else ""
        return f"- 호스트 밖 백업: 기록 없음(job_runs에 '{HOST_BACKUP_JOB}' 성공이 없다 - 런북 6.5){tail}"
    if backup.get("full_disabled"):
        full = "본문 사본 꺼짐(full_enabled 아님)"
    else:
        full = f"최근 본문 사본 {_mb(backup.get('full_bytes'))}/{backup.get('full_body_rows', '?')}건"
    line = (
        f"- 호스트 밖 백업(OCI Object Storage, 런북 6.5): 마지막 성공 {backup['hours_since']:.1f}시간 전"
        f"({_utc_short(backup['last_success_at'])}, 본문 없는 사본 {_mb(backup.get('nobody_bytes'))} · {full}), "
        f"그 뒤 수집 {backup['rows_since']}건, 최근 {WINDOW_HOURS}시간 실패 {failed}"
    )
    if backup.get("restore_check_at"):
        line += f" / 복원 검증 마지막 {_utc_short(backup['restore_check_at'])}({backup['restore_check_days']:.0f}일 전)"
    else:
        line += " / 복원 검증 기록 없음"
    if backup.get("restore_failed_24h"):
        line += f", 최근 {WINDOW_HOURS}시간 복원 검증 실패 " + _failures(backup["restore_failed_24h"],
                                                                     backup.get("restore_stuck_24h", 0))
    for w in backup.get("warnings") or []:
        line += f" / {w}"
    return line


def reclaim_risk(idle: Optional[Dict[str, Any]], backup: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """(수준, 이유). 공식 유휴 기준(ADR 0026 결정 6)에 걸리는지와, 회수되면 무엇을 잃는지(백업 경과)를 합친다."""
    if not idle or not idle.get("measured_at") or idle["hours_since"] > IDLE_CHECK_STALE_HOURS:
        return "모름", f"{IDLE_CHECK_STALE_HOURS}시간 안의 유휴 지표 측정이 없다"
    verdict = idle.get("verdict")
    backup_ok = bool(backup and backup.get("last_success_at") and backup["hours_since"] <= BACKUP_OVERDUE_HOURS)
    if verdict == "safe":
        return "낮음", "공식 유휴 기준 밖"
    criterion = "기준상 유휴" if verdict == "idle" else "기준+5%p 안쪽"
    if not backup_ok:
        return "높음", f"{criterion}이고 {BACKUP_OVERDUE_HOURS}시간 안의 호스트 밖 백업이 없다"
    return "주의", f"{criterion} - 손실은 마지막 백업 뒤 수집분으로 묶여 있다"


def format_idle(idle: Optional[Dict[str, Any]], backup: Optional[Dict[str, Any]]) -> str:
    level, reason = reclaim_risk(idle, backup)
    line = f"- 유휴 회수 위험(ADR 0026 결정 6): {level} - {reason}"
    if idle and idle.get("measured_at"):
        line += (f" (측정 {_utc_short(idle['measured_at'])}, {idle.get('window_days', '?')}일 창 CPU p95 "
                 f"{idle.get('cpu_p95')}% · 네트워크 p95 {idle.get('net_p95')}%, 기준 20%)")
    if backup and backup.get("last_success_at") and level in ("주의", "높음"):
        line += f". 지금 회수되면 마지막 백업 뒤 수집 {backup['rows_since']}건을 잃는다"
    return line


def ops_warnings(backup: Optional[Dict[str, Any]]) -> List[str]:
    """사람이 볼 경고(알림). 백업이 한 번도 없었던 호스트(개발 Mac)에서는 경고하지 않는다 - 줄에만 '기록 없음'."""
    if not backup or not backup.get("last_success_at"):
        return []
    out = []
    if backup["hours_since"] > BACKUP_OVERDUE_HOURS:
        out.append(f"호스트 밖 백업이 {backup['hours_since']:.0f}시간째 없다(기준 {BACKUP_OVERDUE_HOURS}시간) - "
                   "VM의 /var/log/newsletter-backup.log와 job_runs의 'backup' 행을 본다(런북 6.5)")
    if backup.get("failed_24h"):
        out.append(f"최근 {WINDOW_HOURS}시간 호스트 밖 백업 실패 {backup['failed_24h']}회"
                   f"{_stuck_note(backup.get('stuck_24h'))}(job_runs의 '{HOST_BACKUP_JOB}' 행의 status·error)")
    if backup.get("restore_failed_24h"):
        out.append(f"최근 {WINDOW_HOURS}시간 복원 검증 실패 {backup['restore_failed_24h']}회"
                   f"{_stuck_note(backup.get('restore_stuck_24h'))}(job_runs의 '{RESTORE_CHECK_JOB}' 행의 status·error)")
    days = backup.get("restore_check_days")
    if days is None or days > RESTORE_CHECK_OVERDUE_DAYS:
        out.append(f"복원 검증이 {'한 번도 없다' if days is None else f'{days:.0f}일째 없다'}"
                   f"(기준 {RESTORE_CHECK_OVERDUE_DAYS}일) - 런북 6.5의 복원 검증")
    out.extend(backup.get("warnings") or [])
    return out


def _stuck_note(stuck: Optional[int]) -> str:
    return (f" - 그중 {stuck}회는 {HOST_RUN_STALE_HOURS}시간 넘게 'running'으로 남은 행(OOM·재부팅·kill로 끝나지 못한 실행)"
            if stuck else "")


def read_crontab() -> Optional[Tuple[str, str]]:
    """스케줄러가 쓰는 crontab(docker/scheduler-entrypoint.sh가 SCHEDULER_CRONTAB으로 넘긴다)의 (이름, 내용)."""
    path = os.getenv("SCHEDULER_CRONTAB")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return os.path.basename(path), f.read()
    except OSError:
        return None


def disabled_jobs() -> List[str]:
    return sorted({name.strip() for name in os.getenv("JOBS_DISABLED", "").split(",") if name.strip()})


def crontab_jobs(crontab: Optional[Tuple[str, str]]) -> List[str]:
    """crontab의 jobs.run 잡 이름(세지 못하는 줄 포함)."""
    if crontab is None:
        return []
    entries, unsupported = parse_crontab(crontab[1])
    jobs = {e.job for e in entries}
    for line in unsupported:
        match = JOB_RE.search(line)
        if match:
            jobs.add(match.group(1))
    return sorted(jobs)


def image_first_run(cur, git_sha: Optional[str], jobs: Sequence[str]) -> Optional[datetime]:
    """이 코드(이미지)로 스케줄러가 돌린 첫 잡의 시작 시각. crontab은 이미지에 들어 있어 배포 때만 바뀐다 - 배포 전 회차를
    지금 crontab으로 세지 않으려고 쓴다(예: 배포로 새로 생긴 줄의 회차를 배포 전 시각에 '건너뜀'으로 세지 않는다).
    crontab의 jobs.run 잡만 본다: 같은 커밋으로 깐 호스트 스크립트('backup' 등)나 `compose run` 일회성 잡이 스케줄러보다
    먼저 돌았을 수 있다."""
    if not git_sha or not jobs:
        return None
    cur.execute("SELECT min(started_at) FROM job_runs WHERE git_sha = %s AND job = ANY(%s)", (git_sha, list(jobs)))
    row = cur.fetchone()
    return row[0] if row else None


def schedule_status(cur, now: datetime, crontab: Optional[Tuple[str, str]],
                    disabled: Sequence[str] = (), since: Optional[datetime] = None) -> Dict[str, Any]:
    """정시 회차(예정 vs 시작). now는 DB의 now(). since(이 이미지의 첫 실행)가 창 안이면 그 뒤만 센다.

    crontab의 시각은 컨테이너 TZ(UTC)다. DB의 now()는 세션 TimeZone의 aware 시각이라 UTC로 바꿔 센다 - 그대로 쓰면
    세션이 KST일 때 하루 한 번 도는 잡의 회차가 9시간 어긋난다."""
    if crontab is None:
        return {"crontab": None}
    now = now.astimezone(timezone.utc)
    if since is not None:
        since = since.astimezone(timezone.utc)
    name, text = crontab
    entries, unsupported = parse_crontab(text)
    start = now - timedelta(hours=WINDOW_HOURS)
    counted_since = None
    if since is not None and since - SLOT_MATCH_TOLERANCE > start:
        start = since - SLOT_MATCH_TOLERANCE
        counted_since = since.isoformat()
    slots = expected_slots(entries, start, now, disabled)
    runs: Dict[str, List[Tuple[datetime, str]]] = {job: [] for job in slots}
    if slots:
        cur.execute(
            "SELECT job, started_at, status FROM job_runs WHERE job = ANY(%s) AND started_at > %s AND started_at <= %s",
            (sorted(slots), start, now + SLOT_MATCH_TOLERANCE),
        )
        for job, started_at, status in cur.fetchall():
            runs[job].append((started_at, status))
    jobs = {}
    for job, job_slots in slots.items():
        m = match_slots(job_slots, runs[job])
        m["missed"] = [t.isoformat() for t in m["missed"]]
        jobs[job] = m
    return {"crontab": name, "jobs": jobs, "unsupported": unsupported, "counted_since": counted_since,
            "disabled": sorted(set(disabled) & {e.job for e in entries})}


def _latest_success(cur, job: str) -> Tuple[Optional[datetime], Dict[str, Any]]:
    cur.execute(
        "SELECT finished_at, stats FROM job_runs WHERE job = %s AND status = 'succeeded' "
        "ORDER BY finished_at DESC NULLS LAST LIMIT 1",
        (job,),
    )
    row = cur.fetchone()
    return (row[0], row[1] or {}) if row else (None, {})


def _host_failures(cur, job: str, window: str) -> Tuple[int, int]:
    """창 안에 시작한 호스트 스크립트 실행 중 (실패 수, 그중 끝나지 못한 실행 수). 실패 = failed·abandoned 또는
    HOST_RUN_STALE_HOURS 넘게 'running'인 행 - cron이 시작한 뒤 프로세스가 죽으면 행이 'running'으로 남는다."""
    cur.execute(
        "SELECT count(*) FILTER (WHERE status IN ('failed', 'abandoned')), "
        "count(*) FILTER (WHERE status = 'running' AND started_at < now() - make_interval(hours => %s)) "
        "FROM job_runs WHERE job = %s AND started_at >= now() - %s::interval",
        (HOST_RUN_STALE_HOURS, job, window),
    )
    failed, stuck = cur.fetchone()
    return failed + stuck, stuck


def backup_status(cur, now: datetime, window: str) -> Dict[str, Any]:
    """tier0_backup.py가 남긴 'backup'·'restore_check' 행에서 마지막 성공, 실패 수, 그 뒤 수집량."""
    finished, stats = _latest_success(cur, HOST_BACKUP_JOB)
    failed, stuck = _host_failures(cur, HOST_BACKUP_JOB, window)
    restore_failed, restore_stuck = _host_failures(cur, RESTORE_CHECK_JOB, window)
    out: Dict[str, Any] = {"last_success_at": None, "failed_24h": failed, "stuck_24h": stuck,
                           "restore_failed_24h": restore_failed, "restore_stuck_24h": restore_stuck}
    if finished is None:
        return out
    snapshot = stats.get("snapshot_at_utc")
    snapshot_at = datetime.fromisoformat(snapshot) if snapshot else finished
    # raw_news_crawled_at은 시간대 없는 UTC 값이다
    cur.execute("SELECT count(*) FROM news_raw WHERE raw_news_crawled_at > %s",
                (snapshot_at.astimezone(timezone.utc).replace(tzinfo=None),))
    (rows_since,) = cur.fetchone()
    restore_at, _ = _latest_success(cur, RESTORE_CHECK_JOB)
    out.update({
        "last_success_at": finished.isoformat(),
        "hours_since": (now - finished).total_seconds() / 3600,
        "snapshot_at_utc": snapshot_at.isoformat(),
        "rows_since": rows_since,
        "nobody_bytes": (stats.get("nobody") or {}).get("bytes"),
        "full_disabled": bool((stats.get("full") or {}).get("disabled")),
        "full_bytes": (stats.get("full") or {}).get("bytes"),
        "full_body_rows": (stats.get("full") or {}).get("body_rows"),
        "warnings": list(stats.get("warnings") or []),
        "restore_check_at": restore_at.isoformat() if restore_at else None,
        "restore_check_days": (now - restore_at).total_seconds() / 86400 if restore_at else None,
    })
    return out


def idle_status(cur, now: datetime) -> Dict[str, Any]:
    """tier0_backup.py idle-check가 남긴 마지막 측정(scripts/oci/idle_check.py와 같은 계산)."""
    finished, stats = _latest_success(cur, IDLE_CHECK_JOB)
    if finished is None:
        return {"measured_at": None}
    verdict = stats.get("verdict") or {}
    return {
        "measured_at": finished.isoformat(),
        "hours_since": (now - finished).total_seconds() / 3600,
        "verdict": "idle" if verdict.get("idle") else ("at_risk" if verdict.get("at_risk") else "safe"),
        "cpu_p95": stats.get("cpu_p95"),
        "net_p95": stats.get("net_p95"),
        "window_days": stats.get("requested_days"),
    }


def run(ctx) -> Dict[str, Any]:
    from db.connection import get_db_connection
    from jobs.notify import post_message

    window = f"{WINDOW_HOURS} hours"
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT job, status, COUNT(*) FROM job_runs
            WHERE started_at >= now() - %s::interval
            GROUP BY job, status
            """,
            (window,),
        )
        job_runs: Dict[str, Dict[str, int]] = {}
        for job, status, n in cur.fetchall():
            job_runs.setdefault(job, {})[status] = n

        cur.execute(
            """
            SELECT P.press_name,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'ok'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'dropped'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'empty'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'fetch_failed'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'error'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'duplicate'),
                   COUNT(*) FILTER (WHERE N.embedding_result IS NOT NULL)
            FROM news_raw N JOIN press P ON P.press_id = N.press_id
            WHERE N.raw_news_crawled_at >= now() - %s::interval
            GROUP BY P.press_name
            """,
            (window,),
        )
        keys = ("total", "ok", "dropped", "empty", "fetch_failed", "error", "duplicate", "embedded")
        per_press = {row[0]: dict(zip(keys, row[1:])) for row in cur.fetchall()}
        news_24h = {k: sum(p[k] for p in per_press.values()) for k in keys}
        news_24h["per_press"] = per_press

        cur.execute(
            """
            SELECT (SELECT COUNT(*) FROM news_raw),
                   (SELECT COUNT(*) FROM news_raw WHERE embedding_result IS NOT NULL),
                   (SELECT COUNT(*) FROM news_letter WHERE news_letter_created_at >= now() - %s::interval)
            """,
            (window,),
        )
        total, embedded, newsletters = cur.fetchone()
        retention = retention_status(cur, window)
        recsys_by_source = impression_sources(cur, window)
        cur.execute("SELECT now()")
        (now,) = cur.fetchone()
        crontab = read_crontab()
        schedule = schedule_status(cur, now, crontab, disabled_jobs(),
                                   since=image_first_run(cur, resolve_git_sha(), crontab_jobs(crontab)))
        backup = backup_status(cur, now, window)
        idle = idle_status(cur, now)

    report = {
        "job_runs": job_runs,
        "news_raw_24h": news_24h,
        "totals": {"news_raw": total, "embedded": embedded, "newsletters_24h": newsletters},
        "retention": retention,
        "recsys_24h": {"by_source": recsys_by_source},
        "schedule": schedule,
        "backup": backup,
        "idle": idle,
    }
    warning = retention_warning(retention)
    if warning:
        ctx.warn(warning)
    for warning in ops_warnings(backup):
        ctx.warn(warning)
    text = format_report(report)
    print(text)
    report["slack_posted"] = post_message(text)
    return {"report": report}
