-- 국민일보 발행 시각 보정(fix_kmib_created_at*.sql)을 되돌린다(ADR 0008 "2026-10-25 갱신", 런북 6.10). 보관 표까지 지운다.
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] -f <이 파일>
-- 기본은 리허설(ROLLBACK). 보관 표가 없으면 멈춘다. 보정 뒤 발행 시각이 다시 바뀐 행은 덮어쓰지 않도록 멈춘다.

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
    IF to_regclass('news_raw_kmib_created_at_fix') IS NULL THEN
        RAISE EXCEPTION '보관 표 news_raw_kmib_created_at_fix가 없다 - 보정하지 않았거나 이미 되돌렸다';
    END IF;
    SELECT count(*), count(*) FILTER (WHERE n.raw_news_created_at IS DISTINCT FROM b.created_at_after)
      INTO kept, drifted
    FROM news_raw_kmib_created_at_fix b JOIN news_raw n ON n.raw_news_id = b.raw_news_id;
    IF drifted > 0 THEN
        RAISE EXCEPTION '보정 뒤 발행 시각이 다시 바뀐 행이 %개다 - 되돌리지 않는다', drifted;
    END IF;
    RAISE NOTICE 'fix_kmib_created_at_revert restore=%', kept;
END $$;

UPDATE news_raw n SET raw_news_created_at = b.created_at_before
FROM news_raw_kmib_created_at_fix b WHERE n.raw_news_id = b.raw_news_id;

DROP TABLE news_raw_kmib_created_at_fix;

\if :apply
COMMIT;
\echo 'fix_kmib_created_at_revert: COMMIT'
\else
ROLLBACK;
\echo 'fix_kmib_created_at_revert: ROLLBACK (리허설 - -v apply=1이면 커밋한다)'
\endif
