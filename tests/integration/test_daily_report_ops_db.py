"""daily_report의 운영 줄(정시 회차, 호스트 밖 백업, 유휴 회수 위험)이 쓰는 SQL을 실제 PostgreSQL에서 돌린다.

다른 테스트의 job_runs 행과 섞이지 않게 정시 회차는 이 테스트만의 잡 이름으로 세고, 백업 기준 시각은 미래로 둔다
(그 뒤에 수집된 행은 이 테스트가 넣은 것뿐이다). 넣은 행은 끝나면 지운다.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from jobs.tasks import daily_report as dr

UTC = timezone.utc


@pytest.fixture
def cleanup(pg_conn):
    ids = {"job_runs": [], "news_raw": [], "press": []}
    yield ids
    with pg_conn.cursor() as cur:
        for table, key in (("job_runs", "id"), ("news_raw", "raw_news_id"), ("press", "press_id")):
            if ids[table]:
                cur.execute(f"DELETE FROM {table} WHERE {key} = ANY(%s)", (ids[table],))


def _job_run(cur, ids, job, status, started_at, finished_at=None, stats="{}"):
    cur.execute("INSERT INTO job_runs (job, status, started_at, finished_at, stats) VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id", (job, status, started_at, finished_at, stats))
    ids["job_runs"].append(cur.fetchone()[0])


def test_schedule_status_counts_started_runs_per_slot_from_job_runs(pg_conn, cleanup):
    job = f"ingest_it_{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Seoul'")  # 세션 시간대와 무관해야 한다
        cur.execute("SELECT now()")
        (now,) = cur.fetchone()
        slots = sorted({(now - timedelta(hours=h)).replace(minute=5, second=0, microsecond=0) for h in range(0, 26)})
        slots = [s for s in slots if now - timedelta(hours=24) < s <= now]
        for i, slot in enumerate(slots):
            if i % 4 == 3:
                continue  # 건너뛴 회차(행 없음)
            _job_run(cur, cleanup, job, "succeeded" if i % 4 else "failed", slot + timedelta(seconds=1), slot)
        crontab = ("crontab.test", f"5 * * * * python -m jobs.run {job} --stages rss,extract\n")
        status = dr.schedule_status(cur, now, crontab)
    s = status["jobs"][job]
    skipped = len([i for i in range(len(slots)) if i % 4 == 3])
    assert s["scheduled"] == len(slots) and s["started"] == len(slots) - skipped
    assert s["succeeded"] == len([i for i in range(len(slots)) if i % 4 in (1, 2)])
    assert len(s["missed"]) == skipped
    assert "건너뜀" in dr.format_schedule(status)


def test_backup_and_idle_status_read_the_host_script_rows(pg_conn, cleanup):
    with pg_conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'Asia/Seoul'")
        cur.execute("SELECT now()")
        (now,) = cur.fetchone()
        snapshot = (now + timedelta(days=10)).astimezone(UTC)  # 그 뒤 수집은 아래 한 건뿐
        stats = ('{"snapshot_at_utc": "%s", "nobody": {"bytes": 3768320}, '
                 '"full": {"bytes": 42311680, "body_rows": 27427}, "warnings": []}' % snapshot.isoformat())
        _job_run(cur, cleanup, dr.HOST_BACKUP_JOB, "succeeded", now - timedelta(hours=6, minutes=1),
                 now - timedelta(hours=6), stats)
        _job_run(cur, cleanup, dr.HOST_BACKUP_JOB, "failed", now - timedelta(hours=2), now - timedelta(hours=2))
        _job_run(cur, cleanup, dr.RESTORE_CHECK_JOB, "succeeded", now - timedelta(days=3), now - timedelta(days=3))
        _job_run(cur, cleanup, dr.IDLE_CHECK_JOB, "succeeded", now - timedelta(hours=6), now - timedelta(hours=6),
                 '{"cpu_p95": 7.89, "net_p95": 0.076, "requested_days": 7.0, '
                 '"verdict": {"idle": true, "at_risk": true}}')
        cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"it-{uuid.uuid4().hex[:6]}",))
        press = cur.fetchone()[0]
        cleanup["press"].append(press)
        for key, delta in (("after", timedelta(minutes=1)), ("before", -timedelta(minutes=1))):
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_url, raw_news_content, raw_news_crawled_at) "
                "VALUES (%s, 't', %s, '', %s) RETURNING raw_news_id",
                (press, f"https://example.com/ops/{uuid.uuid4().hex}/{key}", (snapshot + delta).replace(tzinfo=None)))
            cleanup["news_raw"].append(cur.fetchone()[0])
        backup = dr.backup_status(cur, now, "24 hours")
        idle = dr.idle_status(cur, now)

    assert backup["hours_since"] == pytest.approx(6.0, abs=0.01)
    assert backup["rows_since"] == 1 and backup["failed_24h"] == 1
    assert backup["nobody_bytes"] == 3768320 and backup["full_body_rows"] == 27427
    assert backup["restore_check_days"] == pytest.approx(3.0, abs=0.01)
    assert idle["verdict"] == "idle" and idle["cpu_p95"] == 7.89 and idle["window_days"] == 7.0
    assert dr.reclaim_risk(idle, backup)[0] == "주의"
    assert any("실패 1회" in w for w in dr.ops_warnings(backup))
