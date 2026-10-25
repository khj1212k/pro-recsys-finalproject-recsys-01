# scripts/oci/build_kmib_created_at_fix.py - 페이지 발행 시각으로 발행 시각을 고치는 SQL 생성기(1차 국민일보, 2차 전 언론사). 지어낸 id·URL만 쓴다.
import csv
import importlib.util
import io
import sys
from pathlib import Path

import pytest

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


# ----------------------------------------------------------------------------- 2차 보정: 보관 표 이름·언론사 인자 (런북 6.10)

ROW = ["1", "https://example.test/article/1", "2026-10-26T09:00:00Z", "2026-10-25T10:05:00Z", "2026-10-25T18:50:00+09:00", ""]


def _cli(tmp_path, capsys, *args, rows=(ROW,)):
    src = tmp_path / "in.csv"
    with open(src, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    rc = gen.cli([str(src), *args])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_default_run_is_unchanged_kmib_only_with_the_first_audit_table(tmp_path, capsys):
    rc, sql, _ = _cli(tmp_path, capsys)
    assert rc == 0
    assert "CREATE TABLE news_raw_kmib_created_at_fix (" in sql
    assert "JOIN press p ON p.press_id = n.press_id\nWHERE p.press_name = '국민일보' AND n.raw_news_created_at = v.before;" in sql
    assert "FROM news_raw_kmib_created_at_fix b\nWHERE n.raw_news_id = b.raw_news_id;" in sql


def test_default_run_keeps_the_first_pass_wording_to_the_letter(tmp_path, capsys):
    """인자 없이 부르면 예전과 같은 출력이다 - 머리말, 보관 표 설명, 출처 기본값, 멈추는 조건의 문구까지."""
    _, sql, _ = _cli(tmp_path, capsys)
    lines = sql.splitlines()
    assert lines[0] == '-- 국민일보 발행 시각 보정 (scripts/oci/build_kmib_created_at_fix.py가 만든 파일, ADR 0008 "2026-10-25 갱신", 런북 6.10).'
    assert lines[1] == ("-- 행 1개, 건너뜀 {'no_page_time': 0, 'no_tz': 0, 'after_crawl': 0, 'too_old': 0}. "
                        "기본은 리허설(ROLLBACK), -v apply=1이면 COMMIT.")
    assert lines[3] == ("-- 멈추는 조건: 보관 표가 이미 있다(이미 보정함) / 바꾼 행 수가 기대와 다르다 / "
                        "바꾼 뒤에도 수집 시각보다 10분 넘게 뒤인 국민일보 행이 대상에 남는다.")
    assert "    source text NOT NULL DEFAULT 'article page JSON-LD datePublished',\n" in sql
    assert ("COMMENT ON TABLE news_raw_kmib_created_at_fix IS\n"
            "    '국민일보 발행 시각 보정(ADR 0008 2026-10-25)의 원래 값. 되돌리기용이자 \"이 DB는 보정했다\"는 표시 - 되돌리기 스크립트만 지운다.';") in sql
    assert "보관 표 news_raw" not in sql  # 머리말에 보관 표 이름을 덧붙이는 것은 기본이 아닐 때뿐이다


def test_second_pass_wording_names_the_second_pass_and_the_meta_fallback(tmp_path, capsys):
    _, sql, _ = _cli(tmp_path, capsys, "--table", "news_raw_created_at_fix_2", "--all-presses")
    lines = sql.splitlines()
    assert lines[0].startswith("-- 전 언론사 발행 시각 보정 (")
    assert lines[1].endswith(", 보관 표 news_raw_created_at_fix_2. 기본은 리허설(ROLLBACK), -v apply=1이면 COMMIT.")
    assert lines[3].endswith("10분 넘게 뒤인 행이 대상에 남는다.")
    assert "DEFAULT 'article page datePublished (JSON-LD, else meta article:published_time)'" in sql
    assert "'전 언론사 발행 시각 보정(ADR 0008 2026-10-25 갱신 2)의 원래 값." in sql


def test_second_pass_uses_another_audit_table_and_does_not_limit_the_press(tmp_path, capsys):
    rc, sql, err = _cli(tmp_path, capsys, "--table", "news_raw_created_at_fix_2", "--all-presses")
    assert rc == 0 and "rows=1" in err
    assert "news_raw_kmib_created_at_fix" not in sql  # 첫 보관 표를 건드리지 않는다
    assert sql.count("news_raw_created_at_fix_2") >= 6
    assert "CREATE TABLE news_raw_created_at_fix_2 (" in sql
    assert "JOIN press" not in sql and "press_name" not in sql
    assert "JOIN news_raw n ON n.raw_news_id = v.id\nWHERE n.raw_news_created_at = v.before;" in sql
    assert "전 언론사" in sql


def test_press_can_be_narrowed_to_another_single_press_and_is_quoted(tmp_path, capsys):
    _, sql, _ = _cli(tmp_path, capsys, "--press", "세계일보")
    assert "WHERE p.press_name = '세계일보' AND n.raw_news_created_at = v.before;" in sql
    _, sql, _ = _cli(tmp_path, capsys, "--press", "O'Brien Times")
    assert "p.press_name = 'O''Brien Times'" in sql and "'O''Brien Times 발행 시각 보정" in sql


def test_press_and_all_presses_cannot_be_combined(tmp_path, capsys):
    with pytest.raises(SystemExit) as stop:
        _cli(tmp_path, capsys, "--press", "세계일보", "--all-presses")
    assert stop.value.code == 2


@pytest.mark.parametrize("table", ["News_Raw", "x; DROP TABLE news_raw", "1abc", "a-b", "", "a" * 64, "한글", 'x"y', "public.x"])
def test_unsafe_audit_table_names_are_refused(tmp_path, capsys, table):
    rc, sql, err = _cli(tmp_path, capsys, "--table", table)
    assert rc == 2 and sql == "" and "보관 표 이름" in err
    with pytest.raises(ValueError):
        gen.main(str(tmp_path / "in.csv"), table=table)


def test_revert_sql_for_the_default_table_is_the_committed_revert_file(capsys):
    assert gen.cli(["--revert"]) == 0
    assert capsys.readouterr().out == (REPO / "scripts" / "oci" / "fix_kmib_created_at_revert.sql").read_text(encoding="utf-8")


def test_revert_sql_for_another_table_only_changes_the_table_name(capsys):
    assert gen.cli(["--revert", "--table", "news_raw_created_at_fix_2"]) == 0
    sql = capsys.readouterr().out
    committed = (REPO / "scripts" / "oci" / "fix_kmib_created_at_revert.sql").read_text(encoding="utf-8")
    assert "news_raw_kmib_created_at_fix" not in sql
    assert sql == committed.replace("news_raw_kmib_created_at_fix", "news_raw_created_at_fix_2")
    assert "DROP TABLE news_raw_created_at_fix_2;" in sql and "to_regclass('news_raw_created_at_fix_2')" in sql


def test_revert_needs_no_csv_but_a_fix_run_does(capsys):
    assert gen.cli(["--revert"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit) as stop:
        gen.cli([])
    assert stop.value.code == 2


def test_main_keeps_its_one_argument_form(tmp_path, capsys):
    rc, sql, _ = _run(tmp_path, [ROW], capsys)
    assert rc == 0 and "CREATE TABLE news_raw_kmib_created_at_fix (" in sql
