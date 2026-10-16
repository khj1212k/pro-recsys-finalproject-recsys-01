"""Tier 0 규모(30,000행)의 합성 news_raw에서 마이그레이션 f1db3221e45f의 길이 채우기와 retention 잡을 잰다.

ADR 0023 개정(2026-10-17)의 "CI에서 잰 값"이 이 테스트의 리포트다. RETENTION_REPORT(경로)가 있을 때만 돈다 -
CI의 "Write retention measurement report" 단계가 JSON을 아티팩트(retention-measurement)로 남긴다.

재는 것: 걸린 시간, 만든 WAL 바이트, 테이블(힙·TOAST) 크기, 그리고 그동안 다른 연결에서 20 ms마다 넣는
INSERT(수집기 역할)와 추출 대기 행 UPDATE(본문 추출기 역할)의 지연. CI 러너는 Tier 0(1/8 OCPU, 1 GB)가 아니다 -
시간은 그대로 옮길 수 없고, 행 수·WAL·크기·락 동작이 옮겨지는 값이다.

본문은 지어낸 것이다: 한글 음절을 무작위로 이은 "낱말" 2,000개에서 고른 낱말열(행마다 다른 시드). Tier 0 실측의
평균 1,768자(2026-10-16)에 맞췄다. 압축률은 실제 기사와 다르다 - 리포트에 실제 값과 함께 적는다.
"""
import hashlib
import io
import json
import os
import random
import statistics
import threading
import time
import uuid
from datetime import timedelta

import psycopg2
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("RETENTION_REPORT"), reason="RETENTION_REPORT가 없으면 측정하지 않는다(CI의 측정 단계 전용)"
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BEFORE = "a6f1e83b0d57"
N_ROWS = 30_000
DAYS_SPAN = 40          # 하루 750행 - 30일 기한이면 약 10일치(7,500행)가 만료
OK_SHARE = 0.88         # Tier 0 2026-10-16: 본문 있는 행 27,070 / 30,712
TIER0 = {"rows": 30712, "with_body": 27070, "heap_mb": 28, "toast_mb": 49, "body_bytes": 114130386,
         "stored_bytes": 64503578, "avg_chars": 1768.4, "measured_at_utc": "2026-10-16T16:04"}


def _alembic_config():
    from alembic.config import Config

    cfg = Config(os.path.join(REPO_ROOT, "backend", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "backend", "alembic"))
    return cfg


def _vocabulary(rng, size=2000):
    return ["".join(chr(0xAC00 + rng.randrange(11172)) for _ in range(rng.randint(1, 4))) for _ in range(size)]


def _copy_rows(press_id, now_utc, tag):
    """COPY 텍스트 형식(탭 구분, NULL = \\N). 본문에는 탭·줄바꿈·역슬래시가 없다. 빈 본문('')은 빈 칸이다."""
    rng = random.Random(20261017)
    words = _vocabulary(rng)
    buf = io.StringIO()
    for i in range(N_ROWS):
        crawled = now_utc - timedelta(days=i % DAYS_SPAN, seconds=(i * 37) % 86400)
        if rng.random() < OK_SHARE:
            body = f"{i}번 합성 기사. " + " ".join(rng.choice(words) for _ in range(rng.randint(250, 760)))
            status, sha = "ok", hashlib.sha256(body.encode("utf-8")).hexdigest()
        else:
            body, status, sha = "", rng.choice(["dropped", "fetch_failed"]), None
        fields = [str(press_id), f"합성 제목 {i}", body, f"https://itest.example/{tag}/{i}",
                  crawled.isoformat() + "+00", crawled.isoformat(), status, crawled.isoformat() + "+00",
                  "3" if status == "fetch_failed" else "1", sha or "\\N"]
        buf.write("\t".join(fields) + "\n")
    buf.seek(0)
    return buf


def _scalar(cur, sql, params=None):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def _sizes(cur):
    cur.execute(
        """
        SELECT pg_relation_size('news_raw'), pg_relation_size(reltoastrelid), pg_total_relation_size('news_raw')
        FROM pg_class WHERE oid = 'news_raw'::regclass
        """
    )
    heap, toast, total = cur.fetchone()
    return {"heap_bytes": heap, "toast_bytes": toast, "total_bytes": total}


class _Writer(threading.Thread):
    """수집기·본문 추출기 역할: 20 ms마다 새 행 INSERT와 추출 대기 행 UPDATE를 번갈아 하고 지연을 잰다."""

    def __init__(self, url, press_id, tag, pending_ids):
        super().__init__(daemon=True)
        self.url, self.press_id, self.tag, self.pending_ids = url, press_id, tag, pending_ids
        self.latencies_ms = {"insert": [], "update_pending": []}
        self.errors = []
        self.stop = threading.Event()

    def run(self):
        conn = psycopg2.connect(self.url)
        conn.autocommit = True
        n = 0
        try:
            with conn.cursor() as cur:
                cur.execute("SET lock_timeout = '30s'")
                while not self.stop.is_set():
                    n += 1
                    started = time.monotonic()
                    try:
                        if n % 2:
                            cur.execute(
                                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, "
                                "raw_news_created_at, raw_news_crawled_at) VALUES (%s, '동시 수집', '', %s, now(), "
                                "now() AT TIME ZONE 'UTC')",
                                (self.press_id, f"https://itest.example/{self.tag}/live/{n}"),
                            )
                            kind = "insert"
                        else:
                            cur.execute(
                                "UPDATE news_raw SET raw_news_extract_attempts = raw_news_extract_attempts "
                                "WHERE raw_news_id = %s",
                                (self.pending_ids[n % len(self.pending_ids)],),
                            )
                            kind = "update_pending"
                        self.latencies_ms[kind].append((time.monotonic() - started) * 1000)
                    except psycopg2.Error as e:  # 컬럼이 없어지는 순간 등 - 세고 계속한다
                        self.errors.append(type(e).__name__)
                    time.sleep(0.02)
        finally:
            conn.close()

    def summary(self):
        out = {"errors": len(self.errors)}
        for kind, values in self.latencies_ms.items():
            values = sorted(values)
            out[kind] = {
                "n": len(values),
                "p50_ms": round(statistics.median(values), 2) if values else None,
                "p99_ms": round(values[int(len(values) * 0.99) - 1], 2) if len(values) >= 100 else None,
                "max_ms": round(values[-1], 2) if values else None,
            }
        return out


def _timed_with_writer(database_url, press_id, tag, pending_ids, cur, fn):
    writer = _Writer(database_url, press_id, tag, pending_ids)
    lsn = _scalar(cur, "SELECT pg_current_wal_lsn()")
    writer.start()
    time.sleep(0.2)
    started = time.monotonic()
    try:
        result = fn()
    finally:
        took = time.monotonic() - started
        time.sleep(0.2)
        writer.stop.set()
        writer.join(timeout=10)
    wal = _scalar(cur, "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), %s)", (lsn,))
    return result, {"seconds": round(took, 3), "wal_bytes": int(wal), "concurrent_writes": writer.summary()}


def test_measure_backfill_and_retention_at_tier0_scale(database_url, pg_conn, isolated_news_raw, monkeypatch):
    from alembic import command
    from config.settings import Settings
    from jobs.run import main

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(Settings, "BODY_RETENTION_DAYS", 30)
    cfg = _alembic_config()
    tag = uuid.uuid4().hex[:8]
    report = {"rows": N_ROWS, "days_span": DAYS_SPAN, "tier0_reference": TIER0}
    with pg_conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM job_runs")
        (last_run,) = cur.fetchone()
        cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (f"itest-volume-{tag}",))
        press_id = cur.fetchone()[0]
        report["server"] = {
            "version": _scalar(cur, "SHOW server_version"),
            "shared_buffers": _scalar(cur, "SHOW shared_buffers"),
            "maintenance_work_mem": _scalar(cur, "SHOW maintenance_work_mem"),
            "runner_cpus": os.cpu_count(),
        }
    try:
        with pg_conn.cursor() as cur:
            now_utc = _scalar(cur, "SELECT now() AT TIME ZONE 'UTC'")
            started = time.monotonic()
            cur.copy_expert(
                "COPY news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, "
                "raw_news_crawled_at, raw_news_extract_status, raw_news_extracted_at, raw_news_extract_attempts, "
                "raw_news_content_sha256) FROM STDIN",
                _copy_rows(press_id, now_utc, tag),
            )
            report["seed_seconds"] = round(time.monotonic() - started, 3)
            cur.execute("ANALYZE news_raw")
            cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE raw_news_content <> ''), "
                "sum(octet_length(raw_news_content)), sum(pg_column_size(raw_news_content)), "
                "avg(char_length(raw_news_content)) FILTER (WHERE raw_news_content <> '') FROM news_raw"
            )
            rows, with_body, body_bytes, stored_bytes, avg_chars = cur.fetchone()
            report["seeded"] = {"rows": rows, "with_body": with_body, "body_bytes": int(body_bytes),
                                "stored_bytes": int(stored_bytes), "avg_chars": round(float(avg_chars), 1),
                                **_sizes(cur)}
            # 동시 쓰기 역할이 건드릴 "추출 대기" 행(보존 잡이 건드리지 않는 행)을 몇 개 만든다
            cur.execute(
                "UPDATE news_raw SET raw_news_extract_status = NULL, raw_news_extract_attempts = 0 "
                "WHERE raw_news_id IN (SELECT raw_news_id FROM news_raw WHERE raw_news_content = '' "
                "ORDER BY raw_news_id LIMIT 50) RETURNING raw_news_id"
            )
            pending_ids = [r[0] for r in cur.fetchall()]

        # 1) 마이그레이션: 이 리비전 전으로 내린 뒤(길이 열이 사라진다) 다시 올리며 길이를 채운다
        started = time.monotonic()
        command.downgrade(cfg, BEFORE)
        report["downgrade_seconds"] = round(time.monotonic() - started, 3)
        with pg_conn.cursor() as cur:
            _, report["upgrade"] = _timed_with_writer(
                database_url, press_id, tag, pending_ids, cur, lambda: command.upgrade(cfg, "head"))
            report["upgrade"]["filled"] = _scalar(
                cur, "SELECT count(*) FROM news_raw WHERE raw_news_content_length IS NOT NULL")
            report["upgrade"]["sizes_after"] = _sizes(cur)
            # 시드한 행은 모두 채워졌다. 마이그레이션 중에 동시 쓰기 역할이 (예전 코드처럼 길이 없이) 넣은 행 가운데
            # 길이 채우기가 지나간 뒤에 들어온 행은 비어 있다 - 보존 잡이 비울 때 길이를 쓴다.
            assert _scalar(cur, "SELECT count(*) FROM news_raw WHERE raw_news_title <> '동시 수집' "
                                "AND raw_news_content IS NOT NULL "
                                "AND raw_news_content_length IS DISTINCT FROM char_length(raw_news_content)") == 0
            report["upgrade"]["concurrent_rows_left_without_length"] = _scalar(
                cur, "SELECT count(*) FROM news_raw WHERE raw_news_title = '동시 수집' AND raw_news_content_length IS NULL")
            cur.execute("VACUUM news_raw")
            report["upgrade"]["sizes_after_vacuum"] = _sizes(cur)
            expected = _scalar(
                cur, "SELECT count(*) FROM news_raw WHERE raw_news_crawled_at <= "
                     "(now() AT TIME ZONE 'UTC') - interval '30 days' AND raw_news_extract_status IS NOT NULL")

        # 2) 첫 실행(약 10일치가 한꺼번에 만료된 상태 - 2026-10-26 첫 실행보다 크다)
        with pg_conn.cursor() as cur:
            code, report["retention_first"] = _timed_with_writer(
                database_url, press_id, tag, pending_ids, cur, lambda: main(["retention"]))
            assert code == 0
            cur.execute("SELECT status, stats FROM job_runs WHERE job = 'retention' ORDER BY id DESC LIMIT 1")
            status, stats = cur.fetchone()
            assert status == "succeeded" and stats["remaining_expired_bodies"] == 0
            # 세고 나서 잡이 시작하기까지 몇 초 사이에 경계를 넘는 행이 있을 수 있다(늘기만 한다)
            assert stats["purged"] >= expected
            assert stats["purged"] == _scalar(cur, "SELECT count(*) FROM news_raw WHERE raw_news_content_purged_at IS NOT NULL")
            report["retention_first"]["job_stats"] = {k: stats[k] for k in (
                "purged", "purged_bodies", "purged_bytes", "batches", "batch_size", "purge_s", "scanned",
                "skipped_awaiting_extraction", "oldest_remaining_body_age_days")}
            report["retention_first"]["sizes_after"] = _sizes(cur)
            cur.execute("VACUUM news_raw")
            report["retention_first"]["sizes_after_vacuum"] = _sizes(cur)

            # 3) 하루 뒤처럼: 하루치(약 750행)를 더 만료시키고 다시 돈다 - 매일의 정상 상태
            cur.execute("UPDATE news_raw SET raw_news_crawled_at = raw_news_crawled_at - interval '1 day' "
                        "WHERE raw_news_content IS NOT NULL AND raw_news_content <> ''")
        with pg_conn.cursor() as cur:
            code, report["retention_daily"] = _timed_with_writer(
                database_url, press_id, tag, pending_ids, cur, lambda: main(["retention"]))
            assert code == 0
            cur.execute("SELECT stats FROM job_runs WHERE job = 'retention' ORDER BY id DESC LIMIT 1")
            stats = cur.fetchone()[0]
            report["retention_daily"]["job_stats"] = {k: stats[k] for k in ("purged", "purged_bytes", "batches",
                                                                              "purge_s", "scanned")}
            # 4) 할 일이 없는 실행(멱등): 날마다 치르는 고정 비용
            code, report["retention_noop"] = _timed_with_writer(
                database_url, press_id, tag, pending_ids, cur, lambda: main(["retention"]))
            assert code == 0
            cur.execute("SELECT stats FROM job_runs WHERE job = 'retention' ORDER BY id DESC LIMIT 1")
            assert cur.fetchone()[0]["purged"] == 0
    finally:
        command.upgrade(cfg, "head")
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE press_id = %s", (press_id,))
            cur.execute("DELETE FROM press WHERE press_id = %s", (press_id,))
            cur.execute("DELETE FROM job_runs WHERE id > %s", (last_run,))
            cur.execute("VACUUM news_raw")

    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    with open(os.environ["RETENTION_REPORT"], "w", encoding="utf-8") as f:
        f.write(text + "\n")
