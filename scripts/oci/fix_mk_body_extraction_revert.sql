-- scripts/oci/fix_mk_body_extraction.sql의 보관 표를 정리한다 (ADR 0026 결정 9의 "2026-10-25 추가: 매일경제 본문 컨테이너",
-- docs/runbook-hosting.md 6.12). 보관 표를 지워야 범위(until·min_length)를 바꿔 다시 재추출할 수 있다.
--
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] -f <이 파일>
--
-- 기본은 리허설(ROLLBACK). 보관 표가 없으면 멈춘다(정리할 것이 없다).
--
-- 이 스크립트는 news_raw를 바꾸지 않는다. 국민일보 복구(fix_kmib_body_extraction_revert.sql)와 달리 되돌릴 행이 없다:
--   - 재추출이 되돌린 행은 원래 ok(본문 있음) 또는 duplicate(본문 원래 없음 - target=shared가 ok 행의 짝으로 되돌린 행)였다.
--     보관 표에는 본문을 복사하지 않았으므로(ADR 0023: 새 본문 사본 위치는 개정과 승인이 필요하다) 옛 본문은 이 표로 돌아오지 않는다.
--     본문 없는 'ok'를 만들면 본문 길이·해시와 어긋난다
--   - 아직 다시 받지 않은 행(미시도)은 그대로 둔다: 다음 ingest가 원본 페이지에서 다시 받는다. 그 ingest가 옛 추출기라면(고친 추출기를
--     되돌렸다면) 옛 본문과 같은 부푼 글이 다시 채워진다 - 되돌린 것과 같은 결과다
--   - 이미 다시 받은 행은 새 추출 결과다. 건드리지 않는다
--   옛 본문이 꼭 필요하면 재추출 전에 뜬 호스트 밖 백업(런북 6.5, 본문 사본 full/ - 수집 27일 이내 행만 담고 하루 뒤 지워진다)에서
--   본문 포함 복원(--with-bodies)을 한다.
-- 하는 일: 보관한 행이 지금 어떤 상태인지 NOTICE로 알리고, 보관 표를 지운다. 알려 주는 값은 재추출이 어떻게 끝났는지 보는 확인용이다
-- (다시 받지 못한 행 = fetch_failed·error는 옛 본문이 사라진 행이다).
--
--   NOTICE: fix_mk_revert audited=<보관한 행> unattempted=<아직 미시도> reextracted_ok=<ok로 다시 받음> dropped=<350자 미만으로 버림>
--           empty=<본문 없음> duplicate=<다른 ok 행과 같은 본문> failed=<fetch_failed·error> purged_or_missing=<본문을 비웠거나 없어진 행>

\set ON_ERROR_STOP on
\if :{?apply}
\else
\set apply 0
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

DO $$
BEGIN
    IF to_regclass('news_raw_mk_extract_fix') IS NULL THEN
        RAISE EXCEPTION '보관 표 news_raw_mk_extract_fix가 없다 - 이 DB는 재추출하지 않았거나 이미 정리했다';
    END IF;
END $$;

DO $$
DECLARE
    audited bigint;
    unattempted bigint;
    reextracted_ok bigint;
    dropped bigint;
    emptied bigint;
    duplicated bigint;
    failed bigint;
    purged bigint;
    absent bigint;
BEGIN
    SELECT count(*),
           count(*) FILTER (WHERE n.raw_news_id IS NOT NULL AND n.raw_news_content_purged_at IS NOT NULL),
           count(*) FILTER (WHERE n.raw_news_id IS NULL),
           count(*) FILTER (WHERE n.raw_news_id IS NOT NULL AND n.raw_news_content_purged_at IS NULL AND n.raw_news_extract_status IS NULL),
           count(*) FILTER (WHERE n.raw_news_id IS NOT NULL AND n.raw_news_content_purged_at IS NULL AND n.raw_news_extract_status = 'ok'),
           count(*) FILTER (WHERE n.raw_news_id IS NOT NULL AND n.raw_news_content_purged_at IS NULL AND n.raw_news_extract_status = 'dropped'),
           count(*) FILTER (WHERE n.raw_news_id IS NOT NULL AND n.raw_news_content_purged_at IS NULL AND n.raw_news_extract_status = 'empty'),
           count(*) FILTER (WHERE n.raw_news_id IS NOT NULL AND n.raw_news_content_purged_at IS NULL AND n.raw_news_extract_status = 'duplicate'),
           count(*) FILTER (WHERE n.raw_news_id IS NOT NULL AND n.raw_news_content_purged_at IS NULL AND n.raw_news_extract_status IN ('fetch_failed', 'error'))
      INTO audited, purged, absent, unattempted, reextracted_ok, dropped, emptied, duplicated, failed
    FROM news_raw_mk_extract_fix f
    LEFT JOIN news_raw n ON n.raw_news_id = f.raw_news_id;
    RAISE NOTICE 'fix_mk_revert audited=% unattempted=% reextracted_ok=% dropped=% empty=% duplicate=% failed=% purged_or_missing=%',
        audited, unattempted, reextracted_ok, dropped, emptied, duplicated, failed, purged + absent;
END $$;

DROP TABLE news_raw_mk_extract_fix;

\if :apply
COMMIT;
\echo 'fix_mk_revert: COMMIT'
\else
ROLLBACK;
\echo 'fix_mk_revert: ROLLBACK (리허설 - -v apply=1이면 커밋한다)'
\endif
