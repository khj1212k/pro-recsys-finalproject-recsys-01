-- 동아일보의 '본문 추출 대기' 행을 닫는다 (ADR 0026 2026-10-25 C-31 결정, docs/runbook-hosting.md 6.8).
--
-- 배경: 동아일보는 VM(OCI) 출발지의 본문 요청을 거절한다(ADR 0026 결정 9·증거 10). 2026-10-25부터 수집기는 동아일보의
-- RSS 요약을 본문 칸에 바로 넣고 상태를 'rss_summary'로 닫는다. 그 전에 들어온 동아일보 행 중 아직 추출 대기인 것
-- (상태 NULL, 또는 재시도 가능한 상태로 시도 횟수 상한 전)은 본문을 영영 받지 못하는데 매 회차 다시 대상이 되고
-- 언론사별 차단기가 미뤘다. 그 행들을 본문 없이 'blocked_origin'으로 닫는다. 원래 상태·시도 횟수는 보관 표에 남겨
-- 되돌릴 수 있다(scripts/oci/close_donga_pending_revert.sql). 요약은 피드에 남아 있지 않아 채우지 않는다.
--
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] [-v max_attempts=3] -f <이 파일>
--
-- 기본은 리허설(ROLLBACK)이다. -v apply=1일 때만 COMMIT한다. 한 트랜잭션이고 아래를 어기면 예외로 멈춘다:
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
           OR (n.raw_news_extract_status IN ('fetch_failed', 'error')
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
