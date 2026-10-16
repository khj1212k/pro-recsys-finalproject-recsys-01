import sys
import os
from datetime import date

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from core.llm.pricing import PriceTable, load_pricing


SAMPLE = {
    "currency": "USD",
    "models": {
        "cheap": {
            "provider": "p",
            "prices": [
                {"input_per_1m": 0.5, "output_per_1m": 2.0, "source": "https://example.com/a", "accessed": "2026-09-25"},
            ],
        },
        "scheduled": {
            "provider": "p",
            "prices": [
                {"input_per_1m": 1.0, "output_per_1m": 4.0, "until": "2026-12-31",
                 "source": "https://example.com/b", "accessed": "2026-09-25"},
                {"input_per_1m": 2.0, "output_per_1m": 8.0, "from": "2027-01-01",
                 "source": "https://example.com/b", "accessed": "2026-09-25"},
            ],
        },
    },
}


def test_cost_is_tokens_times_per_million_price():
    table = PriceTable.from_dict(SAMPLE)
    assert table.cost("cheap", 1_000_000, 500_000) == pytest.approx(0.5 + 1.0)


def test_scheduled_price_change_is_applied_by_date():
    table = PriceTable.from_dict(SAMPLE)
    assert table.cost("scheduled", 1_000_000, 0, on=date(2026, 12, 31)) == pytest.approx(1.0)
    assert table.cost("scheduled", 1_000_000, 0, on=date(2027, 1, 1)) == pytest.approx(2.0)


def test_unknown_model_fails_loudly_instead_of_costing_zero():
    table = PriceTable.from_dict(SAMPLE)
    with pytest.raises(KeyError):
        table.cost("missing", 10, 10)


def test_every_price_entry_must_cite_a_source_and_access_date():
    bad = {"currency": "USD", "models": {"x": {"provider": "p", "prices": [{"input_per_1m": 1, "output_per_1m": 1}]}}}
    with pytest.raises(ValueError):
        PriceTable.from_dict(bad)


def _table(prices, currency="USD"):
    cited = [{"source": "https://example.com/p", "accessed": "2026-10-06", **p} for p in prices]
    return {"currency": currency, "models": {"m": {"provider": "p", "prices": cited}}}


@pytest.mark.parametrize("bad", [
    {"input_per_1m": 0, "output_per_1m": 2.5},      # 0원: 예약이 0이라 상한이 걸리지 않는다
    {"input_per_1m": 0.3, "output_per_1m": 0},
    {"input_per_1m": -0.3, "output_per_1m": 2.5},
    {"input_per_1m": float("nan"), "output_per_1m": 2.5},
    {"input_per_1m": float("inf"), "output_per_1m": 2.5},
    {"input_per_1m": "three", "output_per_1m": 2.5},
    {"input_per_1m": True, "output_per_1m": 2.5},
    {"output_per_1m": 2.5},
])
def test_price_must_be_a_positive_finite_number(bad):
    with pytest.raises(ValueError):
        PriceTable.from_dict(_table([bad]))


def test_per_thousand_price_written_in_the_per_million_column_is_rejected():
    """$0.30/1M을 1K 단위($0.0003)로 적으면 비용이 1000배 적게 잡힌다 - 타당성 하한으로 잡는다."""
    with pytest.raises(ValueError, match="1K 토큰당"):
        PriceTable.from_dict(_table([{"input_per_1m": 0.0003, "output_per_1m": 0.0025}]))
    with pytest.raises(ValueError, match="1K 토큰당"):
        PriceTable.from_dict(_table([{"input_per_1m": 0.30, "output_per_1m": 0.0025}]))


def test_price_below_the_floor_needs_an_explicit_flag_on_that_entry():
    table = PriceTable.from_dict(_table([{"input_per_1m": 0.005, "output_per_1m": 0.02, "allow_below_floor": True}]))

    assert table.cost("m", 1_000_000, 1_000_000) == pytest.approx(0.025)
    with pytest.raises(ValueError):
        PriceTable.from_dict(_table([{"input_per_1m": 0.005, "output_per_1m": 0.02, "allow_below_floor": "yes"}]))


def test_currency_other_than_usd_is_rejected():
    """가드는 단가를 USD로 읽는다. KRW 표를 그대로 계산하면 상한의 뜻이 달라진다."""
    with pytest.raises(ValueError, match="USD"):
        PriceTable.from_dict(_table([{"input_per_1m": 420, "output_per_1m": 3500}], currency="KRW"))


@pytest.mark.parametrize("prices", [
    # until을 달지 않은 옛 가격이 새 가격을 가린다
    [{"input_per_1m": 1.0, "output_per_1m": 4.0}, {"input_per_1m": 2.0, "output_per_1m": 8.0, "from": "2027-01-01"}],
    # 하루가 겹친다
    [{"input_per_1m": 1.0, "output_per_1m": 4.0, "until": "2027-01-01"},
     {"input_per_1m": 2.0, "output_per_1m": 8.0, "from": "2027-01-01"}],
    # 기간 없는 항목 둘
    [{"input_per_1m": 1.0, "output_per_1m": 4.0}, {"input_per_1m": 2.0, "output_per_1m": 8.0}],
])
def test_overlapping_price_windows_are_rejected(prices):
    with pytest.raises(ValueError, match="겹치는"):
        PriceTable.from_dict(_table(prices))


def test_price_window_that_ends_before_it_starts_is_rejected():
    with pytest.raises(ValueError):
        PriceTable.from_dict(_table([{"input_per_1m": 1.0, "output_per_1m": 4.0, "from": "2027-01-01", "until": "2026-12-31"}]))


def test_shipped_pricing_file_loads_and_prices_the_incumbent_default():
    from core.llm.registry import ROLE_DEFAULT_MODEL

    table = load_pricing()
    for model in ROLE_DEFAULT_MODEL.values():
        assert table.cost(model, 1_000_000, 0, on=date(2026, 9, 25)) > 0
