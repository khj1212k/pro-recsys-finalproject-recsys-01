-- 매일경제 본문이 잘못 저장된 행 재추출: 해당 ok 행(과 그 짝 duplicate 행)을 '미시도'로 되돌려 다음 ingest가 다시 받게 한다
-- (ADR 0026 결정 9의 "2026-10-25 추가 2"와 그 측정표, docs/runbook-hosting.md 6.12).
--
-- 배경: 옛 추출기는 매일경제 기사 페이지 전체를 trafilatura에 넘겼고, 본문이 아닌 글이 본문으로 저장된 행이 두 가지 있다
-- (고친 추출기 crawler/content_extractor/body_container.py는 본문 노드 itemprop="articleBody"만 넘긴다).
--   (1) 해설 모달 글이 섞인 행: 모달(div.modal_ai_explain)은 본문 노드 밖에 있고 글이 본문보다 몇 배 길다. 본문 1,000자 안팎이
--       3,500자 이상으로 부푼다. 저장 길이로 가린다 -> target=length.
--   (2) 내비게이션·인기 기사 목록 글이 본문이 된 행: 본문이 아주 짧은 기사(사진 기사 등)는 전체 페이지에서 본문 대신 그 목록 글(1,500자
--       안팎)이 뽑힌다. 이 글은 하루 단위로 기사마다 같아, 첫 기사는 ok로 남고 나머지는 같은 해시의 duplicate가 된다. 길이로는
--       평범한 기사와 가려지지 않고 같은 해시를 나눠 가진 묶음으로 가린다 -> target=shared.
-- 원본 페이지는 그대로 있으므로 다시 받으면 된다.
--
--   psql -X -v ON_ERROR_STOP=1 -v until='<고친 추출기의 첫 ingest 시작 UTC>' \
--        [-v target=length|shared] [-v min_length=2500] [-v batch_limit=500] [-v apply=1] -f <이 파일>
--
--   until        이 시각 전에 추출된 행까지 대상이다 = 고친 추출기를 배포한 뒤 매일경제 기사를 처음 처리한 ingest의 started_at
--                (job_runs, UTC). 그 뒤에 추출된 행은 고친 추출기가 받은 것이라 건드리지 않는다. 시간대를 적지 않으면 UTC로 읽는다.
--   target       length(기본) = 저장 길이가 min_length 이상인 ok 행. 같은 해시를 나눠 가진 duplicate 행이 있는 ok 행은 빼고 shared에 맡긴다.
--                shared = 같은 해시를 나눠 가진 묶음: 같은 언론사의 duplicate 행이 있는 ok 행(길이 무관)과 그 짝 ok 행이 있는 duplicate 행.
--                묶음은 한 배치로 되돌려야 하므로(ok 행만 먼저 되돌리면 duplicate 행이 짝을 잃는다) 대상이 batch_limit을 넘으면 거부한다.
--                두 target은 서로 겹치지 않는다. 둘 다 돌려야 하고 순서는 상관없다.
--   min_length   target=length에서만 쓴다. 저장된 본문 길이(raw_news_content_length)가 이 값 이상인 행이 대상이다. 기본 2500, 2500 미만은
--                거부한다. 근거(ADR 0026의 측정표): 2026-10-25 30일 창 매일경제 ok 7,706행에서 표본 60건을 다시 받아 컨테이너 길이와 견줬다.
--                3,500자 이상은 16/16이 부풀었고, 3,000~3,499자는 16건 중 9건, 2,500~2,999자는 12건 중 1건(95% 구간 0~38%)이 부풀었다.
--                부풀지 않은 행을 다시 받아도 본문은 같고(저장 길이와 컨테이너 길이가 같았다. 다운로드에 실패하면 그 본문을 잃지만 표본 60/60은
--                성공했다) 부푼 행을 놓치면 틀린 본문이 남으므로, 측정한 가장 낮은 구간(2,500)까지 내려갔다. 2,500 미만은 표본 16건에서
--                모달이 섞인 행이 0건이었다(95% 상한 21%).
--   batch_limit  한 번에 되돌릴 최대 행 수. 기본 500, 1000 초과는 거부한다. 매일경제 서버에 한꺼번에 수천 건을 다시 요청하지 않으려는
--                값이다(추출기는 미시도 행을 모두 한 회차에 받는다). 오래된 행(raw_news_id가 작은 행)부터 되돌린다 - 보존 기한(30일)이
--                먼저 닿는 행이다. 3,162행이면 7회로 나눠 돌린다: 한 회차의 ingest가 앞 배치를 모두 받은 뒤에 다음 배치를 돌린다.
--
-- 대상 행(조건을 모두 만족):
--   - 언론사가 매일경제이고 추출 시각이 until보다 앞이다
--   - target=length: 추출 상태 ok, 저장 길이 min_length 이상, 같은 해시의 duplicate 행(추출 시각이 until 앞)이 없다
--     target=shared:  (추출 상태 ok이고 같은 해시의 duplicate 행이 있다) 또는 (추출 상태 duplicate이고 같은 해시의 ok 행이 있다)
--   - 본문을 비운 적이 없고(raw_news_content_purged_at IS NULL) 수집한 지 30일이 안 됐다(ADR 0023) - 본문 추출기의 대상
--     조회(extractor.py)와 같은 조건이라, 이 조건 밖의 행은 되돌려도 다시 받지 못한다. 그래서 건드리지 않는다.
--     만료 직전 행은 3시간 여유를 둬서 뺀다(수집한 지 30일 - 3시간이 안 된 행만): 다음 정시 ingest 전에 30일이 지나는 행을 되돌리면
--     다시 받히지 못한 채 만료돼 본문 길이·해시까지 NULL로 남는다(보존 잡은 추출 대기 중인 빈 행을 건드리지 않는다). 그런 행은
--     부푼 채로 두면 보존 잡이 본문을 비우고 길이·해시는 남긴다
--   - 이 스크립트가 이미 보관한 행이 아니다 - 같은 행을 두 번 보관하지 않는다(재실행해도 안전하다)
--   - 짝이 없는 duplicate·dropped·empty 행은 대상이 아니다: 본문이 원래 비어 있고 어떤 ok 행의 글도 대신 받지 않았다
--
-- 되돌리는 값 = 수집기가 막 넣은 행(rss_collector.py)과 같다:
--   추출 상태 NULL, 본문 '', 본문 해시 NULL, 본문 길이 NULL, 추출 시각 NULL, 시도 횟수 0
-- 본문을 NULL이 아니라 ''로 두는 이유: NULL은 "보존 기한이 지나 비웠다"는 뜻이다(ADR 0023, ck_news_raw_purged_body_gone).
-- 본문이 남아 있으면 임베딩 단계가 상태와 상관없이 그 본문을 집으므로(pipeline/stages.py) 반드시 비워야 한다.
--
-- 원래 값은 보관 표 news_raw_mk_extract_fix에 남긴다(해시·길이·시도 횟수·추출 시각·배치 번호·target). 본문은 보관 표에 복사하지
-- 않는다 - 새 본문 사본 위치는 ADR 0023 개정과 승인이 있어야 한다. 그래서 되돌려도 옛 본문은 돌아오지 않는다. 호스트 밖 본문
-- 백업(6.5의 full/)은 최근 27일 수집분만 담고 하루 뒤 지워지므로, 시작 직전에 6.5로 새로 뜬 백업에서만 옛 본문을 복원할 수 있고
-- 수집한 지 27일이 넘은 대상(오래된 행부터 되돌리므로 첫 배치들 - 2026-10-25 조회 281행)은 어떤 본문 백업에도 없다: 다시 받지 못하면
-- 복구할 수 없다. 부푼 본문은 틀린 값이라 잃어도 되고, 부풀지 않은 행의 본문은 보존 기한이 3일 안에 지운다.
-- 보관 표는 배치를 거듭해도 남는다(끝 배치 뒤 확인용). 정리(보관 표만 지운다, news_raw는 되돌리지 않는다):
-- scripts/oci/fix_mk_body_extraction_revert.sql
--
-- 기본은 리허설이다: 끝이 ROLLBACK이라 아무것도 바뀌지 않는다. -v apply=1일 때만 COMMIT한다.
-- 한 트랜잭션이고, 아래 기준을 하나라도 어기면 예외로 멈춘다(ON_ERROR_STOP → psql 종료 코드 3, 아무것도 커밋되지 않음):
--   - target이 length·shared가 아니거나, min_length가 2500 미만이거나 batch_limit이 1~1000 밖이다
--   - until이 지금보다 뒤이거나, until 뒤에 고친 추출기로 돈 ingest가 없거나(job_runs의 stats.extract.body_container에 매일경제 키가
--     있는 실행 = 고친 추출기가 매일경제 기사를 처리한 실행), until 뒤에 옛 추출기가 매일경제 기사를 처리한 ingest가 있거나, 고친
--     추출기가 본문 노드를 못 찾은 기사(container_missing)가 10%를 넘는다 = 고친 추출기를 아직 배포하지 않았거나 새 페이지에 맞지
--     않는다. 이 상태로 되돌리면 다음 ingest가 되돌린 행을 같은 글로 다시 채운다
--   - ingest가 지금 돌고 있다(job_runs의 'running') = 끝난 뒤에 돌린다
--   - 앞 배치가 아직 다 받히지 않았다(보관한 행 가운데 미시도가 남아 있다. target 가리지 않는다) = 한 회차에 수천 건이 몰리지 않게 다음
--     ingest가 끝난 뒤에 돌린다
--   - 대상이 0행이다 = 모든 배치를 마쳤거나 until·min_length가 틀렸다
--   - target=shared의 대상이 batch_limit을 넘는다 = 묶음을 쪼갤 수 없으니 batch_limit을 올린다(최대 1000)
--   - 대상 중 임베딩이 있거나 뉴스레터에 묶인 행이 있다 = 이 스크립트가 다루지 않는 후속 산출물이다 (먼저 처리한다)
-- 트랜잭션을 연 채로 기다리지 않는다(스크립트가 한 번에 끝난다). 대상 행의 락을 잡는 동안 같은 행을 쓰는 잡이 기다린다.

\set ON_ERROR_STOP on
\if :{?until}
\else
DO $$ BEGIN RAISE EXCEPTION '-v until=<고친 추출기의 첫 ingest 시작 UTC>가 필요하다 (런북 6.12 2단계)'; END $$;
\endif
\if :{?target}
\else
\set target length
\endif
\if :{?min_length}
\else
\set min_length 2500
\endif
\if :{?batch_limit}
\else
\set batch_limit 500
\endif
\if :{?apply}
\else
\set apply 0
\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';
SET LOCAL TIME ZONE 'UTC';
SELECT set_config('fix_mk.until', :'until', true) AS until_value \gset fix_mk_
SELECT set_config('fix_mk.target', :'target', true) AS target_value \gset fix_mk_
SELECT set_config('fix_mk.min_length', :'min_length', true) AS min_length_value \gset fix_mk_
SELECT set_config('fix_mk.batch_limit', :'batch_limit', true) AS batch_limit_value \gset fix_mk_

DO $$
DECLARE
    min_len integer := current_setting('fix_mk.min_length')::integer;
    cap integer := current_setting('fix_mk.batch_limit')::integer;
BEGIN
    IF current_setting('fix_mk.target') NOT IN ('length', 'shared') THEN
        RAISE EXCEPTION 'target=%는 length 또는 shared여야 한다 (런북 6.12)', current_setting('fix_mk.target');
    END IF;
    IF min_len < 2500 THEN
        RAISE EXCEPTION 'min_length=%는 측정한 범위(2500 이상, ADR 0026 측정표)보다 낮다 - 부풀지 않은 기사를 모두 다시 받게 된다', min_len;
    END IF;
    IF cap < 1 OR cap > 1000 THEN
        RAISE EXCEPTION 'batch_limit=%는 1~1000 밖이다 - 매일경제 서버에 한꺼번에 많은 요청을 보내지 않는다. 나눠 돌린다(런북 6.12)', cap;
    END IF;
END $$;

-- 보관 표는 배치를 거듭해도 남는다: 이미 있으면 그대로 쓰고(앞 배치의 행을 다시 보관하지 않는다), 없으면 만든다.
CREATE TABLE IF NOT EXISTS news_raw_mk_extract_fix (
    raw_news_id integer PRIMARY KEY,
    press_name varchar NOT NULL,
    extract_status_before varchar NOT NULL,
    content_sha256_before varchar,
    content_length_before integer NOT NULL,
    extract_attempts_before smallint NOT NULL,
    extracted_at_before timestamptz NOT NULL,
    crawled_at timestamp NOT NULL,
    min_length integer NOT NULL,
    until_at timestamptz NOT NULL,
    batch_no integer NOT NULL,
    target_kind varchar NOT NULL CHECK (target_kind IN ('length', 'shared')),
    fixed_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE news_raw_mk_extract_fix IS
    '매일경제 본문 재추출(ADR 0026 결정 9, 2026-10-25)이 미시도로 되돌린 행의 원래 메타데이터. 본문은 담지 않는다(ADR 0023). 같은 행을 두 번 보관하지 않는 장치이자 앞 배치가 다 받혔는지 보는 기준 - 되돌리기 스크립트(fix_mk_body_extraction_revert.sql)만 지운다.';

-- 고친 추출기가 배포됐는지 확인한다(운영자의 순서 지키기에만 기대지 않는다). 고친 추출기가 매일경제 기사를 처리한 ingest만
-- stats.extract.body_container에 매일경제 키를 남긴다(crawler/content_extractor/body_container.py의 규칙이 있는 언론사만 센다).
-- 옛 추출기가 매일경제 기사를 처리했으면(본문을 받은 기사 = ok·dropped·empty·duplicate가 있으면) 그 키가 없다. 다운로드에 실패한
-- 기사만 있던 실행은 어느 추출기인지 알 수 없어 세지 않는다. extract 단계를 돌지 않은 실행도 세지 않는다.
DO $$
DECLARE
    until_at timestamptz := current_setting('fix_mk.until')::timestamptz;
    fixed_runs bigint;
    old_runs bigint;
    n_container bigint;
    n_missing bigint;
BEGIN
    IF until_at > now() THEN
        RAISE EXCEPTION 'until=%가 지금(%)보다 뒤다 - 고친 추출기가 매일경제 기사를 처음 처리한 ingest의 started_at을 UTC로 적는다(런북 6.12 2단계)', until_at, now();
    END IF;
    SELECT count(*) FILTER (WHERE r.bc ? '매일경제'),
           count(*) FILTER (WHERE NOT (r.bc ? '매일경제') AND r.parsed > 0),
           coalesce(sum(coalesce((r.bc->'매일경제'->>'container')::bigint, 0)), 0),
           coalesce(sum(coalesce((r.bc->'매일경제'->>'container_missing')::bigint, 0)), 0)
      INTO fixed_runs, old_runs, n_container, n_missing
    FROM (
        SELECT coalesce(stats->'extract'->'body_container', '{}'::jsonb) AS bc,
               coalesce((stats->'extract'->'per_press'->'매일경제'->>'ok')::bigint, 0)
             + coalesce((stats->'extract'->'per_press'->'매일경제'->>'dropped')::bigint, 0)
             + coalesce((stats->'extract'->'per_press'->'매일경제'->>'empty')::bigint, 0)
             + coalesce((stats->'extract'->'per_press'->'매일경제'->>'duplicate')::bigint, 0) AS parsed
        FROM job_runs
        WHERE job = 'ingest' AND started_at >= until_at AND jsonb_typeof(stats->'extract') = 'object'
    ) r;
    IF fixed_runs = 0 THEN
        RAISE EXCEPTION 'until=% 뒤에 고친 추출기(stats.extract.body_container의 매일경제 키)로 돈 ingest가 없다 - 고친 추출기를 배포했는지, until이 매일경제 기사를 처음 처리한 ingest의 started_at인지 확인한다. 새 매일경제 기사가 쌓인 뒤에 본다(런북 6.12 1~2단계)', until_at;
    END IF;
    IF old_runs > 0 THEN
        RAISE EXCEPTION 'until=% 뒤에 옛 추출기로 매일경제 기사를 처리한 ingest가 %회 있다 - until이 고친 추출기의 첫 ingest보다 이르거나 옛 추출기로 되돌렸다. 옛 추출기가 도는 채로 되돌리면 다음 ingest가 같은 모달 글로 다시 채운다', until_at, old_runs;
    END IF;
    IF n_missing * 10 > n_container + n_missing THEN
        RAISE EXCEPTION 'until=% 뒤 매일경제 기사 %건 중 %건에서 본문 노드를 못 찾았다(container_missing) - 고친 추출기가 새 페이지에 맞지 않는다. 페이지 개편을 확인한다(런북 6.12 2단계)', until_at, n_container + n_missing, n_missing;
    END IF;
END $$;

-- ingest가 지금 돌고 있으면 멈춘다. 돌고 있는 회차가 이 배치의 행과 앞 배치의 행을 한 번에 받지 않게, 끝난 뒤에 돌린다.
-- 프로세스가 죽어 'running'으로 남은 행은 다음 ingest 시작 때 abandoned가 된다(jobs/store.py).
DO $$
DECLARE
    running bigint;
    pending bigint;
BEGIN
    SELECT count(*) INTO running FROM job_runs WHERE job = 'ingest' AND status = 'running';
    IF running > 0 THEN
        RAISE EXCEPTION 'ingest가 지금 돌고 있다(job_runs ''running'' %건) - 끝난 뒤에 다시 돌린다(런북 6.12)', running;
    END IF;
    -- 앞 배치에서 보관한 행 가운데 아직 다시 받지 않은 행 = 추출기가 다음 회차에 모두 받을 행. 이 위에 한 배치를 더 쌓으면
    -- 한 회차의 요청이 batch_limit을 넘는다.
    SELECT count(*) INTO pending
    FROM news_raw_mk_extract_fix f JOIN news_raw n ON n.raw_news_id = f.raw_news_id
    WHERE n.raw_news_extract_status IS NULL AND n.raw_news_extract_attempts = 0
      AND n.raw_news_content_purged_at IS NULL
      AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '30 days';
    IF pending > 0 THEN
        RAISE EXCEPTION '앞 배치의 행 %건이 아직 미시도다 - 다음 ingest가 끝나 다시 받은 뒤에 돌린다(한 회차에 batch_limit을 넘는 요청이 몰리지 않게)', pending;
    END IF;
END $$;

-- 이번 실행의 대상 후보(보관한 행·보존 기한 밖 행은 이미 뺐다). 조건은 여기 한 곳이다 - 개수 확인·보관·남은 수가 모두 이 표를 쓴다.
-- 짝 = 같은 언론사에서 같은 본문 해시를 나눠 가진 행: URL만 다른 같은 글이거나 모든 기사에 같은 목록 글이 뽑힌 경우다.
-- 이 표는 읽기만 하고 news_raw를 바꾸지 않는다(트랜잭션이 끝나면 사라진다).
CREATE TEMP TABLE fix_mk_candidates ON COMMIT DROP AS
SELECT n.raw_news_id
FROM news_raw n
JOIN press p ON p.press_id = n.press_id
WHERE p.press_name = '매일경제'
  AND n.raw_news_extracted_at < :'until'::timestamptz
  AND n.raw_news_content_purged_at IS NULL
  -- 추출기의 대상 조회(extractor.py)와 같은 30일에 3시간 여유를 더했다: 다음 정시 ingest 전에 만료되는 행은 되돌리지 않는다
  AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '30 days' + interval '3 hours'
  AND NOT EXISTS (SELECT 1 FROM news_raw_mk_extract_fix f WHERE f.raw_news_id = n.raw_news_id)
  AND CASE :'target'
        WHEN 'length' THEN
            n.raw_news_extract_status = 'ok'
            AND n.raw_news_content_length >= :'min_length'::integer
            AND NOT EXISTS (SELECT 1 FROM news_raw d
                            WHERE d.press_id = n.press_id AND d.raw_news_extract_status = 'duplicate'
                              AND d.raw_news_content_sha256 = n.raw_news_content_sha256
                              AND d.raw_news_extracted_at < :'until'::timestamptz)
        ELSE
            (n.raw_news_extract_status = 'ok'
             AND EXISTS (SELECT 1 FROM news_raw d
                         WHERE d.press_id = n.press_id AND d.raw_news_extract_status = 'duplicate'
                           AND d.raw_news_content_sha256 = n.raw_news_content_sha256
                           AND d.raw_news_extracted_at < :'until'::timestamptz))
            OR (n.raw_news_extract_status = 'duplicate'
                AND EXISTS (SELECT 1 FROM news_raw o
                            WHERE o.press_id = n.press_id AND o.raw_news_extract_status = 'ok'
                              AND o.raw_news_content_sha256 = n.raw_news_content_sha256
                              AND o.raw_news_extracted_at < :'until'::timestamptz))
      END;

DO $$
DECLARE
    total bigint;
    cap integer := current_setting('fix_mk.batch_limit')::integer;
BEGIN
    SELECT count(*) INTO total FROM fix_mk_candidates;
    IF total = 0 THEN
        RAISE EXCEPTION '재추출 대상이 0행이다 - 모든 배치를 마쳤거나 until·min_length·target이 틀렸다(런북 6.12 3단계)';
    END IF;
    -- 같은 해시의 묶음은 한 배치로 되돌린다: ok 행만 먼저 되돌리면 그 duplicate 행들은 짝(해시)을 잃어 이 스크립트가 다시 찾지 못한다
    IF current_setting('fix_mk.target') = 'shared' AND total > cap THEN
        RAISE EXCEPTION 'target=shared 대상 %행이 batch_limit=%를 넘는다 - 같은 해시의 묶음은 한 배치로 되돌려야 하므로 batch_limit을 올린다(최대 1000, 그 위면 멈추고 확인한다)', total, cap;
    END IF;
END $$;

SELECT set_config('fix_mk.batch_no', (SELECT coalesce(max(batch_no), 0) + 1 FROM news_raw_mk_extract_fix)::text, true) AS batch_no_value \gset fix_mk_

INSERT INTO news_raw_mk_extract_fix (
    raw_news_id, press_name, extract_status_before, content_sha256_before, content_length_before,
    extract_attempts_before, extracted_at_before, crawled_at, min_length, until_at, batch_no, target_kind)
SELECT n.raw_news_id, p.press_name, n.raw_news_extract_status, n.raw_news_content_sha256, n.raw_news_content_length,
       n.raw_news_extract_attempts, n.raw_news_extracted_at, n.raw_news_crawled_at,
       :'min_length'::integer, :'until'::timestamptz, current_setting('fix_mk.batch_no')::integer, :'target'
FROM news_raw n
JOIN fix_mk_candidates c ON c.raw_news_id = n.raw_news_id
JOIN press p ON p.press_id = n.press_id
ORDER BY n.raw_news_id
LIMIT :'batch_limit'::integer
FOR UPDATE OF n;

DO $$
DECLARE
    linked bigint;
BEGIN
    SELECT count(*) INTO linked
    FROM news_raw_mk_extract_fix f JOIN news_raw n ON n.raw_news_id = f.raw_news_id
    WHERE f.batch_no = current_setting('fix_mk.batch_no')::integer
      AND (n.embedding_result IS NOT NULL OR n.news_letter_id IS NOT NULL);
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
FROM news_raw_mk_extract_fix f
WHERE n.raw_news_id = f.raw_news_id
  AND f.batch_no = current_setting('fix_mk.batch_no')::integer;

DO $$
DECLARE
    total bigint;
    reset bigint;
    remaining bigint;
BEGIN
    SELECT count(*) INTO total FROM news_raw_mk_extract_fix WHERE batch_no = current_setting('fix_mk.batch_no')::integer;
    SELECT count(*) INTO reset
    FROM news_raw_mk_extract_fix f JOIN news_raw n ON n.raw_news_id = f.raw_news_id
    WHERE f.batch_no = current_setting('fix_mk.batch_no')::integer
      AND n.raw_news_extract_status IS NULL AND n.raw_news_content = '' AND n.raw_news_content_sha256 IS NULL
      AND n.raw_news_content_length IS NULL AND n.raw_news_extracted_at IS NULL AND n.raw_news_extract_attempts = 0
      AND n.raw_news_content_purged_at IS NULL
      -- 본문 추출기의 대상 조회(extractor.py)가 이 행을 다시 고른다
      AND n.raw_news_crawled_at > (now() AT TIME ZONE 'UTC') - interval '30 days';
    IF reset <> total THEN
        RAISE EXCEPTION '이번 배치 보관 %행 중 %행만 미시도가 됐다', total, reset;
    END IF;
    -- 이 배치 뒤에도 남는 대상(후보 가운데 보관하지 않은 행): 다음 배치 수를 가늠한다. target=shared는 한 배치로 끝나 항상 0이다
    SELECT count(*) INTO remaining
    FROM fix_mk_candidates c
    WHERE NOT EXISTS (SELECT 1 FROM news_raw_mk_extract_fix f WHERE f.raw_news_id = c.raw_news_id);
    RAISE NOTICE 'fix_mk batch=% reset=% remaining=% archived_total=% target=%', current_setting('fix_mk.batch_no')::integer, total,
        remaining, (SELECT count(*) FROM news_raw_mk_extract_fix), current_setting('fix_mk.target');
END $$;

-- 운영자가 보는 요약: 이번 배치의 행 수, 이전 본문 길이 분포, 수집 시각 범위
SELECT f.batch_no,
       max(f.target_kind) AS target,
       count(*) AS rows,
       min(f.content_length_before) AS min_len_before,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY f.content_length_before)::int AS median_len_before,
       max(f.content_length_before) AS max_len_before,
       min(f.crawled_at) AS first_crawled_utc,
       max(f.crawled_at) AS last_crawled_utc
FROM news_raw_mk_extract_fix f
WHERE f.batch_no = current_setting('fix_mk.batch_no')::integer
GROUP BY f.batch_no;

\if :apply
COMMIT;
\echo 'fix_mk: COMMIT'
\else
ROLLBACK;
\echo 'fix_mk: ROLLBACK (리허설 - -v apply=1이면 커밋한다)'
\endif
