import pytest

from evaluation.llm.faithfulness import (
    check_against_sources,
    compare_rewrite,
    extract_facts,
)


# ---------------------------------------------------------------------------
# Number extraction / normalization
# ---------------------------------------------------------------------------

class TestNumberExtraction:
    def test_trillion_and_hundred_million_combo(self):
        facts = extract_facts("삼성전자는 3조2000억원의 매출을 올렸다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(3.2e12, "KRW")]

    def test_hundred_million_and_ten_thousand_combo(self):
        facts = extract_facts("이번 분기 매출은 1억 2천만 원이었다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(1.2e8, "KRW")]

    def test_comma_grouped_scaled_number(self):
        facts = extract_facts("거래 규모는 1,200억 수준이다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(1.2e11, "")]

    def test_percent(self):
        facts = extract_facts("성장률은 3.5%로 집계됐다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(3.5, "%")]

    def test_percentage_point_symbol(self):
        facts = extract_facts("물가상승률이 2.3%p 상승했다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(2.3, "%p")]

    def test_percentage_point_word_normalizes_same_as_symbol(self):
        facts = extract_facts("실업률이 2.3퍼센트포인트 낮아졌다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(2.3, "%p")]

    def test_person_count_with_scale(self):
        facts = extract_facts("참가자는 10만 명을 넘었다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(100000.0, "명")]

    def test_bare_number_requires_unit_case(self):
        facts = extract_facts("올해 신고 건수는 300건이다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(300.0, "건")]

    def test_multiplier_word(self):
        facts = extract_facts("가격이 2배로 뛰었다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(2.0, "배")]

    def test_basis_points(self):
        facts = extract_facts("금리가 50bp 인상됐다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(50.0, "bp")]

    def test_dollar_billion(self):
        facts = extract_facts("투자 규모는 $1.2 billion 이다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(1.2e9, "USD")]

    def test_won_won_won_dollar_normalizes_to_same_value_as_dollar_billion(self):
        facts = extract_facts("해당 기업은 12억 달러를 유치했다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(1.2e9, "USD")]

    def test_ten_thousand_plus_bare_remainder(self):
        facts = extract_facts("상품권 1만2000원이 지급된다.")
        assert [(n.value, n.unit) for n in facts.numbers] == [(12000.0, "KRW")]


# ---------------------------------------------------------------------------
# False-positive traps: things that must NOT be extracted as numeric claims
# ---------------------------------------------------------------------------

class TestFalsePositiveTraps:
    def test_year_is_not_a_number(self):
        facts = extract_facts("2026년에 새로운 정책이 시행된다.")
        assert facts.numbers == []

    def test_list_numbering_is_not_a_number(self):
        facts = extract_facts("1. 첫 번째 안건입니다.")
        assert facts.numbers == []

    def test_phone_like_string_is_not_a_number(self):
        facts = extract_facts("문의는 010-1234-5678로 연락주세요.")
        assert facts.numbers == []

    def test_company_count_counter_is_not_a_number(self):
        # 반도체 3사: 사(社) is a bound counter, not a unit in _UNIT_MAP --
        # deliberately excluded, see faithfulness.py's comment near NUMBER_RE.
        facts = extract_facts("반도체 3사 중 하나로 꼽힌다.")
        assert facts.numbers == []

    def test_bare_duration_year_is_not_a_number(self):
        facts = extract_facts("3년 연속 매출이 증가했다.")
        assert facts.numbers == []

    def test_bare_number_without_unit_is_not_extracted(self):
        facts = extract_facts("참여율은 1,200 수준이다.")
        assert facts.numbers == []


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

class TestDateExtraction:
    def test_full_date_normalizes(self):
        facts = extract_facts("2026년 3월 5일에 발표됐다.")
        assert len(facts.dates) == 1
        d = facts.dates[0]
        assert (d.year, d.month, d.day, d.relative, d.normalized) == (2026, 3, 5, False, "2026-03-05")

    def test_year_month_normalizes(self):
        facts = extract_facts("2026년 3월에 시행 예정이다.")
        d = facts.dates[0]
        assert (d.year, d.month, d.day, d.relative, d.normalized) == (2026, 3, None, False, "2026-03")

    def test_bare_year_normalizes(self):
        facts = extract_facts("2026년에 새로운 정책이 시행된다.")
        d = facts.dates[0]
        assert (d.year, d.month, d.day, d.relative, d.normalized) == (2026, None, None, False, "2026")

    def test_month_day_without_year_not_resolved(self):
        facts = extract_facts("3월 5일 회의가 열린다.")
        d = facts.dates[0]
        assert (d.year, d.month, d.day, d.relative, d.normalized) == (None, 3, 5, False, None)

    @pytest.mark.parametrize("word", ["오늘", "어제", "지난주"])
    def test_relative_expressions_are_flagged_not_resolved(self, word):
        facts = extract_facts(f"{word} 발표가 있었다.")
        assert len(facts.dates) == 1
        d = facts.dates[0]
        assert d.relative is True
        assert d.normalized is None
        assert d.surface == word

    def test_numbers_inside_a_date_are_not_also_counted_as_numbers(self):
        facts = extract_facts("2026년 3월 5일에 발표됐다.")
        assert facts.numbers == []


# ---------------------------------------------------------------------------
# Named entity extraction (Kiwi NNP / SL / SH)
# ---------------------------------------------------------------------------

class TestEntityExtraction:
    def test_simple_proper_noun(self):
        facts = extract_facts("삼성전자는 오늘 발표했다.")
        assert [(e.surface, e.tag) for e in facts.entities] == [("삼성전자", "NNP")]

    def test_adjacent_sl_and_nnp_tokens_merge_into_one_entity(self):
        facts = extract_facts("SK하이닉스가 신제품을 공개했다.")
        assert [(e.surface, e.tag) for e in facts.entities] == [("SK하이닉스", "SL")]

    def test_latin_entity_split_from_trailing_digit_by_kiwi(self):
        facts = extract_facts("GPT-4 모델이 공개됐다.")
        surfaces = [e.surface for e in facts.entities]
        assert "GPT" in surfaces

    def test_single_char_proper_noun_is_filtered_out(self):
        facts = extract_facts("이 대표가 발언했다.")
        assert facts.entities == []

    def test_single_char_latin_entity_is_filtered_out(self):
        facts = extract_facts("K 방역이 주목받았다.")
        assert facts.entities == []

    def test_two_separate_entities_are_not_merged_across_whitespace(self):
        facts = extract_facts("미국 연준이 금리를 동결했다.")
        assert [e.surface for e in facts.entities] == ["미국", "연준"]


# ---------------------------------------------------------------------------
# Quote extraction
# ---------------------------------------------------------------------------

class TestQuoteExtraction:
    def test_straight_double_quotes(self):
        facts = extract_facts('이재용 회장은 "성장을 지속하겠다"고 말했다.')
        assert [q.surface for q in facts.quotes] == ["성장을 지속하겠다"]

    def test_curly_double_quotes(self):
        facts = extract_facts("이재용 회장은 “성장을 지속하겠다”고 말했다.")
        assert [q.surface for q in facts.quotes] == ["성장을 지속하겠다"]

    def test_curly_single_quotes(self):
        facts = extract_facts("대표는 ‘혁신을 이어가겠다’고 밝혔다.")
        assert [q.surface for q in facts.quotes] == ["혁신을 이어가겠다"]

    def test_straight_single_quotes(self):
        facts = extract_facts("대표는 '혁신을 이어가겠다'고 밝혔다.")
        assert [q.surface for q in facts.quotes] == ["혁신을 이어가겠다"]

    def test_multiple_quotes_in_one_text(self):
        text = '그는 "첫 번째 발언"이라 했고, 이어 "두 번째 발언"이라 덧붙였다.'
        facts = extract_facts(text)
        assert [q.surface for q in facts.quotes] == ["첫 번째 발언", "두 번째 발언"]

    def test_no_quotes_returns_empty_list(self):
        facts = extract_facts("오늘 발표가 있었다.")
        assert facts.quotes == []


# ---------------------------------------------------------------------------
# check_against_sources
# ---------------------------------------------------------------------------

class TestCheckAgainstSources:
    def test_exact_number_match_is_supported(self):
        report = check_against_sources(
            "매출은 500억원이었다.", ["업계 자료에 따르면 매출은 500억원이었다."]
        )
        assert report.number_exact == 1
        assert report.unsupported_numbers == []
        assert report.passed is True

    def test_rounded_number_counts_as_approx_and_still_passes(self):
        report = check_against_sources(
            "올해 매출은 3.21조원을 기록했다.", ["업계 자료에 따르면 매출은 3.2조원 수준이었다."]
        )
        assert report.number_exact == 0
        assert report.number_approx == 1
        assert report.unsupported_numbers == []
        assert report.passed is True

    def test_unsupported_number_fails_by_default(self):
        report = check_against_sources("매출은 500억원이었다.", ["매출은 300억원이었다."])
        assert len(report.unsupported_numbers) == 1
        assert report.unsupported_numbers[0]["surface"] == "500억원"
        assert report.passed is False

    def test_same_value_different_unit_does_not_match(self):
        report = check_against_sources("성장률은 3.5%를 기록했다.", ["3.5명이 참여했다."])
        assert len(report.unsupported_numbers) == 1
        assert report.passed is False

    def test_unit_less_scaled_number_matches_currency_source(self):
        # "1,200억" carries no explicit unit word but is currency-like, so it
        # is allowed to match a KRW-tagged figure in the source.
        report = check_against_sources(
            "시장 규모는 1,200억 수준이다.",
            ["업계 보고서에 따르면 시장 규모는 1,200억원으로 집계됐다."],
        )
        assert report.number_exact == 1
        assert report.unsupported_numbers == []

    def test_unitless_number_does_not_match_usd_source(self):
        # "12억" (unit-less) must not stand in for a USD-denominated source
        # figure of the same bare magnitude -- cross-currency, no conversion.
        report = check_against_sources(
            "규모는 12억 수준이다.", ["해당 기업은 12억 달러를 유치했다."]
        )
        assert len(report.unsupported_numbers) == 1
        assert report.passed is False

    def test_usd_number_does_not_match_unitless_source(self):
        # same bug, opposite direction: an explicit USD figure in the
        # generated text must not match a unit-less source figure.
        report = check_against_sources(
            "해당 기업은 12억 달러를 유치했다.", ["규모는 12억 수준이다."]
        )
        assert len(report.unsupported_numbers) == 1
        assert report.passed is False

    def test_krw_number_does_not_match_usd_source(self):
        # two explicit currencies must never cross-match, even at the same
        # bare numeric value.
        report = check_against_sources(
            "매출은 12억원이었다.", ["매출은 12억 달러였다."]
        )
        assert len(report.unsupported_numbers) == 1
        assert report.passed is False

    def test_entity_supported_via_fuzzy_match(self):
        report = check_against_sources(
            "삼성전자가 발표했다.", ["삼성전자(005930)가 실적을 공개했다."]
        )
        assert report.unsupported_entities == []
        assert report.passed is True

    def test_entity_unsupported_when_absent_from_sources(self):
        report = check_against_sources("네이버가 발표했다.", ["카카오가 실적을 공개했다."])
        assert len(report.unsupported_entities) == 1
        assert report.unsupported_entities[0]["surface"] == "네이버"
        assert report.passed is False

    def test_quote_supported_when_paraphrase_close_to_source(self):
        report = check_against_sources(
            '그는 "성장을 지속하겠다"고 말했다.', ["그는 인터뷰에서 성장을 지속하겠다고 밝혔다."]
        )
        assert report.unsupported_quotes == []
        assert report.passed is True

    def test_quote_unsupported_when_fabricated(self):
        report = check_against_sources(
            '그는 "완전히 다른 말"이라고 말했다.', ["그는 성장을 지속하겠다고 밝혔다."]
        )
        assert len(report.unsupported_quotes) == 1
        assert report.passed is False

    def test_thresholds_are_configurable(self):
        report = check_against_sources(
            "매출은 500억원이었다.", ["매출은 300억원이었다."], max_unsupported_numbers=1
        )
        assert len(report.unsupported_numbers) == 1
        assert report.passed is True

    def test_no_sources_flags_everything_unsupported(self):
        report = check_against_sources("매출은 500억원이었다.", [])
        assert len(report.unsupported_numbers) == 1
        assert report.passed is False

    def test_report_to_dict_has_expected_shape(self):
        report = check_against_sources("매출은 500억원이었다.", ["매출은 500억원이었다."])
        d = report.to_dict()
        assert set(d.keys()) == {
            "passed",
            "number_total",
            "number_exact",
            "number_approx",
            "unsupported_numbers",
            "entity_total",
            "unsupported_entities",
            "quote_total",
            "unsupported_quotes",
            "thresholds",
        }
        assert d["passed"] is True


# ---------------------------------------------------------------------------
# compare_rewrite (tone-conversion drift)
# ---------------------------------------------------------------------------

class TestCompareRewrite:
    def test_identical_text_has_no_drift(self):
        text = "삼성전자는 1000억원의 매출을 올렸다."
        report = compare_rewrite(text, text)
        assert report.has_drift is False
        assert report.added_numbers == []
        assert report.dropped_numbers == []

    def test_casual_rephrasing_with_same_facts_has_no_drift(self):
        original = "삼성전자는 1000억원의 매출을 기록했다."
        rewritten = "삼성전자는 1000억원의 매출을 냈어요!"
        report = compare_rewrite(original, rewritten)
        assert report.has_drift is False

    def test_changed_number_is_both_added_and_dropped(self):
        original = "삼성전자는 1000억원의 매출을 올렸다."
        rewritten = "삼성전자는 1200억원의 매출을 올렸어요."
        report = compare_rewrite(original, rewritten)
        assert [n["value"] for n in report.added_numbers] == [1.2e11]
        assert [n["value"] for n in report.dropped_numbers] == [1.0e11]
        assert report.has_drift is True

    def test_relative_date_with_same_surface_is_not_drift(self):
        report = compare_rewrite("오늘 발표가 있었다.", "오늘 발표가 있었어요.")
        assert report.added_dates == []
        assert report.dropped_dates == []

    def test_absolute_date_preserved_across_rewrite(self):
        original = "2026년 3월 5일 발표됐다."
        rewritten = "2026년 3월 5일에 발표됐어요!"
        report = compare_rewrite(original, rewritten)
        assert report.added_dates == []
        assert report.dropped_dates == []

    def test_entity_dropped_in_rewrite(self):
        original = "삼성전자와 네이버가 협력한다."
        rewritten = "삼성전자가 협력한다."
        report = compare_rewrite(original, rewritten)
        assert [e["surface"] for e in report.dropped_entities] == ["네이버"]
        assert report.added_entities == []
        assert report.has_drift is True

    def test_entity_added_in_rewrite_is_flagged_as_hallucination_risk(self):
        original = "삼성전자가 발표했다."
        rewritten = "삼성전자와 카카오가 발표했어요."
        report = compare_rewrite(original, rewritten)
        assert [e["surface"] for e in report.added_entities] == ["카카오"]
        assert report.dropped_entities == []

    def test_drift_report_to_dict_has_expected_shape(self):
        report = compare_rewrite("삼성전자가 발표했다.", "삼성전자가 발표했다.")
        d = report.to_dict()
        assert set(d.keys()) == {
            "added_numbers",
            "dropped_numbers",
            "added_dates",
            "dropped_dates",
            "added_entities",
            "dropped_entities",
            "has_drift",
        }
