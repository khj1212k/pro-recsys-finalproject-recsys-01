"""`python -m jobs.run retention`(본문 보존, ADR 0023)과 마이그레이션 f1db3221e45f를 실제 Postgres에서 확인한다.

모든 본문은 지어낸 문장이다. 수집 시각은 DB의 `now() AT TIME ZONE 'UTC'` 기준으로 넣는다(컬럼이 시간대 없는 UTC).
면제 출처는 config/sources.py의 표에 이 테스트만의 언론사 이름을 재배포 가능(공공누리 제1유형)으로 더해 만든다 -
잡은 실제 코드 경로(exempt_press_names)로 그 표를 읽는다. 상업 언론사 역할의 언론사는 표에 없는 이름이라
"모르는 출처는 재배포 불가(fail closed)"도 함께 확인된다.
"""
import hashlib
import os
import time
import uuid
from datetime import datetime, timedelta

import numpy as np
import psycopg2
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEAD = "f1db3221e45f"
BEFORE = "a6f1e83b0d57"
DIM = 1024

# 이름표 -> (언론사 역할, 나이(수집 후 경과), 추출 상태, 본문 있음, 해시 있음, 시도 횟수, 공공누리 유형, 임베딩)
ROWS = {
    "expired_ok":       ("commercial", "30 days 1 hour", "ok", True, True, 1, None, True),
    "fresh_ok":         ("commercial", "29 days 23 hours", "ok", True, True, 1, None, True),
    "legacy_no_hash":   ("commercial", "32 days", "ok", True, False, 1, None, False),
    "dropped":          ("commercial", "31 days", "dropped", False, False, 1, None, False),
    "retrying":         ("commercial", "31 days", "fetch_failed", False, False, 1, None, False),
    "gave_up":          ("commercial", "31 days", "fetch_failed", False, False, 3, None, False),
    "never_extracted":  ("commercial", "31 days", None, False, False, 0, None, False),
    "policy_type1":     ("policy", "31 days", "ok", True, True, 0, 1, False),
    "policy_untyped":   ("policy", "31 days", "ok", True, True, 0, None, False),
    "policy_type2":     ("policy", "31 days", "ok", True, True, 0, 2, False),
    "recent_pending":   ("commercial", "1 hour", None, False, False, 0, None, False),
}
PURGED_BY_RUN = {"expired_ok", "legacy_no_hash", "dropped", "gave_up", "policy_type2"}
WITH_BODY = {k for k, v in ROWS.items() if v[3]}


def _body(key, suffix):
    return f"합성 기사 {key} {suffix}의 본문이다. 숫자 12와 고유명사 없이 지어낸 문장을 되풀이한다. " * 30


def _vec(axis):
    v = np.zeros(DIM, dtype=np.float32)
    v[axis % DIM] = 1.0
    return "[" + ",".join(f"{x:.1f}" for x in v) + "]"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(os.path.join(REPO_ROOT, "backend", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "backend", "alembic"))
    return cfg


@pytest.fixture
def world(database_url, pg_conn, isolated_news_raw, monkeypatch):
    """ROWS의 행을 만들고 이름표 -> raw_news_id를 돌려준다. 끝나면 이 테스트의 행·언론사·job_runs를 지운다."""
    from config import sources
    from config.settings import Settings

    suffix = uuid.uuid4().hex[:8]
    names = {"commercial": f"itest-상업-{suffix}", "policy": f"itest-정책-{suffix}"}
    monkeypatch.setitem(sources.SOURCE_LICENSES, names["policy"], sources.KOGL_TYPE_1_POLICY_BRIEFING)
    monkeypatch.setattr(Settings, "MAX_EXTRACT_ATTEMPTS", 3)
    monkeypatch.setattr(Settings, "BODY_RETENTION_DAYS", 30)
    ids, press_ids = {}, {}
    with pg_conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM job_runs")
        (last_run,) = cur.fetchone()
        for role, name in names.items():
            cur.execute("INSERT INTO press (press_name) VALUES (%s) RETURNING press_id", (name,))
            press_ids[role] = cur.fetchone()[0]
        for n, (key, (role, age, status, has_body, has_hash, attempts, kogl, embedded)) in enumerate(ROWS.items()):
            body = _body(key, suffix) if has_body else ""
            cur.execute(
                """
                INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at,
                    raw_news_crawled_at, raw_news_extract_status, raw_news_extracted_at, raw_news_extract_attempts,
                    raw_news_content_sha256, raw_news_content_length, raw_news_kogl_type, embedding_result)
                VALUES (%s, %s, %s, %s, now(), (now() AT TIME ZONE 'UTC') - %s::interval, %s,
                        CASE WHEN %s THEN now() END, %s, %s, %s, %s, %s::vector)
                RETURNING raw_news_id
                """,
                (press_ids[role], f"합성 제목 {key}", body, f"https://itest.example/{suffix}/{key}", age, status,
                 status is not None, attempts,
                 hashlib.sha256(body.encode("utf-8")).hexdigest() if has_hash else None,
                 len(body) if has_hash else None, kogl, _vec(n) if embedded else None),
            )
            ids[key] = cur.fetchone()[0]
    try:
        yield {"ids": ids, "suffix": suffix, "press_ids": press_ids, "names": names}
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM news_raw WHERE press_id = ANY(%s)", (list(press_ids.values()),))
            cur.execute("DELETE FROM press WHERE press_id = ANY(%s)", (list(press_ids.values()),))
            cur.execute("DELETE FROM job_runs WHERE id > %s", (last_run,))


def _state(pg_conn, ids):
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_news_id, raw_news_content, raw_news_content_sha256, raw_news_content_length,
                   raw_news_content_purged_at, raw_news_title, raw_news_url, raw_news_extract_status,
                   embedding_result IS NOT NULL
            FROM news_raw WHERE raw_news_id = ANY(%s)
            """,
            (list(ids.values()),),
        )
        by_id = {r[0]: r[1:] for r in cur.fetchall()}
    keys = ("content", "sha", "length", "purged_at", "title", "url", "status", "embedded")
    return {name: dict(zip(keys, by_id[rid])) for name, rid in ids.items()}


def _last_run(pg_conn, job="retention"):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT status, stats, error FROM job_runs WHERE job = %s ORDER BY id DESC LIMIT 1", (job,))
        return cur.fetchone()


# --------------------------------------------------------------------------- 경계·면제·멱등


def test_dry_run_then_purge_then_idempotent_rerun(pg_conn, world):
    from jobs.run import main

    ids, suffix = world["ids"], world["suffix"]
    before = _state(pg_conn, ids)

    # 1) dry run: 세기만 한다
    assert main(["retention", "--dry-run"]) == 0
    status, stats, error = _last_run(pg_conn)
    assert status == "succeeded", error
    assert stats["dry_run"] is True and stats["purged"] == 0
    assert stats["scanned"] == 9  # 기한 지난 행 = 1시간 전·29일 23시간 전 행을 뺀 전부
    assert stats["purgeable"] == len(PURGED_BY_RUN) and stats["purgeable_bodies"] == 3
    assert stats["skipped_exempt"] == 2  # 정책 출처의 유형 1·유형 미기록 행
    assert stats["skipped_awaiting_extraction"] == 2  # 재시도 여지가 남은 행, 추출 전 행
    assert stats["remaining_expired_bodies"] == 3
    assert _state(pg_conn, ids) == before

    # 2) 실제 실행
    assert main(["retention"]) == 0
    status, stats, error = _last_run(pg_conn)
    assert status == "succeeded", error
    assert (stats["purged"], stats["purged_bodies"], stats["hash_filled"]) == (len(PURGED_BY_RUN), 3, 1)
    assert stats["remaining_expired_bodies"] == 0 and "warnings" not in stats
    after = _state(pg_conn, ids)
    for key in ROWS:
        row, old = after[key], before[key]
        # 제목·URL·추출 상태·임베딩은 어느 행에서도 바뀌지 않는다
        assert (row["title"], row["url"], row["status"], row["embedded"]) == \
               (old["title"], old["url"], old["status"], old["embedded"]), key
        if key in PURGED_BY_RUN:
            assert row["content"] is None and row["purged_at"] is not None, key
            assert row["length"] == len(old["content"]), key
            expected_sha = hashlib.sha256(_body(key, suffix).encode("utf-8")).hexdigest() if key in WITH_BODY else None
            assert row["sha"] == expected_sha, key
        else:
            assert row == old, key
    # 경계: 30일 1시간 전은 비워지고 29일 23시간 전은 그대로
    assert after["expired_ok"]["content"] is None and after["fresh_ok"]["content"] == before["fresh_ok"]["content"]
    # 가장 오래 남은 상업 언론 본문 = 29일 23시간 전 행
    assert 29.9 < stats["oldest_remaining_body_age_days"] < 30.0

    # 3) 다시 돌려도 아무것도 바뀌지 않는다
    assert main(["retention"]) == 0
    status, stats, _ = _last_run(pg_conn)
    assert status == "succeeded" and stats["purged"] == 0 and stats["purgeable"] == 0
    assert _state(pg_conn, ids) == after


def test_boundary_is_the_same_when_the_session_time_zone_is_kst(pg_conn, world, monkeypatch):
    """세션 TimeZone이 UTC가 아니어도(PGTZ=Asia/Seoul) 경계가 같다. 기준 시각을 `now() AT TIME ZONE 'UTC'`로 만들어
    시간대 없는 수집 시각과 시간대 없는 UTC 값끼리 비교하기 때문이다 - KST 세션에서 `now()`와 바로 비교했다면
    9시간 어긋나 29일 23시간 전 행까지 비워진다. 일일 리포트의 보존 상태도 같은 식이다."""
    from db.connection import connect_unpooled
    from jobs.run import main
    from jobs.tasks.daily_report import retention_status

    ids = world["ids"]
    before = _state(pg_conn, ids)
    monkeypatch.setenv("PGTZ", "Asia/Seoul")  # 잡이 여는 연결(실행 기록·작업)이 모두 KST 세션이 된다
    probe = connect_unpooled(application_name="itest:retention:pgtz")
    try:
        with probe.cursor() as cur:
            cur.execute("SHOW TimeZone")
            assert cur.fetchone()[0] == "Asia/Seoul"
            cur.execute("SELECT now() AT TIME ZONE 'UTC'")
            (now_utc,) = cur.fetchone()
            status_kst = retention_status(cur, "24 hours")
    finally:
        probe.close()
    assert (status_kst["expired_bodies"], status_kst["overdue_bodies"]) == (3, 1)

    assert main(["retention"]) == 0
    status, stats, error = _last_run(pg_conn)
    assert status == "succeeded", error
    after = _state(pg_conn, ids)
    assert {key for key, row in after.items() if row["purged_at"] is not None} == PURGED_BY_RUN
    assert after["fresh_ok"] == before["fresh_ok"]  # 29일 23시간 전 행은 KST 세션에서도 남는다
    # 기준 시각은 UTC 지금 - 30일(KST로 읽었다면 9시간 차이)
    cutoff = datetime.fromisoformat(stats["cutoff_utc"])
    assert abs(cutoff - (now_utc - timedelta(days=30))) < timedelta(minutes=5)


def test_ingest_snapshot_counts_purged_ok_rows_separately(pg_conn, world):
    """ingest 잡의 news_raw 누적 현황: with_content는 지금 본문이 있는 'ok' 행만, 비운 행은 body_purged."""
    from jobs.run import main
    from jobs.tasks.ingest import news_raw_snapshot

    assert main(["retention"]) == 0
    per_press = news_raw_snapshot()["per_press"]
    commercial, policy = per_press[world["names"]["commercial"]], per_press[world["names"]["policy"]]
    # 상업: 'ok' 세 행 중 fresh_ok만 본문이 남는다. 비운 행 = expired_ok, legacy_no_hash, dropped, gave_up
    assert (commercial["with_content"], commercial["body_purged"]) == (1, 4)
    # 정책 출처: 유형 1·미기록은 면제, 유형 2는 비움
    assert (policy["with_content"], policy["body_purged"]) == (2, 1)


def test_purged_row_cannot_get_a_body_back(pg_conn, world):
    """비운 행에 본문을 다시 쓰면 CHECK(ck_news_raw_purged_body_gone)가 막는다."""
    from jobs.run import main

    assert main(["retention"]) == 0
    with pytest.raises(psycopg2.errors.CheckViolation), pg_conn.cursor() as cur:
        cur.execute("UPDATE news_raw SET raw_news_content = '되살린 본문' WHERE raw_news_id = %s",
                    (world["ids"]["expired_ok"],))


# --------------------------------------------------------------------------- 락


def test_second_run_is_skipped_while_the_advisory_lock_is_held(database_url, pg_conn, world):
    from jobs.run import main
    from jobs.runtime import advisory_lock_key

    holder = psycopg2.connect(database_url)
    holder.autocommit = True
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (advisory_lock_key("retention"),))
        before = _state(pg_conn, world["ids"])
        assert main(["retention"]) == 0
        status, stats, _ = _last_run(pg_conn)
        assert (status, stats["reason"]) == ("skipped", "lock_held")
        assert _state(pg_conn, world["ids"]) == before
    finally:
        holder.close()


def test_rows_locked_by_another_writer_are_skipped_not_waited_for(database_url, pg_conn, world):
    """다른 트랜잭션이 잡고 있는 만료 행은 기다리지 않고 건너뛴다(SKIP LOCKED). 남은 수를 경고로 남기고, 다음 실행이 비운다."""
    from jobs.run import main

    ids = world["ids"]
    writer = psycopg2.connect(database_url)
    try:
        with writer.cursor() as cur:
            cur.execute("SELECT 1 FROM news_raw WHERE raw_news_id = %s FOR UPDATE", (ids["expired_ok"],))
        started = time.monotonic()
        assert main(["retention"]) == 0
        took = time.monotonic() - started
        status, stats, _ = _last_run(pg_conn)
        assert status == "succeeded"
        assert stats["purged"] == len(PURGED_BY_RUN) - 1 and stats["remaining_expired_bodies"] == 1
        assert any("1건" in w for w in stats["warnings"])
        assert took < 5  # 락 대기 한도(5초)에 걸리지 않았다 = 기다리지 않았다
        assert _state(pg_conn, ids)["expired_ok"]["content"] is not None
    finally:
        writer.rollback()
        writer.close()

    assert main(["retention"]) == 0
    assert _last_run(pg_conn)[1]["purged"] == 1
    assert _state(pg_conn, ids)["expired_ok"]["content"] is None


def test_an_open_purge_batch_does_not_block_ingest_writes(database_url, pg_conn, world):
    """보존 잡의 배치 트랜잭션이 열려 있는 동안에도 수집기의 새 행 INSERT, 본문 추출기의 대상 조회와 추출 대기 행
    UPDATE, 일일 리포트식 읽기가 기다리지 않는다(lock_timeout 1초로 확인). 배치가 잡는 테이블 락은 행 단위
    쓰기 락(RowExclusiveLock)까지다."""
    from config.retention import exempt_press_names
    from jobs.tasks import retention

    ids, suffix = world["ids"], world["suffix"]
    batch = psycopg2.connect(database_url)
    ingest = psycopg2.connect(database_url)
    ingest.autocommit = True
    try:
        with batch.cursor() as cur:
            cur.execute(retention.CUTOFF_SQL, {"days": 30})
            _now, cutoff = cur.fetchone()
            cur.execute(retention.PURGE_BATCH_SQL, {
                "cutoff": cutoff, "days": 30, "exempt_presses": exempt_press_names(),
                "retryable": ["fetch_failed", "error"], "max_attempts": 3, "batch_size": 1000,
            })
            assert cur.fetchone()[0] == len(PURGED_BY_RUN)  # 아직 커밋하지 않았다
            cur.execute(
                "SELECT mode FROM pg_locks WHERE pid = pg_backend_pid() AND relation = 'news_raw'::regclass"
            )
            modes = {r[0] for r in cur.fetchall()}
        assert modes <= {"AccessShareLock", "RowShareLock", "RowExclusiveLock"}, modes

        with ingest.cursor() as cur:
            cur.execute("SET lock_timeout = '1s'")
            started = time.monotonic()
            # RSS 수집기: 새 URL INSERT
            cur.execute(
                "INSERT INTO news_raw (press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, "
                "raw_news_crawled_at) VALUES (%s, '새 기사', '', %s, now(), now() AT TIME ZONE 'UTC') "
                "ON CONFLICT (raw_news_url) DO NOTHING",
                (world["press_ids"]["commercial"], f"https://itest.example/{suffix}/new"),
            )
            # 본문 추출기: 추출 대기 행에 결과를 쓴다(보존 잡은 이 행을 건드리지 않는다)
            cur.execute(
                "UPDATE news_raw SET raw_news_content = '', raw_news_extract_status = 'fetch_failed', "
                "raw_news_content_length = 0, raw_news_extracted_at = now(), "
                "raw_news_extract_attempts = raw_news_extract_attempts + 1 WHERE raw_news_id = %s",
                (ids["retrying"],),
            )
            assert cur.rowcount == 1
            # 일일 리포트·상태 조회처럼 읽기만 하는 쿼리
            cur.execute("SELECT count(*) FROM news_raw WHERE raw_news_content IS NOT NULL")
            took = time.monotonic() - started
        assert took < 1.0
    finally:
        batch.rollback()
        batch.close()
        ingest.close()


def test_extractor_never_selects_purged_or_expired_rows(pg_conn, world, monkeypatch):
    """본문 추출기는 보존 기한이 지난 기사와 본문을 비운 기사를 대상으로 고르지 않는다 - 다시 내려받지 않는다.
    재시도 상한을 올려 비운 행의 상태가 "재시도 여지 있음"이 돼도 고르지 않는다."""
    from config.settings import Settings
    from crawler.content_extractor.extractor import ContentExtractor, ExtractionResult
    from jobs.run import main

    ids = world["ids"]
    assert main(["retention"]) == 0
    with pg_conn.cursor() as cur:
        # 보존 기한 안인데 비운 표시가 있는 행을 하나 만든다(예: 비운 뒤 수집 시각이 고쳐진 행)
        cur.execute(
            "UPDATE news_raw SET raw_news_crawled_at = (now() AT TIME ZONE 'UTC') - interval '10 days' "
            "WHERE raw_news_id = %s",
            (ids["gave_up"],),
        )
    monkeypatch.setattr(Settings, "MAX_EXTRACT_ATTEMPTS", 5)
    picked = []

    def fake_process(article):
        picked.append(article[0])
        return ExtractionResult(article[0], article[2], "fetch_failed")

    monkeypatch.setattr(ContentExtractor, "process_single_article", staticmethod(fake_process))
    stats = ContentExtractor().extract_parallel_with_stats(num_workers=1)

    assert picked == [ids["recent_pending"]]
    assert stats["targets"] == 1


def test_embedding_and_clustering_skip_purged_rows(pg_conn, world, monkeypatch):
    """임베딩은 본문을 비운 행을 대상으로 삼지 않고 따로 세며, 클러스터링(= 생성 입력)은 그 행을 읽지 않는다."""
    import sys
    import types

    from config.settings import Settings
    from core.clustering.hdbscan_clusterer import NewsClusterer
    from jobs.run import main
    import pipeline.stages as stages

    ids = world["ids"]
    assert main(["retention"]) == 0

    class FakeEmbedder:
        device = "cpu"

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def generate_embeddings_batch(self, texts, batch_size=8):
            assert all(texts)
            return [np.ones(DIM, dtype=np.float32).tolist() for _ in texts], 0.01

    fake_module = types.ModuleType("core.embedder")
    fake_module.NewsEmbedder = FakeEmbedder
    monkeypatch.setitem(sys.modules, "core.embedder", fake_module)

    stats = stages.embed_pending_articles(Settings, batch_size=8)

    # 본문이 있고 임베딩이 없는 행만: 정책 출처 세 행 중 면제된 두 행 (상업 언론의 본문 있는 행은 임베딩이 있거나 비워졌다)
    assert stats["targets"] == 2 and stats["embedded"] == 2
    assert stats["body_purged"] == 2  # 임베딩 전에 비워진 'ok' 행: 해시 없던 행, 정책 출처 유형 2 행
    with pg_conn.cursor() as cur:
        cur.execute("SELECT raw_news_id FROM news_raw WHERE raw_news_id = ANY(%s) AND embedding_result IS NOT NULL",
                    (list(ids.values()),))
        embedded = {r[0] for r in cur.fetchall()}
    assert ids["legacy_no_hash"] not in embedded and ids["policy_type2"] not in embedded
    assert ids["expired_ok"] in embedded  # 비우기 전에 있던 임베딩은 남는다

    data = NewsClusterer()._load_data_from_db(exclude_clustered=False, lookback_hours=24 * 40)
    loaded = set(int(i) for i in data["ids"])
    assert ids["expired_ok"] not in loaded  # 임베딩은 있지만 본문을 비운 행
    assert ids["fresh_ok"] in loaded
    assert all(isinstance(c, str) and c for c in data["contents"])


def test_daily_report_retention_status_counts_expired_and_purged_bodies(pg_conn, world):
    from jobs.run import main
    from jobs.tasks.daily_report import retention_status, retention_warning

    with pg_conn.cursor() as cur:
        before = retention_status(cur, "24 hours")
    assert (before["expired_bodies"], before["overdue_bodies"], before["purged_total"]) == (3, 1, 0)
    assert retention_warning(before) and "1건" in retention_warning(before)

    assert main(["retention"]) == 0
    with pg_conn.cursor() as cur:
        after = retention_status(cur, "24 hours")
    assert (after["expired_bodies"], after["overdue_bodies"]) == (0, 0)
    assert after["purged_24h"] == after["purged_total"] == len(PURGED_BY_RUN)
    assert retention_warning(after) is None


# --------------------------------------------------------------------------- 마이그레이션


def test_downgrade_does_not_resurrect_bodies_and_upgrade_backfills_lengths(database_url, pg_conn, world, monkeypatch):
    from alembic import command
    from jobs.run import main

    ids, suffix = world["ids"], world["suffix"]
    monkeypatch.setenv("DATABASE_URL", database_url)
    cfg = _alembic_config()
    assert main(["retention"]) == 0
    try:
        command.downgrade(cfg, BEFORE)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'news_raw' AND column_name = 'raw_news_content'")
            assert cur.fetchone() == ("NO",)
            cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'news_raw' "
                        "AND column_name IN ('raw_news_content_length', 'raw_news_content_purged_at', "
                        "'raw_news_kogl_type')")
            assert cur.fetchone() == (0,)
            cur.execute("SELECT raw_news_id, raw_news_content, raw_news_content_sha256 FROM news_raw "
                        "WHERE raw_news_id = ANY(%s)", (list(ids.values()),))
            rows = {r[0]: r[1:] for r in cur.fetchall()}
        for key in PURGED_BY_RUN:
            content, sha = rows[ids[key]]
            assert content == "", key  # 본문이 돌아오지 않는다
            if key in WITH_BODY:
                assert sha == hashlib.sha256(_body(key, suffix).encode("utf-8")).hexdigest()
    finally:
        command.upgrade(cfg, "head")

    with pg_conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        assert cur.fetchone() == (HEAD,)
        cur.execute("SELECT conname, convalidated FROM pg_constraint WHERE conrelid = 'news_raw'::regclass "
                    "AND conname IN ('ck_news_raw_kogl_type', 'ck_news_raw_purged_body_gone')")
        assert dict(cur.fetchall()) == {"ck_news_raw_kogl_type": True, "ck_news_raw_purged_body_gone": True}
        cur.execute("SELECT count(*) FROM news_raw WHERE raw_news_id = ANY(%s) AND raw_news_content IS NOT NULL "
                    "AND raw_news_content_length IS DISTINCT FROM char_length(raw_news_content)",
                    (list(ids.values()),))
        assert cur.fetchone() == (0,)  # 길이를 채웠다(downgrade로 ''가 된 행은 0)
    state = _state(pg_conn, ids)
    assert all(state[k]["content"] == "" and state[k]["purged_at"] is None for k in PURGED_BY_RUN)

    # 비운 표시는 사라졌지만 본문도 없다. 다시 돌리면 같은 행이 다시 비워진다(NULL). 공공누리 유형 열도 내렸다
    # 올리며 비었으므로, 유형 2였던 정책 출처 행은 이제 "유형 미기록 = 면제"로 보여 ''로 남는다 - 본문은 여전히 없다.
    assert main(["retention"]) == 0
    state = _state(pg_conn, ids)
    for key in PURGED_BY_RUN - {"policy_type2"}:
        assert state[key]["content"] is None and state[key]["purged_at"] is not None, key
    assert (state["policy_type2"]["content"], state["policy_type2"]["purged_at"]) == ("", None)


def test_news_raw_model_matches_the_retention_migration(database_url):
    """autogenerate가 본문 NULL 허용과 새 컬럼 셋에서 모델과 스키마의 차이를 보고하지 않는다."""
    import sys

    sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    import app.models  # noqa: F401

    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            diffs = compare_metadata(MigrationContext.configure(conn), SQLModel.metadata)
    finally:
        engine.dispose()
    related = [d for d in diffs if "raw_news_content" in repr(d) or "raw_news_kogl_type" in repr(d)]
    assert related == []
