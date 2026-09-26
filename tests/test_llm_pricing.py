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


def test_shipped_pricing_file_loads_and_prices_the_incumbent_default():
    from core.llm.registry import ROLE_DEFAULT_MODEL

    table = load_pricing()
    for model in ROLE_DEFAULT_MODEL.values():
        assert table.cost(model, 1_000_000, 0, on=date(2026, 9, 25)) > 0
