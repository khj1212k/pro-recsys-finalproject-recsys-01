-- Tier 0(E2.1.Micro) news_raw를 Mac 덤프를 복원한 DB(Tier 1 A1)에 URL 기준으로 병합한다 (ADR 0026 결정 4, 런북 6.4).
--
-- 두 DB는 독립적으로 채워져 raw_news_id가 서로 다르다. 그래서 id가 아니라 raw_news_url(UNIQUE)로 합치고,
-- 언론사는 press_id가 아니라 press_name으로 찾는다. 규칙:
--   * 같은 URL이 이미 있으면 대상 쪽 행을 그대로 둔다(ON CONFLICT DO NOTHING) - Mac 행에는 임베딩이 있다.
--   * 새 URL인데 정제 본문 해시가 대상의 'ok' 행과 같으면 'duplicate'로 넣고 본문을 비운다
--     (부분 UNIQUE uq_news_raw_content_sha256_ok, Alembic d48994e9d26e와 같은 규칙).
--     원본 행이 'rss_summary'(동아일보 RSS 요약, ADR 0026 2026-10-25)면 대상의 'ok'뿐 아니라 'rss_summary' 행과 해시가 같을 때도
--     'duplicate'다 - 수집기(crawler/rss_collector.py)가 같은 요약을 두 번 넣지 않는 규칙을 이 경로에서도 지킨다. 'rss_summary'
--     쪽은 UNIQUE가 없어(부분 인덱스 ix_news_raw_content_sha256_rss_summary는 유일하지 않다) 이 규칙이 유일한 방어다.
--     'blocked_origin' 행(본문 없이 닫은 행)은 해시가 없어 그대로 들어간다.
--   * 병합된 행의 embedding_result는 NULL이다 - 대상 호스트의 embed 잡이 채운다.
--   * 보존 기한(ADR 0023)이 지나 원본에서 본문을 비운 행(본문 NULL, raw_news_content_purged_at 있음)은 본문 없이
--     비운 시각·길이·해시와 함께 들어간다 - 본문이 되살아나지 않는다. 원본에서 아직 비우지 않은 만료 본문은
--     대상의 retention 잡이 비운다(런북 6.4의 6단계에서 바로 돌린다).
--   * 기대 행 수(병합 전 + 대상에 없는 URL 수)와 결과가 다르거나 매핑되지 않는 언론사가 있으면 전체를 되돌린다.
--   * 대상 press 테이블에 같은 이름이 둘 이상이면(press_name에는 UNIQUE가 없다) 중단한다 - 이름 JOIN이 URL마다
--     후보를 두 개 만들고 ON CONFLICT DO NOTHING이 그중 하나를 조용히 고르면, 행 수 검증은 통과하는데
--     press_id가 임의로 정해지기 때문이다.
--
-- 입력: Tier 0에서 뽑은 CSV (열 순서 고정, 헤더 포함). 원본은 Alembic f1db3221e45f 이상, **대상은 b7e4a1c93d52 이상**이어야 한다 -
--   동아일보 요약 배포(런북 6.9) 뒤의 원본에는 상태 'rss_summary'·'blocked_origin' 행이 있고, 대상 제약이 두 상태를 모르면
--   CheckViolation으로 병합 전체가 되돌아간다. 대상은 복원한 뒤 migrate로 head까지 올린다(런북 6.4의 3단계).
--   psql -d newsletter -c "COPY (SELECT p.press_name, n.raw_news_title, n.raw_news_content, n.raw_news_url,
--       n.raw_news_created_at, n.raw_news_crawled_at, n.raw_news_extract_status, n.raw_news_extracted_at,
--       n.raw_news_extract_attempts, n.raw_news_content_sha256, n.raw_news_content_length,
--       n.raw_news_content_purged_at, n.raw_news_kogl_type
--     FROM news_raw n JOIN press p USING (press_id) ORDER BY n.raw_news_id) TO STDOUT WITH (FORMAT csv, HEADER)"
--
-- 실행: 대상 DB에서, 스케줄러를 멈춘 상태로(동시 쓰기 없음). CSV는 psql의 표준 입력으로 준다.
--   psql -v ON_ERROR_STOP=1 -1 -d newsletter -f merge_tier0_news_raw.sql < tier0_news_raw.csv
-- -1(단일 트랜잭션)과 ON_ERROR_STOP이 있어야 검증 실패 시 아무것도 남지 않는다.

CREATE TEMP TABLE tier0_news_raw (
    press_name                text        NOT NULL,
    raw_news_title            text        NOT NULL,
    raw_news_content          text,
    raw_news_url              text        NOT NULL,
    raw_news_created_at       timestamptz,
    raw_news_crawled_at       timestamp   NOT NULL,
    raw_news_extract_status   varchar(16),
    raw_news_extracted_at     timestamptz,
    raw_news_extract_attempts smallint    NOT NULL,
    raw_news_content_sha256   varchar(64),
    raw_news_content_length   integer,
    raw_news_content_purged_at timestamptz,
    raw_news_kogl_type        smallint
) ON COMMIT DROP;

\copy tier0_news_raw FROM pstdin WITH (FORMAT csv, HEADER)

CREATE TEMP TABLE merge_plan ON COMMIT DROP AS
SELECT
    (SELECT count(*) FROM news_raw)                                   AS target_before,
    (SELECT count(*) FROM tier0_news_raw)                             AS tier0_rows,
    (SELECT count(*) FROM tier0_news_raw t
      WHERE NOT EXISTS (SELECT 1 FROM news_raw n WHERE n.raw_news_url = t.raw_news_url)) AS tier0_only,
    (SELECT count(DISTINCT t.press_name) FROM tier0_news_raw t
      WHERE NOT EXISTS (SELECT 1 FROM press p WHERE p.press_name = t.press_name))      AS unmapped_press,
    (SELECT count(*) FROM (
        SELECT p.press_name FROM press p
         WHERE EXISTS (SELECT 1 FROM tier0_news_raw t WHERE t.press_name = p.press_name)
         GROUP BY p.press_name HAVING count(*) > 1) d)                                 AS ambiguous_press;

DO $$
DECLARE
    plan merge_plan%ROWTYPE;
BEGIN
    SELECT * INTO plan FROM merge_plan;
    IF plan.unmapped_press > 0 THEN
        RAISE EXCEPTION 'press_name % 개가 대상 press 테이블에 없다 - 병합 중단', plan.unmapped_press;
    END IF;
    IF plan.ambiguous_press > 0 THEN
        RAISE EXCEPTION 'press_name % 개가 대상 press 테이블에 둘 이상 있다 - press_id를 정할 수 없어 병합 중단',
            plan.ambiguous_press;
    END IF;
END $$;

WITH ins AS (
    INSERT INTO news_raw (
        press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, raw_news_crawled_at,
        raw_news_extract_status, raw_news_extracted_at, raw_news_extract_attempts, raw_news_content_sha256,
        raw_news_content_length, raw_news_content_purged_at, raw_news_kogl_type
    )
    SELECT
        p.press_id,
        t.raw_news_title,
        -- 비운 본문(NULL)은 NULL 그대로: 'duplicate'로 들어가도 본문 칸을 ''로 바꾸지 않는다(비운 행의 CHECK).
        CASE WHEN dup.hit AND t.raw_news_content IS NOT NULL THEN '' ELSE t.raw_news_content END,
        t.raw_news_url,
        t.raw_news_created_at,
        t.raw_news_crawled_at,
        CASE WHEN dup.hit THEN 'duplicate' ELSE t.raw_news_extract_status END,
        t.raw_news_extracted_at,
        t.raw_news_extract_attempts,
        t.raw_news_content_sha256,
        CASE WHEN dup.hit AND t.raw_news_content IS NOT NULL THEN 0 ELSE t.raw_news_content_length END,
        t.raw_news_content_purged_at,
        t.raw_news_kogl_type
    FROM tier0_news_raw t
    JOIN press p ON p.press_name = t.press_name
    CROSS JOIN LATERAL (
        -- 상태마다 EXISTS를 따로 둔다: 각각 부분 인덱스(uq_news_raw_content_sha256_ok, ix_news_raw_content_sha256_rss_summary)를
        -- 탄다. 상태 IN (...) 한 줄이면 행마다 news_raw 전체를 읽는다.
        SELECT CASE t.raw_news_extract_status
            WHEN 'ok' THEN EXISTS (
                SELECT 1 FROM news_raw k
                WHERE k.raw_news_extract_status = 'ok'
                  AND k.raw_news_content_sha256 = t.raw_news_content_sha256
                  AND k.raw_news_url <> t.raw_news_url
            )
            WHEN 'rss_summary' THEN EXISTS (
                SELECT 1 FROM news_raw k
                WHERE k.raw_news_extract_status = 'ok'
                  AND k.raw_news_content_sha256 = t.raw_news_content_sha256
                  AND k.raw_news_url <> t.raw_news_url
            ) OR EXISTS (
                SELECT 1 FROM news_raw k
                WHERE k.raw_news_extract_status = 'rss_summary'
                  AND k.raw_news_content_sha256 = t.raw_news_content_sha256
                  AND k.raw_news_url <> t.raw_news_url
            )
            ELSE false
        END AS hit
    ) dup
    ORDER BY t.raw_news_crawled_at, t.raw_news_url
    ON CONFLICT (raw_news_url) DO NOTHING
    RETURNING raw_news_extract_status
)
SELECT raw_news_extract_status AS merged_status, count(*) AS merged_rows
FROM ins GROUP BY 1 ORDER BY 1;

DO $$
DECLARE
    plan merge_plan%ROWTYPE;
    after_rows bigint;
BEGIN
    SELECT * INTO plan FROM merge_plan;
    SELECT count(*) INTO after_rows FROM news_raw;
    RAISE NOTICE 'target_before=% tier0_rows=% tier0_only=% expected=% after=%',
        plan.target_before, plan.tier0_rows, plan.tier0_only, plan.target_before + plan.tier0_only, after_rows;
    IF after_rows <> plan.target_before + plan.tier0_only THEN
        RAISE EXCEPTION '병합 후 행 수 % <> 기대값 % - 되돌린다', after_rows, plan.target_before + plan.tier0_only;
    END IF;
END $$;
