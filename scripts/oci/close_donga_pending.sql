-- 동아일보의 '본문 추출 대기' 행을 닫는다 (ADR 0026 2026-10-25 C-31 결정, docs/runbook-hosting.md 6.8).
--
-- 배경: 동아일보는 VM(OCI) 출발지의 본문 요청을 거절한다(ADR 0026 결정 9·증거 10). 2026-10-25부터 수집기는 동아일보의
-- RSS 요약을 본문 칸에 바로 넣고 상태를 'rss_summary'로 닫는다(짧거나 같은 요약은 'dropped'/'duplicate'). 그 전에 들어온
-- 동아일보 행 중 아직 추출 대기인 것(상태 NULL, 또는 재시도 가능한 상태로 시도 횟수 상한 전)은 본문을 영영 받지 못하는데
-- 매 회차 다시 대상이 되고 언론사별 차단기가 미뤘다. 그 행들을 본문 없이 'blocked_origin'으로 닫는다. 원래 상태·시도 횟수는
-- 보관 표에 남겨 되돌릴 수 있다(scripts/oci/close_donga_pending_revert.sql).
-- 요약은 채우지 않는다: 피드에 아직 남아 있는 배포 전 행도 수집기의 INSERT ... ON CONFLICT (raw_news_url) DO NOTHING이
-- 이미 있는 URL을 건너뛰어 요약을 받지 못한다(수집기는 기존 행을 고치지 않는다).
--
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] [-v max_attempts=3] -f <이 파일>
--
-- max_attempts는 VM .env의 MAX_EXTRACT_ATTEMPTS(없으면 3, config/settings.py)와 같아야 한다 - 다르면 "추출 대기"의 뜻이
-- 추출기와 어긋난다: 더 작으면 추출기가 아직 다시 시도할 행을 닫지 못하고, 더 크면 재시도를 이미 마친 fetch_failed까지
-- 닫는다. 아래 두 곳의
-- ('fetch_failed', 'error')는 config/retention.py의 RETRYABLE_EXTRACT_STATUSES를 옮겨 적은 것이다 - 그 값이 바뀌면 함께
-- 고쳐야 한다(tests/test_news_raw_extract_statuses.py가 같은지 확인한다).
--
-- 기본은 리허설(ROLLBACK)이다. -v apply=1일 때만 COMMIT한다. 한 트랜잭션이고 아래를 어기면 예외로 멈춘다:
--   - ingest가 돌고 있다(job_runs에 job = 'ingest' AND status = 'running') - 같은 행을 두고 추출기와 다투지 않게 한다.
--     시작 직후 job_runs에 쓰려는 새 실행은 이 트랜잭션이 끝날 때까지 기다린다(job_runs를 SHARE ROW EXCLUSIVE로 잠근다)
--   - 보관 표 news_raw_donga_close_pending이 이미 있다 = 이미 닫았다
--   - 닫은 뒤에도 동아일보에 추출 대기 행이 남아 있다
-- 본문이 있는 행(ok 등)과 이미 끝난 행은 건드리지 않는다.

\set ON_ERROR_STOP on
\if :{?apply}
\else
\set apply 0
\endif
\if :{?max_attempts}
\else
\set max_attempts 3
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SELECT set_config('close_donga.max_attempts', :'max_attempts', true) AS max_attempts \gset close_donga_

-- 동시 실행 방지: 새 잡 실행이 job_runs에 행을 쓰지 못하게 잠근 뒤(읽기는 막지 않는다) 도는 ingest가 없음을 본다.
-- 잠그기 전에 확인하면 확인과 닫기 사이에 ingest가 시작할 수 있다.
LOCK TABLE job_runs IN SHARE ROW EXCLUSIVE MODE;
DO $$
DECLARE running bigint; since timestamptz;
BEGIN
    SELECT count(*), min(started_at) INTO running, since FROM job_runs WHERE job = 'ingest' AND status = 'running';
    IF running > 0 THEN
        RAISE EXCEPTION 'ingest가 돌고 있다(job_runs running %건, 가장 이른 시작 %) - 끝난 뒤에 다시 돌린다. 프로세스가 죽어 남은 행이면 다음 ingest가 abandoned로 바꾼다', running, since;
    END IF;
END $$;

CREATE TABLE news_raw_donga_close_pending (
    raw_news_id integer PRIMARY KEY,
    status_before varchar,
    attempts_before smallint NOT NULL,
    closed_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE news_raw_donga_close_pending IS
    '동아일보 추출 대기 행을 blocked_origin으로 닫기 전의 상태(ADR 0026 2026-10-25 C-31). 되돌리기용 - 지우지 않는다.';

INSERT INTO news_raw_donga_close_pending (raw_news_id, status_before, attempts_before)
SELECT n.raw_news_id, n.raw_news_extract_status, n.raw_news_extract_attempts
FROM news_raw n
JOIN press p ON p.press_id = n.press_id
WHERE p.press_name = '동아일보'
  AND COALESCE(n.raw_news_content, '') = ''
  -- 추출 대기 = 추출기가 고르는 행(crawler/content_extractor/extractor.py, config.retention.RETRYABLE_EXTRACT_STATUSES)
  AND (n.raw_news_extract_status IS NULL
       OR (n.raw_news_extract_status IN ('fetch_failed', 'error')
           AND n.raw_news_extract_attempts < current_setting('close_donga.max_attempts')::int));

UPDATE news_raw n
SET raw_news_extract_status = 'blocked_origin'
FROM news_raw_donga_close_pending b
WHERE n.raw_news_id = b.raw_news_id;

DO $$
DECLARE
    closed bigint;
    applied bigint;
    left_pending bigint;
BEGIN
    SELECT count(*) INTO closed FROM news_raw_donga_close_pending;
    SELECT count(*) INTO applied
    FROM news_raw n JOIN news_raw_donga_close_pending b ON b.raw_news_id = n.raw_news_id
    WHERE n.raw_news_extract_status = 'blocked_origin';
    IF applied <> closed THEN
        RAISE EXCEPTION '보관 표 %행 중 %행만 바뀌었다', closed, applied;
    END IF;
    SELECT count(*) INTO left_pending
    FROM news_raw n JOIN press p ON p.press_id = n.press_id
    WHERE p.press_name = '동아일보' AND COALESCE(n.raw_news_content, '') = ''
      AND (n.raw_news_extract_status IS NULL
           OR (n.raw_news_extract_status IN ('fetch_failed', 'error')  -- RETRYABLE_EXTRACT_STATUSES
               AND n.raw_news_extract_attempts < current_setting('close_donga.max_attempts')::int));
    IF left_pending > 0 THEN
        RAISE EXCEPTION '닫은 뒤에도 동아일보 추출 대기 행이 %개 남았다', left_pending;
    END IF;
    RAISE NOTICE 'close_donga closed=%', closed;
END $$;

\if :apply
COMMIT;
\echo 'close_donga: COMMIT'
\else
ROLLBACK;
\echo 'close_donga: 리허설 - ROLLBACK (적용하려면 -v apply=1)'
\endif
