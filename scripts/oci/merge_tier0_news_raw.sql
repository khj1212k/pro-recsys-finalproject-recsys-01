-- Tier 0(E2.1.Micro) news_raw를 Mac 덤프를 복원한 DB(Tier 1 A1)에 URL 기준으로 병합한다 (ADR 0026 결정 4, 런북 6.4).
--
-- 두 DB는 독립적으로 채워져 raw_news_id가 서로 다르다. 그래서 id가 아니라 raw_news_url(UNIQUE)로 합치고,
-- 언론사는 press_id가 아니라 press_name으로 찾는다. 규칙:
--   * 같은 URL이 이미 있으면 대상 쪽 행을 그대로 둔다(ON CONFLICT DO NOTHING) - Mac 행에는 임베딩이 있다.
--   * 새 URL인데 정제 본문 해시가 대상의 'ok' 행과 같으면 'duplicate'로 넣고 본문을 비운다
--     (부분 UNIQUE uq_news_raw_content_sha256_ok, Alembic d48994e9d26e와 같은 규칙).
--   * 병합된 행의 embedding_result는 NULL이다 - 대상 호스트의 embed 잡이 채운다.
--   * 기대 행 수(병합 전 + 대상에 없는 URL 수)와 결과가 다르거나 매핑되지 않는 언론사가 있으면 전체를 되돌린다.
--
-- 입력: Tier 0에서 뽑은 CSV (열 순서 고정, 헤더 포함)
--   psql -d newsletter -c "COPY (SELECT p.press_name, n.raw_news_title, n.raw_news_content, n.raw_news_url,
--       n.raw_news_created_at, n.raw_news_crawled_at, n.raw_news_extract_status, n.raw_news_extracted_at,
--       n.raw_news_extract_attempts, n.raw_news_content_sha256
--     FROM news_raw n JOIN press p USING (press_id) ORDER BY n.raw_news_id) TO STDOUT WITH (FORMAT csv, HEADER)"
--
-- 실행: 대상 DB에서, 스케줄러를 멈춘 상태로(동시 쓰기 없음). CSV는 psql의 표준 입력으로 준다.
--   psql -v ON_ERROR_STOP=1 -1 -d newsletter -f merge_tier0_news_raw.sql < tier0_news_raw.csv
-- -1(단일 트랜잭션)과 ON_ERROR_STOP이 있어야 검증 실패 시 아무것도 남지 않는다.

CREATE TEMP TABLE tier0_news_raw (
    press_name                text        NOT NULL,
    raw_news_title            text        NOT NULL,
    raw_news_content          text        NOT NULL,
    raw_news_url              text        NOT NULL,
    raw_news_created_at       timestamptz,
    raw_news_crawled_at       timestamp   NOT NULL,
    raw_news_extract_status   varchar(16),
    raw_news_extracted_at     timestamptz,
    raw_news_extract_attempts smallint    NOT NULL,
    raw_news_content_sha256   varchar(64)
) ON COMMIT DROP;

\copy tier0_news_raw FROM pstdin WITH (FORMAT csv, HEADER)

CREATE TEMP TABLE merge_plan ON COMMIT DROP AS
SELECT
    (SELECT count(*) FROM news_raw)                                   AS target_before,
    (SELECT count(*) FROM tier0_news_raw)                             AS tier0_rows,
    (SELECT count(*) FROM tier0_news_raw t
      WHERE NOT EXISTS (SELECT 1 FROM news_raw n WHERE n.raw_news_url = t.raw_news_url)) AS tier0_only,
    (SELECT count(DISTINCT t.press_name) FROM tier0_news_raw t
      WHERE NOT EXISTS (SELECT 1 FROM press p WHERE p.press_name = t.press_name))      AS unmapped_press;

DO $$
DECLARE
    plan merge_plan%ROWTYPE;
BEGIN
    SELECT * INTO plan FROM merge_plan;
    IF plan.unmapped_press > 0 THEN
        RAISE EXCEPTION 'press_name % 개가 대상 press 테이블에 없다 - 병합 중단', plan.unmapped_press;
    END IF;
END $$;

WITH ins AS (
    INSERT INTO news_raw (
        press_id, raw_news_title, raw_news_content, raw_news_url, raw_news_created_at, raw_news_crawled_at,
        raw_news_extract_status, raw_news_extracted_at, raw_news_extract_attempts, raw_news_content_sha256
    )
    SELECT
        p.press_id,
        t.raw_news_title,
        CASE WHEN dup.hit THEN '' ELSE t.raw_news_content END,
        t.raw_news_url,
        t.raw_news_created_at,
        t.raw_news_crawled_at,
        CASE WHEN dup.hit THEN 'duplicate' ELSE t.raw_news_extract_status END,
        t.raw_news_extracted_at,
        t.raw_news_extract_attempts,
        t.raw_news_content_sha256
    FROM tier0_news_raw t
    JOIN press p ON p.press_name = t.press_name
    CROSS JOIN LATERAL (
        SELECT t.raw_news_extract_status = 'ok' AND EXISTS (
            SELECT 1 FROM news_raw k
            WHERE k.raw_news_extract_status = 'ok'
              AND k.raw_news_content_sha256 = t.raw_news_content_sha256
              AND k.raw_news_url <> t.raw_news_url
        ) AS hit
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
