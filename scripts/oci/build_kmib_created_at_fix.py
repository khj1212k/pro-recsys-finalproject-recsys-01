"""발행 시각 보정 SQL을 만든다 (ADR 0008 "2026-10-25 갱신" · "갱신 2", docs/runbook-hosting.md 6.10). 처음에는 국민일보만이었다.

2026-10-19~10-23에 국민일보 RSS가 준 pubDate는 기사 페이지의 실제 발행 시각(JSON-LD `datePublished`)과 맞지 않았다 -
수집 시각보다 최대 24시간 뒤인 값이 440행이다(같은 시기의 템플릿 개편, ADR 0026 결정 9 2026-10-25 추가). 오프셋이 일정하지
않아 식으로 고칠 수 없으므로, 대상 행마다 기사 페이지의 `datePublished`를 받아(입력 CSV, 아래) 그 값으로 바꾸는 SQL을 만든다.

입력 CSV(머리줄 없음): raw_news_id, url, created_at_before(UTC ISO), crawled_at(UTC ISO), date_published(ISO, 페이지 값), error
  - 앞 네 칸은 VM에서 읽기 전용으로 뽑고(런북 6.10 1단계), 다섯째 칸은 페이지를 받아 채운다(2초 간격, 본문은 저장하지 않는다).
출력: 한 트랜잭션의 SQL. 기본은 리허설(ROLLBACK), -v apply=1이면 COMMIT. 보관 표에 원래 값을 남긴다.
되돌리기: scripts/oci/fix_kmib_created_at_revert.sql (보관 표가 기본 이름일 때) 또는 이 스크립트의 --revert

  python3 -I scripts/oci/build_kmib_created_at_fix.py <CSV>                                        # 1차: 국민일보, 보관 표 news_raw_kmib_created_at_fix
  python3 -I scripts/oci/build_kmib_created_at_fix.py <CSV> --all-presses --table news_raw_created_at_fix_2   # 2차: 전 언론사, 다른 보관 표
  python3 -I scripts/oci/build_kmib_created_at_fix.py --revert --table news_raw_created_at_fix_2          # 2차의 되돌리기 SQL
  --press <이름>은 다른 한 언론사로 좁힌다. 보관 표 이름은 소문자·숫자·밑줄만 받는다.

쓰는 행(나머지는 건너뛰고 건수만 머리말 주석에 적는다):
  - 페이지 값이 있고 시간대가 붙어 있다
  - 페이지 값이 수집 시각보다 10분 넘게 뒤가 아니다(수집 전에 발행됐어야 한다)
  - 페이지 값이 수집 시각보다 7일 넘게 앞이 아니다
SQL은 VALUES의 원래 값(created_at_before)과 지금 행의 값이 같은 행만 바꾼다 - 그 사이 다른 이유로 바뀐 행은 건드리지 않는다.
"""
import argparse
import csv
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 두 값은 수집기의 ai_workspace/crawler/content_extractor/page_pubdate.py(FUTURE_SLACK·MAX_AGE_BEFORE_CRAWL)와 같아야 한다.
# 이 스크립트는 `python3 -I`로 저장소 밖 의존성 없이 돌려서 거기서 읽지 않고 복사했다 - tests/test_build_kmib_created_at_fix.py가 같은지 확인한다.
SLACK_AFTER_CRAWL = timedelta(minutes=10)
MAX_BEFORE_CRAWL = timedelta(days=7)

DEFAULT_TABLE = "news_raw_kmib_created_at_fix"  # 되돌리기 파일(fix_kmib_created_at_revert.sql)이 쓰는 이름
DEFAULT_PRESS = "국민일보"
REVERT_TEMPLATE = Path(__file__).with_name("fix_kmib_created_at_revert.sql")
_TABLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def check_table(table: str) -> str:
    if not _TABLE_NAME.fullmatch(table):
        raise ValueError(f"보관 표 이름은 소문자·숫자·밑줄만 받는다: {table!r}")
    return table


def revert_sql(table: str = DEFAULT_TABLE) -> str:
    """보관 표 이름을 바꾼 되돌리기 SQL. 기본 이름이면 fix_kmib_created_at_revert.sql과 같다."""
    return REVERT_TEMPLATE.read_text(encoding="utf-8").replace(DEFAULT_TABLE, check_table(table))


def main(src: str, table: str = DEFAULT_TABLE, press: "str | None" = DEFAULT_PRESS) -> int:
    """press가 None이면 언론사를 가리지 않는다(2차 보정). 아니면 그 언론사의 행만 바꾼다."""
    check_table(table)
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
    scope = press or "전 언론사"
    scope_sql = scope.replace("'", "''")
    # 1차(기본 보관 표)의 출력은 예전과 글자 하나까지 같다 - 문구가 다른 것은 기본 보관 표가 아닐 때(2차)뿐이다.
    first_pass = table == DEFAULT_TABLE
    table_note = "" if first_pass else f", 보관 표 {table}"
    adr_ref = "ADR 0008 2026-10-25" if first_pass else "ADR 0008 2026-10-25 갱신 2"
    source_default = ("article page JSON-LD datePublished" if first_pass
                      else "article page datePublished (JSON-LD, else meta article:published_time)")
    press_in_stop = f"{press} " if press else ""
    if press is None:
        press_join, press_where = "", ""
    else:
        press_join = "JOIN press p ON p.press_id = n.press_id\n"
        press_where = "p.press_name = '" + press.replace("'", "''") + "' AND "
    sys.stdout.write(f"""-- {scope} 발행 시각 보정 (scripts/oci/build_kmib_created_at_fix.py가 만든 파일, ADR 0008 "2026-10-25 갱신", 런북 6.10).
-- 행 {len(keep)}개, 건너뜀 {skipped}{table_note}. 기본은 리허설(ROLLBACK), -v apply=1이면 COMMIT.
--   psql -X -v ON_ERROR_STOP=1 [-v apply=1] -f <이 파일>
-- 멈추는 조건: 보관 표가 이미 있다(이미 보정함) / 바꾼 행 수가 기대와 다르다 / 바꾼 뒤에도 수집 시각보다 10분 넘게 뒤인 {press_in_stop}행이 대상에 남는다.

\\set ON_ERROR_STOP on
\\if :{{?apply}}
\\else
\\set apply 0
\\endif

BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE TABLE {table} (
    raw_news_id integer PRIMARY KEY,
    created_at_before timestamptz NOT NULL,
    created_at_after timestamptz NOT NULL,
    source text NOT NULL DEFAULT '{source_default}',
    fixed_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE {table} IS
    '{scope_sql} 발행 시각 보정({adr_ref})의 원래 값. 되돌리기용이자 "이 DB는 보정했다"는 표시 - 되돌리기 스크립트만 지운다.';

INSERT INTO {table} (raw_news_id, created_at_before, created_at_after)
SELECT n.raw_news_id, n.raw_news_created_at, v.after
FROM (VALUES
{values}
) AS v(id, before, after)
JOIN news_raw n ON n.raw_news_id = v.id
{press_join}WHERE {press_where}n.raw_news_created_at = v.before;

UPDATE news_raw n
SET raw_news_created_at = b.created_at_after
FROM {table} b
WHERE n.raw_news_id = b.raw_news_id;

DO $$
DECLARE fixed bigint; ahead bigint;
BEGIN
    SELECT count(*) INTO fixed FROM {table};
    IF fixed <> {len(keep)} THEN
        RAISE EXCEPTION '보정 대상 %행 중 %행만 원래 값이 그대로였다 - 다시 뽑는다(런북 6.10)', {len(keep)}, fixed;
    END IF;
    SELECT count(*) INTO ahead FROM news_raw n JOIN {table} b USING (raw_news_id)
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


def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", nargs="?", help="입력 CSV(--revert이면 필요 없다)")
    parser.add_argument("--table", default=DEFAULT_TABLE, help=f"보관 표 이름(기본 {DEFAULT_TABLE})")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--press", default=DEFAULT_PRESS, help=f"이 언론사의 행만 바꾼다(기본 {DEFAULT_PRESS})")
    scope.add_argument("--all-presses", action="store_true", help="언론사를 가리지 않는다(2차 보정)")
    parser.add_argument("--revert", action="store_true", help="보정 대신 --table의 되돌리기 SQL을 출력한다")
    args = parser.parse_args(argv)
    try:
        check_table(args.table)
        if args.revert:
            sys.stdout.write(revert_sql(args.table))
            return 0
        if not args.csv:
            parser.error("입력 CSV가 필요하다")
        return main(args.csv, table=args.table, press=None if args.all_presses else args.press)
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
