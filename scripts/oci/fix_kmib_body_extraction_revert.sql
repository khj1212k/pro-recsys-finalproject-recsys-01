-- scripts/oci/fix_kmib_body_extraction.sql의 복구를 되돌린다 (ADR 0026 결정 9의 "2026-10-25 추가"와
-- 증거 12, docs/runbook-hosting.md 6.8). 보관 표까지 지운다 - 그래야 범위를 바꿔 다시 복구할 수 있다.
--
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] -f <이 파일>
--
-- 기본은 리허설(ROLLBACK). 보관 표가 없으면 멈춘다(되돌릴 것이 없다).
--
-- 되돌리는 범위 - 본문을 보관 표에 복사하지 않았으므로(ADR 0023: 새 본문 사본 위치는 개정과 승인이 필요하다) 본문 없이
-- 원래대로 돌릴 수 있는 행만 돌린다:
--   - 원래 duplicate·dropped·empty였고 아직 다시 받지 않은 행(복구가 만든 '미시도' 그대로인 행): 원래 상태·해시·길이·시도
--     횟수·추출 시각으로 되돌린다. 이 상태들의 본문은 원래도 ''였다
--   - 원래 ok였던 행: 되돌리지 않는다. 본문(쓸모없는 특집 글)을 보관하지 않았다. '미시도'로 남아 다음 ingest가 원본
--     페이지에서 다시 받는다 - 본문 없는 'ok'를 만들면 본문 길이·해시와 어긋난다
--   - 이미 다시 받은 행(추출 상태가 NULL이 아니거나 시도 횟수가 0이 아님): 건드리지 않는다. 지금 값이 새 추출 결과다
-- 되돌린 뒤 행 수를 NOTICE로 알린다: restored(돌려놓음), left_unattempted_ok(원래 ok라 미시도로 둠), already_reextracted(건드리지 않음).

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
    IF to_regclass('news_raw_kmib_extract_fix') IS NULL THEN
        RAISE EXCEPTION '보관 표 news_raw_kmib_extract_fix가 없다 - 이 DB는 복구하지 않았거나 이미 되돌렸다';
    END IF;
END $$;

CREATE TEMP TABLE kmib_fix_state ON COMMIT DROP AS
SELECT f.raw_news_id, f.extract_status_before,
       (n.raw_news_extract_status IS NULL AND n.raw_news_extract_attempts = 0 AND n.raw_news_extracted_at IS NULL
        AND n.raw_news_content = '' AND n.raw_news_content_sha256 IS NULL AND n.raw_news_content_length IS NULL
        AND n.raw_news_content_purged_at IS NULL) AS still_unattempted
FROM news_raw_kmib_extract_fix f
JOIN news_raw n ON n.raw_news_id = f.raw_news_id;

UPDATE news_raw n
SET raw_news_extract_status = f.extract_status_before,
    raw_news_content_sha256 = f.content_sha256_before,
    raw_news_content_length = f.content_length_before,
    raw_news_extract_attempts = f.extract_attempts_before,
    raw_news_extracted_at = f.extracted_at_before
FROM news_raw_kmib_extract_fix f
JOIN kmib_fix_state s ON s.raw_news_id = f.raw_news_id
WHERE n.raw_news_id = f.raw_news_id
  AND s.still_unattempted
  AND f.extract_status_before IN ('duplicate', 'dropped', 'empty');

DO $$
DECLARE
    restored bigint;
    left_ok bigint;
    reextracted bigint;
    audited bigint;
    gone bigint;
BEGIN
    SELECT count(*) INTO audited FROM news_raw_kmib_extract_fix;
    SELECT count(*) FILTER (WHERE still_unattempted AND extract_status_before IN ('duplicate', 'dropped', 'empty')),
           count(*) FILTER (WHERE still_unattempted AND extract_status_before = 'ok'),
           count(*) FILTER (WHERE NOT still_unattempted)
      INTO restored, left_ok, reextracted
    FROM kmib_fix_state;
    gone := audited - restored - left_ok - reextracted;  -- 보관 표에는 있는데 news_raw에서 사라진 행
    RAISE NOTICE 'fix_kmib_revert restored=% left_unattempted_ok=% already_reextracted=% missing_from_news_raw=%',
        restored, left_ok, reextracted, gone;
END $$;

DROP TABLE news_raw_kmib_extract_fix;

\if :apply
COMMIT;
\echo 'fix_kmib_revert: COMMIT'
\else
ROLLBACK;
\echo 'fix_kmib_revert: ROLLBACK (리허설 - -v apply=1이면 커밋한다)'
\endif
