-- AI타임스 기존 행의 발행 시각 9시간 보정 (ADR 0008 "2026-10-17 갱신", docs/runbook-hosting.md 6.7).
--
-- 배경: 2026-10-06 이전 수집기는 시간대 없는 발행 시각에 UTC를 붙였다. AI타임스 피드는 KST 벽시계 시각을 시간대 없이
-- 내보내므로 그 코드로 수집한 행의 raw_news_created_at은 9시간 뒤로 저장돼 있다. 고친 코드로 돈 첫 ingest(= 경계)
-- 앞에 수집한 AI타임스 행 전부에서 9시간을 뺀다(런북 6.7의 "넓은 안"). 원래 값은 보관 표에 남겨 되돌릴 수 있다
-- (scripts/oci/fix_aitimes_created_at_kst_revert.sql).
--
--   psql -X -v ON_ERROR_STOP=1 -v boundary='<경계 UTC>' [-v apply=1] [-v tolerance='10 minutes'] -f <이 파일>
--
-- 기본은 리허설이다: 끝이 ROLLBACK이라 아무것도 바뀌지 않는다. -v apply=1일 때만 COMMIT한다.
-- 한 트랜잭션이고, 아래 기준을 하나라도 어기면 예외로 멈춘다(ON_ERROR_STOP → psql 종료 코드 3, 아무것도 커밋되지 않음):
--   - 보관 표 news_raw_created_at_fix_kst가 이미 있다 = 이 DB는 이미 보정했다(한 행은 한 번만 뺀다)
--   - 보정 대상이 0행이다 = 경계 시각이 틀렸다
--   - 보정 뒤에도 수집 시각보다 tolerance 넘게 앞선 AI타임스 행이 있다
--   - 보정한 행의 최소 수집 지연이 8시간을 넘는다 = 이미 보정한 행을 한 번 더 뺀 것이다(보관 표를 잃은 DB)
-- 병합된 DB(런북 6.4)에서는 돌리지 않는다 - 행이 어느 수집기에서 왔는지 수집 시각으로 가를 수 없다.
-- 트랜잭션을 연 채로 기다리지 않는다(스크립트가 한 번에 끝난다). 열려 있는 동안 같은 행을 쓰는 ingest가 기다린다.

\set ON_ERROR_STOP on
\if :{?boundary}
\else
DO $$ BEGIN RAISE EXCEPTION '-v boundary=<고친 코드로 돈 첫 ingest의 started_at, UTC>가 필요하다 (런북 6.7 2단계)'; END $$;
\endif
\if :{?apply}
\else
\set apply 0
\endif
\if :{?tolerance}
\else
\set tolerance '10 minutes'
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SELECT set_config('fix_kst.tolerance', :'tolerance', true) AS tolerance \gset fix_kst_

CREATE TABLE news_raw_created_at_fix_kst (
    raw_news_id integer PRIMARY KEY,
    created_at_before timestamptz NOT NULL,
    created_at_after timestamptz NOT NULL,
    boundary_utc timestamp NOT NULL,
    fixed_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE news_raw_created_at_fix_kst IS
    'AI타임스 발행 시각 9시간 보정(ADR 0008 2026-10-17)의 원래 값. 되돌리기용이자 "이 DB는 보정했다"는 표시 - 지우지 않는다.';

INSERT INTO news_raw_created_at_fix_kst (raw_news_id, created_at_before, created_at_after, boundary_utc)
SELECT n.raw_news_id, n.raw_news_created_at, n.raw_news_created_at - interval '9 hours', :'boundary'::timestamp
FROM news_raw n
JOIN press p ON p.press_id = n.press_id
WHERE p.press_name = 'AI타임스'
  AND n.raw_news_created_at IS NOT NULL
  AND n.raw_news_crawled_at < :'boundary'::timestamp;

UPDATE news_raw n
SET raw_news_created_at = b.created_at_after
FROM news_raw_created_at_fix_kst b
WHERE n.raw_news_id = b.raw_news_id;

DO $$
DECLARE
    tol interval := current_setting('fix_kst.tolerance')::interval;
    fixed bigint;
    applied bigint;
    ahead bigint;
    min_lag interval;
    max_lag interval;
BEGIN
    SELECT count(*) INTO fixed FROM news_raw_created_at_fix_kst;
    IF fixed = 0 THEN
        RAISE EXCEPTION '보정 대상이 0행이다 - 경계 시각을 확인한다(런북 6.7 2단계)';
    END IF;
    SELECT count(*) INTO applied
    FROM news_raw n JOIN news_raw_created_at_fix_kst b ON b.raw_news_id = n.raw_news_id
    WHERE n.raw_news_created_at = b.created_at_after;
    IF applied <> fixed THEN
        RAISE EXCEPTION '보관 표 %행 중 %행만 바뀌었다', fixed, applied;
    END IF;
    SELECT count(*) INTO ahead
    FROM news_raw n JOIN press p ON p.press_id = n.press_id
    WHERE p.press_name = 'AI타임스'
      AND n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + tol;
    IF ahead > 0 THEN
        RAISE EXCEPTION '보정 뒤에도 AI타임스 %행의 발행 시각이 수집보다 % 넘게 뒤다', ahead, tol;
    END IF;
    SELECT min((n.raw_news_crawled_at AT TIME ZONE 'UTC') - n.raw_news_created_at),
           max((n.raw_news_crawled_at AT TIME ZONE 'UTC') - n.raw_news_created_at)
      INTO min_lag, max_lag
    FROM news_raw n JOIN news_raw_created_at_fix_kst b ON b.raw_news_id = n.raw_news_id;
    IF min_lag > interval '8 hours' THEN
        RAISE EXCEPTION '보정한 행의 최소 수집 지연이 %다 - 이미 보정한 행을 한 번 더 뺀 것으로 보인다', min_lag;
    END IF;
    RAISE NOTICE 'fix_kst fixed=% min_lag=% max_lag=% ahead_after_fix=% tolerance=%', fixed, min_lag, max_lag, ahead, tol;
END $$;

SELECT count(*) AS fixed_rows,
       min(b.created_at_before) AS first_before,
       max(b.created_at_before) AS last_before,
       min(b.boundary_utc) AS boundary_utc
FROM news_raw_created_at_fix_kst b;

\if :apply
COMMIT;
\echo 'fix_kst: COMMIT'
\else
ROLLBACK;
\echo 'fix_kst: ROLLBACK (리허설 - -v apply=1이면 커밋한다)'
\endif
