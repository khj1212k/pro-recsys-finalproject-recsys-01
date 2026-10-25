-- scripts/oci/close_donga_pending.sql을 되돌린다(원래 상태·시도 횟수로, 보관 표까지 지운다). ADR 0026 2026-10-25 C-31.
--
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] -f <이 파일>
--
-- 기본은 리허설(ROLLBACK). 보관 표가 없으면 멈춘다. 닫은 뒤 상태가 다른 이유로 바뀐 행(blocked_origin이 아닌 행)은
-- 덮어쓰지 않도록 멈춘다.

\set ON_ERROR_STOP on
\if :{?apply}
\else
\set apply 0
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DO $$
DECLARE kept bigint; drifted bigint;
BEGIN
    IF to_regclass('news_raw_donga_close_pending') IS NULL THEN
        RAISE EXCEPTION '보관 표 news_raw_donga_close_pending이 없다 - 닫지 않았거나 이미 되돌렸다';
    END IF;
    SELECT count(*), count(*) FILTER (WHERE n.raw_news_extract_status IS DISTINCT FROM 'blocked_origin')
      INTO kept, drifted
    FROM news_raw_donga_close_pending b JOIN news_raw n ON n.raw_news_id = b.raw_news_id;
    IF drifted > 0 THEN
        RAISE EXCEPTION '닫은 뒤 상태가 다시 바뀐 행이 %개다 - 되돌리지 않는다', drifted;
    END IF;
    RAISE NOTICE 'close_donga_revert restore=%', kept;
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
