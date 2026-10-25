# scripts/oci/build_kmib_created_at_fix.py - 페이지 발행 시각으로 국민일보 발행 시각을 고치는 SQL 생성기. 지어낸 id·URL만 쓴다.
import csv
import importlib.util
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_kmib_created_at_fix", REPO / "scripts" / "oci" / "build_kmib_created_at_fix.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def _run(tmp_path, rows, capsys):
    src = tmp_path / "in.csv"
    with open(src, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    rc = gen.main(str(src))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_keeps_rows_whose_page_time_is_before_the_crawl_and_skips_the_rest(tmp_path, capsys):
    rows = [
        ["1", "https://example.test/article/1", "2026-10-23T10:38:12Z", "2026-10-22T19:07:00Z", "2026-10-23T03:05:00+09:00", ""],  # 수집 2분 전 발행
        ["2", "https://example.test/article/2", "2026-10-23T10:38:12Z", "2026-10-22T19:07:00Z", "", "no datePublished"],
        ["3", "https://example.test/article/3", "2026-10-23T10:38:12Z", "2026-10-22T19:07:00Z", "2026-10-23T12:00:00+09:00", ""],  # 수집보다 뒤
        ["4", "https://example.test/article/4", "2026-10-23T10:38:12Z", "2026-10-22T19:07:00Z", "2026-10-10T12:00:00+09:00", ""],  # 7일 넘게 앞
        ["5", "https://example.test/article/5", "2026-10-23T10:38:12Z", "2026-10-22T19:07:00Z", "2026-10-23T03:05:00", ""],        # 시간대 없음
    ]
    rc, sql, err = _run(tmp_path, rows, capsys)
    assert rc == 0
    assert "(1, '2026-10-23T10:38:12+00:00'::timestamptz, '2026-10-22T18:05:00+00:00'::timestamptz)" in sql
    for rid in ("2", "3", "4", "5"):
        assert f"\n    ({rid}, " not in sql
    assert "rows=1" in err and "'no_page_time': 1" in err and "'after_crawl': 1" in err and "'too_old': 1" in err and "'no_tz': 1" in err
    assert "IF fixed <> 1 THEN" in sql and "n.raw_news_created_at = v.before" in sql


def test_no_usable_rows_returns_1(tmp_path, capsys):
    rc, sql, _ = _run(tmp_path, [["9", "u", "2026-10-23T10:00:00Z", "2026-10-22T19:00:00Z", "", "x"]], capsys)
    assert rc == 1 and sql == ""
