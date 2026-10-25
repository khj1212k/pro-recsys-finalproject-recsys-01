"""국민일보 발행 시각 보정 SQL을 만든다 (ADR 0008 "2026-10-25 갱신", docs/runbook-hosting.md 6.10).

2026-10-19~10-23에 국민일보 RSS가 준 pubDate는 기사 페이지의 실제 발행 시각(JSON-LD `datePublished`)과 맞지 않았다 -
수집 시각보다 최대 24시간 뒤인 값이 440행이다(같은 시기의 템플릿 개편, ADR 0026 결정 9 2026-10-25 추가). 오프셋이 일정하지
않아 식으로 고칠 수 없으므로, 대상 행마다 기사 페이지의 `datePublished`를 받아(입력 CSV, 아래) 그 값으로 바꾸는 SQL을 만든다.

입력 CSV(머리줄 없음): raw_news_id, url, created_at_before(UTC ISO), crawled_at(UTC ISO), date_published(ISO, 페이지 값), error
  - 앞 네 칸은 VM에서 읽기 전용으로 뽑고(런북 6.10 1단계), 다섯째 칸은 페이지를 받아 채운다(2초 간격, 본문은 저장하지 않는다).
출력: 한 트랜잭션의 SQL. 기본은 리허설(ROLLBACK), -v apply=1이면 COMMIT. 보관 표 news_raw_kmib_created_at_fix에 원래 값을 남긴다.
되돌리기: scripts/oci/fix_kmib_created_at_revert.sql

쓰는 행(나머지는 건너뛰고 건수만 머리말 주석에 적는다):
  - 페이지 값이 있고 시간대가 붙어 있다
  - 페이지 값이 수집 시각보다 10분 넘게 뒤가 아니다(수집 전에 발행됐어야 한다)
  - 페이지 값이 수집 시각보다 7일 넘게 앞이 아니다
SQL은 VALUES의 원래 값(created_at_before)과 지금 행의 값이 같은 행만 바꾼다 - 그 사이 다른 이유로 바뀐 행은 건드리지 않는다.
"""
import csv
import sys
from datetime import datetime, timedelta, timezone

SLACK_AFTER_CRAWL = timedelta(minutes=10)
MAX_BEFORE_CRAWL = timedelta(days=7)


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def main(src: str) -> int:
    keep, skipped = [], {"no_page_time": 0, "no_tz": 0, "after_crawl": 0, "too_old": 0}
    for rid, url, before, crawled, pub, err in csv.reader(open(src, encoding="utf-8")):
        if not pub:
            skipped["no_page_time"] += 1
            continue
        p = parse(pub)
        if p.tzinfo is None:
            skipped["no_tz"] += 1
            continue
        c = parse(crawled)
        if p > c + SLACK_AFTER_CRAWL:
            skipped["after_crawl"] += 1
            continue
        if p < c - MAX_BEFORE_CRAWL:
            skipped["too_old"] += 1
            continue
        keep.append((int(rid), parse(before).astimezone(timezone.utc), p.astimezone(timezone.utc)))
    if not keep:
        print("보정할 행이 없다", file=sys.stderr)
        return 1
    values = ",\n".join(f"    ({rid}, '{b.isoformat()}'::timestamptz, '{a.isoformat()}'::timestamptz)" for rid, b, a in keep)
    sys.stdout.write(f"""-- 국민일보 발행 시각 보정 (scripts/oci/build_kmib_created_at_fix.py가 만든 파일, ADR 0008 "2026-10-25 갱신", 런북 6.10).
-- 행 {len(keep)}개, 건너뜀 {skipped}. 기본은 리허설(ROLLBACK), -v apply=1이면 COMMIT.
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] -f <이 파일>
-- 멈추는 조건: 보관 표가 이미 있다(이미 보정함) / 바꾼 행 수가 기대와 다르다 / 바꾼 뒤에도 수집 시각보다 10분 넘게 뒤인 국민일보 행이 대상에 남는다.

\\set ON_ERROR_STOP on
\\if :{{?apply}}
\\else
\\set apply 0
\\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE TABLE news_raw_kmib_created_at_fix (
    raw_news_id integer PRIMARY KEY,
    created_at_before timestamptz NOT NULL,
    created_at_after timestamptz NOT NULL,
    source text NOT NULL DEFAULT 'article page JSON-LD datePublished',
    fixed_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE news_raw_kmib_created_at_fix IS
    '국민일보 발행 시각 보정(ADR 0008 2026-10-25)의 원래 값. 되돌리기용이자 "이 DB는 보정했다"는 표시 - 되돌리기 스크립트만 지운다.';

INSERT INTO news_raw_kmib_created_at_fix (raw_news_id, created_at_before, created_at_after)
SELECT n.raw_news_id, n.raw_news_created_at, v.after
FROM (VALUES
{values}
) AS v(id, before, after)
JOIN news_raw n ON n.raw_news_id = v.id
JOIN press p ON p.press_id = n.press_id
WHERE p.press_name = '국민일보' AND n.raw_news_created_at = v.before;

UPDATE news_raw n
SET raw_news_created_at = b.created_at_after
FROM news_raw_kmib_created_at_fix b
WHERE n.raw_news_id = b.raw_news_id;

DO $$
DECLARE fixed bigint; ahead bigint;
BEGIN
    SELECT count(*) INTO fixed FROM news_raw_kmib_created_at_fix;
    IF fixed <> {len(keep)} THEN
        RAISE EXCEPTION '보정 대상 %행 중 %행만 원래 값이 그대로였다 - 다시 뽑는다(런북 6.10)', {len(keep)}, fixed;
    END IF;
    SELECT count(*) INTO ahead FROM news_raw n JOIN news_raw_kmib_created_at_fix b USING (raw_news_id)
    WHERE n.raw_news_created_at > (n.raw_news_crawled_at AT TIME ZONE 'UTC') + interval '10 minutes';
    IF ahead > 0 THEN
        RAISE EXCEPTION '보정한 행 중 %행이 아직 수집 시각보다 10분 넘게 뒤다', ahead;
    END IF;
    RAISE NOTICE 'fix_kmib_created_at fixed=%', fixed;
END $$;

\\if :apply
COMMIT;
\\echo 'fix_kmib_created_at: COMMIT'
\\else
ROLLBACK;
\\echo 'fix_kmib_created_at: ROLLBACK (리허설 - -v apply=1이면 커밋한다)'
\\endif
""")
    print(f"rows={len(keep)} skipped={skipped}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
