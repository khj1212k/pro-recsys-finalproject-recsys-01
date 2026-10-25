-- 국민일보 본문 추출 붕괴 복구: 붕괴 기간에 추출된 행을 '미시도'로 되돌려 다음 ingest가 다시 받게 한다
-- (ADR 0026 결정 9의 "2026-10-25 추가"와 증거 12, docs/runbook-hosting.md 6.8).
--
-- 배경: 국민일보는 2026-10-19 URL 형식과 페이지 템플릿을 바꿨고, 그 뒤 본문 추출기가 기사 본문 대신 페이지의 특집 카드
-- 묶음을 뽑았다. 그 글(또는 그 글과 본문이 섞인 2만 자 안팎의 글)이 'ok'로 저장됐고, 같은 특집 글을 뽑은 나머지 기사는
-- 본문 해시 중복 제거(ADR 0006)에 걸려 'duplicate'가 됐다. 고친 추출기(본문 컨테이너 선택)를 배포한 뒤 이 스크립트로
-- 붕괴 기간의 행을 되돌린다. 원본 페이지는 그대로 있으므로 다시 받으면 된다.
--
--   psql -X -v ON_ERROR_STOP=1 -v boundary='<붕괴 시작 UTC>' -v until='<고친 추출기의 첫 ingest 시작 UTC>' \
--        [-v apply=1] [-v press='국민일보'] [-v max_rows=3000] -f <이 파일>
--
--   boundary  이 시각 이후에 추출된 행부터 대상이다. 마지막으로 정상이던 추출(2026-10-19 03:06:10 UTC)과 붕괴 후 첫 추출
--             (05:06:54 UTC) 사이의 값이면 된다: '2026-10-19 04:00:00+00'. 시간대를 적지 않으면 UTC로 읽는다.
--   until     이 시각 전에 추출된 행까지 대상이다 = 고친 추출기를 배포한 뒤 처음 돈 ingest의 started_at(job_runs, UTC).
--             그 뒤에 추출된 행은 고친 추출기가 받은 것이라 건드리지 않는다.
--
-- 대상 행(조건을 모두 만족):
--   - 언론사가 press 변수의 값이고 추출 상태가 ok·duplicate·dropped·empty이다 (다운로드 실패·오류 행은 이미 재시도 대상이다)
--   - 추출 시각이 [boundary, until) 안이다 - 붕괴 기간의 추출은 모든 기사가 새 템플릿 페이지를 받았으므로, 'ok'의 본문이
--     특집 글 그대로가 아니라 본문과 섞인 것이어도 같은 원인이다. 'duplicate'만 되돌리면 이 ok 행 대부분이 남는다
--   - 본문을 비운 적이 없고(raw_news_content_purged_at IS NULL) 수집한 지 30일이 안 됐다(ADR 0023) - 본문 추출기의 대상
--     조회(extractor.py)와 같은 조건이라, 이 조건 밖의 행은 되돌려도 다시 받지 못한다. 그래서 건드리지 않는다
--   - duplicate이면 그 해시의 원본(ok)이 boundary 이전에 추출된 행이 아니다 - 붕괴 전 템플릿으로 받은 같은 글이 이미 있어 옛
--     추출기가 정확히 중복으로 잡은 행이다(구 URL과 신 URL로 이중 수집된 기사). 되돌려 다시 받으면 새 템플릿 추출이 옛 추출과
--     글자까지 같다는 보장이 없어 같은 기사가 ok 두 벌로 풀릴 수 있다. 특집 글 해시의 duplicate는 원본이 boundary 이후다
--
-- 되돌리는 값 = 수집기가 막 넣은 행(rss_collector.py)과 같다:
--   추출 상태 NULL, 본문 '', 본문 해시 NULL, 본문 길이 NULL, 추출 시각 NULL, 시도 횟수 0
-- 본문을 NULL이 아니라 ''로 두는 이유: NULL은 "보존 기한이 지나 비웠다"는 뜻이다(ADR 0023, ck_news_raw_purged_body_gone).
-- 본문이 남아 있으면 임베딩 단계가 상태와 상관없이 그 본문을 집으므로(pipeline/stages.py) 반드시 비워야 한다.
--
-- 원래 값은 보관 표 news_raw_kmib_extract_fix에 남긴다(상태·해시·길이·시도 횟수·추출 시각). 본문은 보관 표에 복사하지
-- 않는다 - 새 본문 사본 위치는 ADR 0023 개정과 승인이 있어야 하고, 이 본문은 쓸모없는 특집 글이다.
-- 되돌리기: scripts/oci/fix_kmib_body_extraction_revert.sql
--
-- 기본은 리허설이다: 끝이 ROLLBACK이라 아무것도 바뀌지 않는다. -v apply=1일 때만 COMMIT한다.
-- 한 트랜잭션이고, 아래 기준을 하나라도 어기면 예외로 멈춘다(ON_ERROR_STOP → psql 종료 코드 3, 아무것도 커밋되지 않음):
--   - 보관 표 news_raw_kmib_extract_fix가 이미 있다 = 이 DB는 이미 복구했다 (다시 하려면 되돌리기 스크립트로 보관 표를 먼저 치운다)
--   - 대상이 0행이다 = boundary·until·press가 틀렸다
--   - 대상이 max_rows를 넘는다 = 범위가 너무 넓다(기본 3000. 붕괴 엿새에 1,200행쯤이었다)
--   - 대상 중 임베딩이 있거나 뉴스레터에 묶인 행이 있다 = 이 스크립트가 다루지 않는 후속 산출물이다 (먼저 처리한다)
--   - until이 지금보다 뒤이거나, until 뒤에 고친 추출기로 돈 ingest가 없거나(job_runs의 stats.extract.body_container는 고친 추출기만
--     남기는 키다), until 뒤에 옛 추출기로 돈 ingest가 있다 = 고친 추출기를 아직 배포하지 않았거나 until이 첫 ingest보다 이르다.
--     옛 추출기가 도는 채로 되돌리면 다음 ingest가 되돌린 행을 같은 특집 글로 다시 채운다
-- 트랜잭션을 연 채로 기다리지 않는다(스크립트가 한 번에 끝난다). 대상 행의 락을 잡는 동안 같은 행을 쓰는 잡이 기다린다.

\set ON_ERROR_STOP on
\if :{?boundary}
\else
DO $$ BEGIN RAISE EXCEPTION '-v boundary=<붕괴 시작 UTC>가 필요하다 (런북 6.8 2단계)'; END $$;
\endif
\if :{?until}
\else
DO $$ BEGIN RAISE EXCEPTION '-v until=<고친 추출기의 첫 ingest 시작 UTC>가 필요하다 (런북 6.8 2단계)'; END $$;
\endif
\if :{?press}
\else
\set press '국민일보'
\endif
\if :{?max_rows}
\else
\set max_rows 3000
\endif
\if :{?apply}
\else
\set apply 0
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET LOCAL TIME ZONE 'UTC';
SELECT set_config('fix_kmib.max_rows', :'max_rows', true) AS max_rows \gset fix_kmib_
SELECT set_config('fix_kmib.until', :'until', true) AS until_value \gset fix_kmib_

-- 고친 추출기가 배포됐는지 확인한다(운영자의 순서 지키기에만 기대지 않는다). 고친 추출기가 도는 ingest만 stats.extract에
-- body_container 키를 남긴다(비어 있어도 있다 - crawler/content_extractor/extractor.py). extract 단계를 돌지 않은 실행은 세지 않는다.
DO $$
DECLARE
    until_at timestamptz := current_setting('fix_kmib.until')::timestamptz;
    fixed_runs bigint;
    old_runs bigint;
BEGIN
    IF until_at > now() THEN
        RAISE EXCEPTION 'until=%가 지금(%)보다 뒤다 - 고친 추출기의 첫 ingest started_at을 UTC로 적는다(런북 6.8 2단계)', until_at, now();
    END IF;
    SELECT count(*) FILTER (WHERE stats->'extract' ? 'body_container'),
           count(*) FILTER (WHERE NOT (stats->'extract' ? 'body_container'))
      INTO fixed_runs, old_runs
    FROM job_runs
    WHERE job = 'ingest' AND started_at >= until_at AND jsonb_typeof(stats->'extract') = 'object';
    IF fixed_runs = 0 THEN
        RAISE EXCEPTION 'until=% 뒤에 고친 추출기(stats.extract.body_container)로 돈 ingest가 없다 - 고친 추출기를 배포했는지, until이 그 첫 ingest의 started_at인지 확인한다(런북 6.8 1~2단계)', until_at;
    END IF;
    IF old_runs > 0 THEN
        RAISE EXCEPTION 'until=% 뒤에 옛 추출기로 돈 ingest가 %회 있다 - until이 고친 추출기의 첫 ingest보다 이르거나 옛 추출기로 되돌렸다. 옛 추출기가 도는 채로 되돌리면 다음 ingest가 같은 특집 글로 다시 채운다', until_at, old_runs;
    END IF;
END $$;

CREATE TABLE news_raw_kmib_extract_fix (
    raw_news_id integer PRIMARY KEY,
    press_name varchar NOT NULL,
    extract_status_before varchar NOT NULL,
    content_sha256_before varchar,
    content_length_before integer,
    extract_attempts_before smallint NOT NULL,
    extracted_at_before timestamptz NOT NULL,
    crawled_at timestamp NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
    fixed_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE news_raw_kmib_extract_fix IS
    '국민일보 본문 추출 붕괴 복구(ADR 0026 결정 9, 2026-10-25)가 미시도로 되돌린 행의 원래 메타데이터. 본문은 담지 않는다(ADR 0023). 되돌리기용이자 "이 DB는 복구했다"는 표시 - 되돌리기 스크립트(fix_kmib_body_extraction_revert.sql)만 지운다.';

INSERT INTO news_raw_kmib_extract_fix (
    raw_news_id, press_name, extract_status_before, content_sha256_before, content_length_before,
    extract_attempts_before, extracted_at_before, crawled_at, window_start, window_end)
SELECT n.raw_news_id, p.press_name, n.raw_news_extract_status, n.raw_news_content_sha256, n.raw_news_content_length,
       n.raw_news_extract_attempts, n.raw_news_extracted_at, n.raw_news_crawled_at,
       :'boundary'::timestamptz, :'until'::timestamptz
FROM news_raw n
JOIN press p ON p.press_id = n.press_id
WHERE p.press_name = :'press'
  AND n.raw_news_extract_status IN ('ok', 'duplicate', 'dropped', 'empty')
  AND n.raw_news_extracted_at >= :'boundary'::timestamptz
  AND n.raw_news_extracted_at < :'until'::timestamptz
  AND n.raw_news_content_purged_at IS NULL
  AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '30 days'
  AND NOT (n.raw_news_extract_status = 'duplicate' AND EXISTS (
        SELECT 1 FROM news_raw o
        WHERE o.raw_news_extract_status = 'ok'
          AND o.raw_news_content_sha256 = n.raw_news_content_sha256
          AND o.raw_news_extracted_at < :'boundary'::timestamptz))
ORDER BY n.raw_news_id
FOR UPDATE OF n;

DO $$
DECLARE
    total bigint;
    cap bigint := current_setting('fix_kmib.max_rows')::bigint;
    linked bigint;
BEGIN
    SELECT count(*) INTO total FROM news_raw_kmib_extract_fix;
    IF total = 0 THEN
        RAISE EXCEPTION '복구 대상이 0행이다 - boundary·until·press를 확인한다(런북 6.8 2단계)';
    END IF;
    IF total > cap THEN
        RAISE EXCEPTION '복구 대상이 %행이라 max_rows=%를 넘는다 - 범위(boundary·until)를 확인한다', total, cap;
    END IF;
    SELECT count(*) INTO linked
    FROM news_raw_kmib_extract_fix f JOIN news_raw n ON n.raw_news_id = f.raw_news_id
    WHERE n.embedding_result IS NOT NULL OR n.news_letter_id IS NOT NULL;
    IF linked > 0 THEN
        RAISE EXCEPTION '대상 %행에 임베딩이 있거나 뉴스레터에 묶여 있다 - 이 스크립트는 후속 산출물을 지우지 않는다. 먼저 그 행을 처리한다', linked;
    END IF;
END $$;

UPDATE news_raw n
SET raw_news_extract_status = NULL,
    raw_news_content = '',
    raw_news_content_sha256 = NULL,
    raw_news_content_length = NULL,
    raw_news_extracted_at = NULL,
    raw_news_extract_attempts = 0
FROM news_raw_kmib_extract_fix f
WHERE n.raw_news_id = f.raw_news_id;

DO $$
DECLARE
    total bigint;
    reset bigint;
BEGIN
    SELECT count(*) INTO total FROM news_raw_kmib_extract_fix;
    SELECT count(*) INTO reset
    FROM news_raw_kmib_extract_fix f JOIN news_raw n ON n.raw_news_id = f.raw_news_id
    WHERE n.raw_news_extract_status IS NULL AND n.raw_news_content = '' AND n.raw_news_content_sha256 IS NULL
      AND n.raw_news_content_length IS NULL AND n.raw_news_extracted_at IS NULL AND n.raw_news_extract_attempts = 0
      AND n.raw_news_content_purged_at IS NULL
      -- 본문 추출기의 대상 조회(extractor.py)가 이 행을 다시 고른다
      AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '30 days';
    IF reset <> total THEN
        RAISE EXCEPTION '보관 표 %행 중 %행만 미시도가 됐다', total, reset;
    END IF;
    RAISE NOTICE 'fix_kmib reset=% ok=% duplicate=% dropped=% empty=%', total,
        (SELECT count(*) FROM news_raw_kmib_extract_fix WHERE extract_status_before = 'ok'),
        (SELECT count(*) FROM news_raw_kmib_extract_fix WHERE extract_status_before = 'duplicate'),
        (SELECT count(*) FROM news_raw_kmib_extract_fix WHERE extract_status_before = 'dropped'),
        (SELECT count(*) FROM news_raw_kmib_extract_fix WHERE extract_status_before = 'empty');
END $$;

-- 운영자가 보는 요약: 이전 상태별 행 수, 서로 다른 본문 해시 수, 수집 시각 범위
SELECT f.extract_status_before AS before_status,
       count(*) AS rows,
       count(DISTINCT f.content_sha256_before) AS distinct_hashes,
       min(f.crawled_at) AS first_crawled_utc,
       max(f.crawled_at) AS last_crawled_utc
FROM news_raw_kmib_extract_fix f
GROUP BY f.extract_status_before
ORDER BY f.extract_status_before;

\if :apply
COMMIT;
\echo 'fix_kmib: COMMIT'
\else
ROLLBACK;
\echo 'fix_kmib: ROLLBACK (리허설 - -v apply=1이면 커밋한다)'
\endif
