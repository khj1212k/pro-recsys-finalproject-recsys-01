-- Tier 0 승격 기준(ADR 0026 결정 3)의 7일 창 재측정 질의 (읽기 전용). ADR 0026 증거 13의 수치를 다시 내는 질의이고("재수집" 정의만 고쳤다 - C1), 쓰는 법은 런북 6.11에 있다.
--   ssh -F .ops/micro/ssh_config micro "cd ~/newsletter-recsys && sudo docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U newsletter -d newsletter -f -" < scripts/oci/soak_window_check.sql
-- 창은 아래 \set 두 줄이다. 처음 값은 2026-10-25 승격 판정 창이고, 2026-11-01 재확인은 10-25 07:00 ~ 11-01 07:00 UTC로 바꾼다.
-- 창의 시작·끝은 정시(분 0초 0)여야 한다(정시 슬롯을 시 단위로 센다).
-- 읽는 것: job_runs(상태·시각·통계)와 news_raw의 상태·시각, 기사 제목은 재수집 묶음 판정에서 같은지만 비교한다(출력하지 않는다). 본문 칸은 읽지 않는다.
-- 이 질의로 못 재는 것: 커널 저널의 OOM, docker inspect, 스크립트 sha256, 버킷의 full/ 수명 주기 삭제(런북 6.11의 따로 읽는 목록).
\set w0 '''2026-10-18 07:00+00'''
\set w1 '''2026-10-25 07:00+00'''
-- 국민일보가 URL 형식을 바꾼 시각(UTC). 이 앞에 구 URL(view.asp)로 수집된 기사가 새 URL로 다시 들어온 행을 "재수집"으로 센다.
\set kmib_reorg '''2026-10-19 05:00'''

SET default_transaction_read_only = on;
SET statement_timeout = '180s';
\pset format unaligned
\pset fieldsep ' | '
\pset tuples_only on

-- ============================================================ A. 잡 (결정 3 (a))
-- A1. 정시 ingest 슬롯: 슬롯 수 | 시작한 슬롯 | 성공한 슬롯 (시작되지 않은 회차까지 센다, 증거 8)
WITH slots AS (SELECT generate_series((:w0)::timestamptz, (:w1)::timestamptz - interval '1 hour', interval '1 hour') h),
ing AS (SELECT date_trunc('hour', started_at) h, bool_or(status = 'succeeded') ok
        FROM job_runs WHERE job = 'ingest' AND started_at >= :w0 AND started_at < :w1 GROUP BY 1)
SELECT 'A1 ingest_slots', count(*), count(i.h), count(*) FILTER (WHERE i.ok)
FROM slots s LEFT JOIN ing i USING (h);

-- A2. 연속 48슬롯 창: 창 수 | 48/48인 창 | 최솟값
WITH slots AS (SELECT row_number() OVER (ORDER BY h) rn, h
               FROM generate_series((:w0)::timestamptz, (:w1)::timestamptz - interval '1 hour', interval '1 hour') h),
ing AS (SELECT date_trunc('hour', started_at) h, bool_or(status = 'succeeded') ok
        FROM job_runs WHERE job = 'ingest' AND started_at >= :w0 AND started_at < :w1 GROUP BY 1),
j AS (SELECT s.rn, coalesce(i.ok, false)::int ok FROM slots s LEFT JOIN ing i USING (h)),
w AS (SELECT sum(ok) OVER (ORDER BY rn ROWS BETWEEN CURRENT ROW AND 47 FOLLOWING) s,
             count(*) OVER (ORDER BY rn ROWS BETWEEN CURRENT ROW AND 47 FOLLOWING) c FROM j)
SELECT 'A2 win48', count(*), count(*) FILTER (WHERE s = 48), min(s) FROM w WHERE c = 48;

-- A3. 정시 popularity 슬롯: 슬롯 수 | 시작 | 성공
WITH slots AS (SELECT generate_series((:w0)::timestamptz, (:w1)::timestamptz - interval '1 hour', interval '1 hour') h),
p AS (SELECT date_trunc('hour', started_at) h, bool_or(status = 'succeeded') ok
      FROM job_runs WHERE job = 'popularity' AND started_at >= :w0 AND started_at < :w1 GROUP BY 1)
SELECT 'A3 popularity_slots', count(*), count(p.h), count(*) FILTER (WHERE p.ok)
FROM slots s LEFT JOIN p USING (h);

-- A4. 창 안 전체 잡(손으로 한 실행 포함): 잡 | 실행 | 성공 | 끝나지 않은 것 | 상태 종류 (마지막 줄 NULL이 합계)
SELECT 'A4 jobs', job, count(*), count(*) FILTER (WHERE status = 'succeeded'), count(*) FILTER (WHERE finished_at IS NULL),
       string_agg(DISTINCT status, ',')
FROM job_runs WHERE started_at >= :w0 AND started_at < :w1
GROUP BY ROLLUP (job) ORDER BY job NULLS LAST;

-- A5. ingest 소요(초): 중앙 | p95 | 최대 | 1시간 넘은 회차
SELECT 'A5 ingest_seconds',
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at))::numeric),
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at - started_at))::numeric),
       round(max(extract(epoch FROM finished_at - started_at))::numeric),
       count(*) FILTER (WHERE finished_at - started_at > interval '1 hour')
FROM job_runs WHERE job = 'ingest' AND started_at >= :w0 AND started_at < :w1;

-- A6. 코드(git_sha)별: 코드 | 잡 | 실행 | 성공 | 첫 시작 | 마지막 시작 (창 안에서 코드가 바뀐 곳을 본다. 손으로 한 실행은 코드 칸이 비어 있다)
SELECT 'A6 by_sha', left(coalesce(git_sha, '(null)'), 7), job, count(*), count(*) FILTER (WHERE status = 'succeeded'),
       to_char(min(started_at), 'MM-DD HH24:MI'), to_char(max(started_at), 'MM-DD HH24:MI')
FROM job_runs WHERE started_at >= :w0 AND started_at < :w1
GROUP BY 2, 3 ORDER BY min(started_at);

-- A7. 정시가 아닌 실행(손으로 한 백업·복원 검증, rss_retry): id | 잡 | 상태 | 시작 | 코드
SELECT 'A7 non_cron', id, job, status, to_char(started_at, 'MM-DD HH24:MI:SS'), left(coalesce(git_sha, ''), 7)
FROM job_runs WHERE started_at >= :w0 AND started_at < :w1 AND job IN ('backup', 'restore_check', 'rss_retry')
  AND (job <> 'backup' OR extract(minute FROM started_at) <> 10 OR extract(hour FROM started_at) <> 18)
ORDER BY started_at;

-- ============================================================ B. 경고와 동아일보 RSS (성공률이 무엇을 가렸나)
-- B1. 경고가 있던 ingest: 경고 있는 회차 | 전체 ingest | 경고 수 합
SELECT 'B1 warn_runs', count(*) FILTER (WHERE jsonb_array_length(coalesce(stats->'warnings', '[]'::jsonb)) > 0), count(*),
       sum(jsonb_array_length(coalesce(stats->'warnings', '[]'::jsonb)))
FROM job_runs WHERE job = 'ingest' AND started_at >= :w0 AND started_at < :w1;

-- B2. 동아일보 RSS 읽기: 읽기를 시도한 회차 | 실패 | 성공
SELECT 'B2 donga_rss', count(*), count(*) FILTER (WHERE e.value->>'error' IS NOT NULL), count(*) FILTER (WHERE e.value->>'error' IS NULL)
FROM job_runs j, jsonb_each(j.stats->'rss'->'per_feed') e
WHERE j.job = 'ingest' AND j.started_at >= :w0 AND j.started_at < :w1 AND e.value->>'press' = '동아일보';

-- B3. 동아일보 RSS 성공 사이 공백: 성공 회차 | 공백 수 | 6시간 이상 | 12시간 이상 | 최장
WITH r AS (SELECT j.started_at FROM job_runs j
           WHERE j.job = 'ingest' AND j.started_at >= :w0 AND j.started_at < :w1
             AND EXISTS (SELECT 1 FROM jsonb_each(j.stats->'rss'->'per_feed') e
                         WHERE e.value->>'press' = '동아일보' AND e.value->>'error' IS NULL)),
g AS (SELECT started_at - lag(started_at) OVER (ORDER BY started_at) gap FROM r)
SELECT 'B3 donga_gaps', count(*), count(gap), count(*) FILTER (WHERE gap >= interval '6 hours'),
       count(*) FILTER (WHERE gap >= interval '12 hours'), max(gap)
FROM g;

-- B4. 옛 건전성 규칙(ADR 0006 결정 9)을 저장된 통계에 대 본 반사실: 회차 | (가) targets를 분모로 쓰면 | (나) 시도한 기사 5건 이상인 회차 | (나) 그중 절반 이상 실패 | (다) 미룬 기사를 실패로
WITH e AS (SELECT coalesce((stats->'extract'->>'targets')::int, 0) t, coalesce((stats->'extract'->>'deferred')::int, 0) d,
                  coalesce((stats->'extract'->>'fetch_failed')::int, 0) + coalesce((stats->'extract'->>'error')::int, 0) f
           FROM job_runs WHERE job = 'ingest' AND started_at >= :w0 AND started_at < :w1)
SELECT 'B4 old_rule', count(*),
       count(*) FILTER (WHERE t >= 5 AND f * 2 >= t),
       count(*) FILTER (WHERE t - d >= 5),
       count(*) FILTER (WHERE t - d >= 5 AND f * 2 >= t - d),
       count(*) FILTER (WHERE t >= 5 AND (f + d) * 2 >= t)
FROM e;

-- ============================================================ C. 수집 지연 (결정 3 (a), 발행 raw_news_created_at -> 수집 raw_news_crawled_at, 시간)
-- 창 안에 수집된 행. 읽기: 가(제외 없음, 판정값) | 나(재수집 제외) | 다(국민일보 제외) | 음수를 최악(99시간)으로
-- 열: 읽기 | 행 | 중앙 | p95 | p99 | 최대 | 3시간 넘은 행 | 음수 행
WITH w AS (
  SELECT n.raw_news_id, p.press_name, n.raw_news_url, md5(n.raw_news_title) th,
         extract(epoch FROM n.raw_news_crawled_at - (n.raw_news_created_at AT TIME ZONE 'UTC')) / 3600 h
  FROM news_raw n JOIN press p USING (press_id)
  WHERE n.raw_news_crawled_at >= (:w0)::timestamptz AT TIME ZONE 'UTC' AND n.raw_news_crawled_at < (:w1)::timestamptz AT TIME ZONE 'UTC'
), old_titles AS (   -- 개편 앞에 구 URL로 수집된 국민일보 기사의 제목 해시
  SELECT DISTINCT md5(o.raw_news_title) th FROM news_raw o JOIN press po USING (press_id)
  WHERE po.press_name = '국민일보' AND o.raw_news_url LIKE '%view.asp%' AND o.raw_news_crawled_at < (:kmib_reorg)::timestamp
), recrawl AS (      -- 새 URL 형식(view.asp가 아님)으로 다시 들어온 행. 구 URL 행은 넣지 않는다(지연이 정상이다)
  SELECT w.raw_news_id FROM w
  WHERE w.press_name = '국민일보' AND w.raw_news_url LIKE '%/article/%' AND w.raw_news_url NOT LIKE '%view.asp%'
    AND w.th IN (SELECT th FROM old_titles)
), r AS (
  SELECT 1 o, '가. 전체 행(판정값)' k, h FROM w
  UNION ALL SELECT 2, '나. 재수집 제외', h FROM w WHERE raw_news_id NOT IN (SELECT raw_news_id FROM recrawl)
  UNION ALL SELECT 3, '다. 국민일보 제외', h FROM w WHERE press_name <> '국민일보'
  UNION ALL SELECT 4, '(참고) 음수를 99시간으로', CASE WHEN h < 0 THEN 99 ELSE h END FROM w
)
SELECT 'C1 delay', k, count(*),
       round((percentile_cont(0.5) WITHIN GROUP (ORDER BY h))::numeric, 2),
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY h))::numeric, 3),
       round((percentile_cont(0.99) WITHIN GROUP (ORDER BY h))::numeric, 2),
       round(max(h)::numeric, 2), count(*) FILTER (WHERE h > 3), count(*) FILTER (WHERE h < 0)
FROM r GROUP BY o, k ORDER BY o;

-- C2. 언론사별 (가 읽기): 언론사 | 행 | 중앙 | p95 | 3시간 넘은 행 | 최대
WITH w AS (
  SELECT p.press_name, extract(epoch FROM n.raw_news_crawled_at - (n.raw_news_created_at AT TIME ZONE 'UTC')) / 3600 h
  FROM news_raw n JOIN press p USING (press_id)
  WHERE n.raw_news_crawled_at >= (:w0)::timestamptz AT TIME ZONE 'UTC' AND n.raw_news_crawled_at < (:w1)::timestamptz AT TIME ZONE 'UTC')
SELECT 'C2 by_press', press_name, count(*), round((percentile_cont(0.5) WITHIN GROUP (ORDER BY h))::numeric, 2),
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY h))::numeric, 2), count(*) FILTER (WHERE h > 3), round(max(h)::numeric, 1)
FROM w GROUP BY press_name ORDER BY press_name;

-- C3. 수집일(UTC)별 (가 읽기): 날짜 | 행 | p95 | 3시간 넘은 행
WITH w AS (
  SELECT n.raw_news_crawled_at c, extract(epoch FROM n.raw_news_crawled_at - (n.raw_news_created_at AT TIME ZONE 'UTC')) / 3600 h
  FROM news_raw n
  WHERE n.raw_news_crawled_at >= (:w0)::timestamptz AT TIME ZONE 'UTC' AND n.raw_news_crawled_at < (:w1)::timestamptz AT TIME ZONE 'UTC')
SELECT 'C3 by_day', c::date, count(*), round((percentile_cont(0.95) WITHIN GROUP (ORDER BY h))::numeric, 2), count(*) FILTER (WHERE h > 3)
FROM w GROUP BY 2 ORDER BY 2;

-- C4. 국민일보 발행 시각 이상(저장된 발행 시각이 수집보다 10분 넘게 뒤인 새 URL 행): 수집 시각(시) | 행 수. 보정(런북 6.10) 뒤에도 새로 들어오면 여기 나온다.
--     보관 표 news_raw_kmib_created_at_fix가 있으면 이미 보정한 행은 뺀다. 재확인 전에 이 줄이 비어 있게 6.10을 돌린다.
SELECT to_regclass('news_raw_kmib_created_at_fix') IS NOT NULL AS has_fix \gset
\if :has_fix
SELECT 'C4 kmib_anomaly', to_char(n.raw_news_crawled_at, 'MM-DD HH24'), count(*)
FROM news_raw n JOIN press p USING (press_id)
WHERE p.press_name = '국민일보' AND n.raw_news_url NOT LIKE '%view.asp%'
  AND n.raw_news_crawled_at >= (:w0)::timestamptz AT TIME ZONE 'UTC' AND n.raw_news_crawled_at < (:w1)::timestamptz AT TIME ZONE 'UTC'
  AND n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + interval '10 minutes'
  AND NOT EXISTS (SELECT 1 FROM news_raw_kmib_created_at_fix f WHERE f.raw_news_id = n.raw_news_id)
GROUP BY 2 ORDER BY 2;
-- C5. 보정 전 읽기(보관 표의 원래 값으로 되돌려 계산): 읽기 | 행 | 중앙 | p95 | p99 | 최대
WITH w AS (
  SELECT extract(epoch FROM n.raw_news_crawled_at - (coalesce(f.created_at_before, n.raw_news_created_at) AT TIME ZONE 'UTC')) / 3600 h
  FROM news_raw n LEFT JOIN news_raw_kmib_created_at_fix f ON f.raw_news_id = n.raw_news_id
  WHERE n.raw_news_crawled_at >= (:w0)::timestamptz AT TIME ZONE 'UTC' AND n.raw_news_crawled_at < (:w1)::timestamptz AT TIME ZONE 'UTC'
), r AS (
  SELECT 1 o, '가′. 전체 행, 음수 그대로' k, h FROM w
  UNION ALL SELECT 2, '나′. 음수 제외', h FROM w WHERE h >= 0
  UNION ALL SELECT 3, '라′. 음수를 99시간으로', CASE WHEN h < 0 THEN 99 ELSE h END FROM w
)
SELECT 'C5 delay_before_fix', k, count(*), round((percentile_cont(0.5) WITHIN GROUP (ORDER BY h))::numeric, 2),
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY h))::numeric, 3), round((percentile_cont(0.99) WITHIN GROUP (ORDER BY h))::numeric, 2),
       round(max(h)::numeric, 2)
FROM r GROUP BY o, k ORDER BY o;
\else
SELECT 'C4 kmib_anomaly', to_char(n.raw_news_crawled_at, 'MM-DD HH24'), count(*)
FROM news_raw n JOIN press p USING (press_id)
WHERE p.press_name = '국민일보' AND n.raw_news_url NOT LIKE '%view.asp%'
  AND n.raw_news_crawled_at >= (:w0)::timestamptz AT TIME ZONE 'UTC' AND n.raw_news_crawled_at < (:w1)::timestamptz AT TIME ZONE 'UTC'
  AND n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + interval '10 minutes'
GROUP BY 2 ORDER BY 2;
\endif

-- ============================================================ D. 언론사별 추출 상태 (조회 시각 현재 상태, 범위를 정하는 근거)
-- 언론사 | 수집 행 | ok | dropped | duplicate | fetch_failed | blocked_origin | rss_summary | 상태 NULL
SELECT 'D1 extract_status', p.press_name, count(*),
       count(*) FILTER (WHERE n.raw_news_extract_status = 'ok'), count(*) FILTER (WHERE n.raw_news_extract_status = 'dropped'),
       count(*) FILTER (WHERE n.raw_news_extract_status = 'duplicate'), count(*) FILTER (WHERE n.raw_news_extract_status = 'fetch_failed'),
       count(*) FILTER (WHERE n.raw_news_extract_status = 'blocked_origin'), count(*) FILTER (WHERE n.raw_news_extract_status = 'rss_summary'),
       count(*) FILTER (WHERE n.raw_news_extract_status IS NULL)
FROM news_raw n JOIN press p USING (press_id)
WHERE n.raw_news_crawled_at >= (:w0)::timestamptz AT TIME ZONE 'UTC' AND n.raw_news_crawled_at < (:w1)::timestamptz AT TIME ZONE 'UTC'
GROUP BY p.press_name ORDER BY p.press_name;

-- ============================================================ E. 백업·복원·유휴 (결정 3 (b))
-- E1. 정시 백업(18:10 UTC): 성공 | 실행 | 소요 최소 | 최대(초) | 코드
SELECT 'E1 backup', count(*) FILTER (WHERE status = 'succeeded'), count(*),
       round(min(extract(epoch FROM finished_at - started_at))::numeric), round(max(extract(epoch FROM finished_at - started_at))::numeric),
       string_agg(DISTINCT left(coalesce(git_sha, ''), 7), ',')
FROM job_runs WHERE job = 'backup' AND started_at >= :w0 AND started_at < :w1
  AND extract(hour FROM started_at) = 18 AND extract(minute FROM started_at) = 10;

-- E2. 복원 검증(restore_check) 최근 5건: id | 상태 | 시작 | 아카이브 | 복원한 Alembic 리비전 | news_raw 행 | 소요(초). 창 안에 있고 리비전이 지금 head여야 한다
SELECT 'E2 restore_check', id, status, to_char(started_at, 'MM-DD HH24:MI:SS'), stats->>'archive',
       stats->'checks'->>'alembic_version', stats->'checks'->'table_rows'->>'news_raw', stats->>'duration_s'
FROM job_runs WHERE job = 'restore_check' ORDER BY id DESC LIMIT 5;

-- E3. 유휴 회수 점검·일일 리포트·본문 보존 잡: 잡 | 성공 | 실행
SELECT 'E3 daily_jobs', job, count(*) FILTER (WHERE status = 'succeeded'), count(*)
FROM job_runs WHERE job IN ('idle_check', 'daily_report', 'retention') AND started_at >= :w0 AND started_at < :w1 GROUP BY job ORDER BY job;
