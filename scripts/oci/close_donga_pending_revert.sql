-- scripts/oci/close_donga_pending.sql을 되돌린다(원래 상태·시도 횟수로, 보관 표까지 지운다). ADR 0026 2026-10-25 C-31.
--
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] -f <이 파일>
--
-- 기본은 리허설(ROLLBACK). 보관 표가 없으면 멈춘다. 닫은 뒤 상태가 다른 이유로 바뀐 행(blocked_origin이 아닌 행)은
-- 덮어쓰지 않도록 멈춘다. ingest가 돌고 있어도 멈춘다(close_donga_pending.sql과 같은 방식).
--
-- 되돌리는 것은 상태와 시도 횟수뿐이다. 본문 보존 잡(ADR 0023)이 닫은 뒤에 이 행들을 돌았다면(수집 후 30일) 본문 ''는
-- 이미 NULL이고 raw_news_content_purged_at이 찍혀 있다 - 되돌려도 그 칸들은 그대로이고 상태만 NULL 또는 fetch_failed로
-- 돌아온다. 추출기는 비운 행(purged_at 있음)을 다시 고르지 않으므로 본문을 다시 받으러 가지는 않는다(embed 단계의
-- awaiting_extraction 집계에는 상태가 NULL인 그 행이 계속 잡힌다). 되돌리기 전에 아래 NOTICE의 purged 수를 본다.

\set ON_ERROR_STOP on
\if :{?apply}
\else
\set apply 0
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

LOCK TABLE job_runs IN SHARE ROW EXCLUSIVE MODE;
DO $$
DECLARE running bigint; since timestamptz;
BEGIN
    SELECT count(*), min(started_at) INTO running, since FROM job_runs WHERE job = 'ingest' AND status = 'running';
    IF running > 0 THEN
        RAISE EXCEPTION 'ingest가 돌고 있다(job_runs running %건, 가장 이른 시작 %) - 끝난 뒤에 다시 돌린다', running, since;
    END IF;
END $$;

DO $$
DECLARE kept bigint; drifted bigint; purged bigint;
BEGIN
    IF to_regclass('news_raw_donga_close_pending') IS NULL THEN
        RAISE EXCEPTION '보관 표 news_raw_donga_close_pending이 없다 - 닫지 않았거나 이미 되돌렸다';
    END IF;
    SELECT count(*), count(*) FILTER (WHERE n.raw_news_extract_status IS DISTINCT FROM 'blocked_origin'),
           count(*) FILTER (WHERE n.raw_news_content_purged_at IS NOT NULL)
      INTO kept, drifted, purged
    FROM news_raw_donga_close_pending b JOIN news_raw n ON n.raw_news_id = b.raw_news_id;
    IF drifted > 0 THEN
        RAISE EXCEPTION '닫은 뒤 상태가 다시 바뀐 행이 %개다 - 되돌리지 않는다', drifted;
    END IF;
    RAISE NOTICE 'close_donga_revert restore=% purged=%', kept, purged;
END $$;

UPDATE news_raw n
SET raw_news_extract_status = b.status_before,
    raw_news_extract_attempts = b.attempts_before
FROM news_raw_donga_close_pending b
WHERE n.raw_news_id = b.raw_news_id;

DROP TABLE news_raw_donga_close_pending;

\if :apply
COMMIT;
\echo 'close_donga_revert: COMMIT'
\else
ROLLBACK;
\echo 'close_donga_revert: 리허설 - ROLLBACK (적용하려면 -v apply=1)'
\endif
