-- scripts/oci/fix_aitimes_created_at_kst.sql의 보정을 되돌린다(원래 값으로, 보관 표까지 지운다 - 그래야 범위를 바꿔 다시
-- 보정할 수 있다). ADR 0008 "2026-10-17 갱신", docs/runbook-hosting.md 6.7.
--
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] -f <이 파일>
--
-- 기본은 리허설(ROLLBACK). 보관 표가 없으면 멈춘다(되돌릴 것이 없다). 보정 뒤 발행 시각을 다른 이유로 다시 쓴 행
-- (보관 표의 created_at_after와 다른 행)이 있으면 그 행을 덮어쓰지 않도록 멈춘다.

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
    IF to_regclass('news_raw_created_at_fix_kst') IS NULL THEN
        RAISE EXCEPTION '보관 표 news_raw_created_at_fix_kst가 없다 - 이 DB는 보정하지 않았거나 이미 되돌렸다';
    END IF;
    SELECT count(*), count(*) FILTER (WHERE n.raw_news_created_at IS DISTINCT FROM b.created_at_after)
      INTO kept, drifted
    FROM news_raw_created_at_fix_kst b JOIN news_raw n ON n.raw_news_id = b.raw_news_id;
    IF drifted > 0 THEN
        RAISE EXCEPTION '보정 뒤 발행 시각이 다시 바뀐 행이 %개다 - 되돌리지 않는다', drifted;
    END IF;
    RAISE NOTICE 'fix_kst_revert rows=%', kept;
END $$;

UPDATE news_raw n
SET raw_news_created_at = b.created_at_before
FROM news_raw_created_at_fix_kst b
WHERE n.raw_news_id = b.raw_news_id;

DROP TABLE news_raw_created_at_fix_kst;

\if :apply
COMMIT;
\echo 'fix_kst_revert: COMMIT'
\else
ROLLBACK;
\echo 'fix_kst_revert: ROLLBACK (리허설 - -v apply=1이면 커밋한다)'
\endif
