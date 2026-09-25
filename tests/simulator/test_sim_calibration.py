from dataclasses import replace
from datetime import timedelta

import pandas as pd
import pytest

from sim.calibration import CalibrationTarget, calibrate_bias, ebnerd_base_rate
from sim.catalog import synthetic_catalog
from sim.click_model import preset
from sim.personas import PopulationConfig, generate_population
from sim.reference import REFERENCE_NOW, reference_catalog, reference_profiles


@pytest.fixture(scope="module")
def ref():
    return reference_profiles(), reference_catalog()


@pytest.mark.parametrize("name", ["default", "category_only"])
def test_preset_bias_matches_calibration_routine(ref, name):
    """The shipped biases must be what calibrate_bias produces on the reference setup."""
    profiles, catalog = ref
    cfg = preset(name)
    result = calibrate_bias(cfg, profiles, catalog, REFERENCE_NOW)
    assert result.random_ctr == pytest.approx(CalibrationTarget().random_ctr, rel=1e-3)
    assert cfg.bias == pytest.approx(result.bias, abs=0.02)
    assert result.oracle_in_range


def test_calibration_hits_other_targets_and_oracle_beats_random(ref):
    profiles, catalog = ref
    target = CalibrationTarget(random_ctr=0.05)
    r = calibrate_bias(preset("default"), profiles[:60], catalog, REFERENCE_NOW, target=target)
    assert r.random_ctr == pytest.approx(0.05, rel=1e-3)
    assert r.oracle_ctr > r.random_ctr


def test_calibration_is_deterministic(ref):
    profiles, catalog = ref
    a = calibrate_bias(preset("default"), profiles[:50], catalog, REFERENCE_NOW, seed=1)
    b = calibrate_bias(preset("default"), profiles[:50], catalog, REFERENCE_NOW, seed=1)
    assert a.bias == b.bias


def test_calibration_rejects_too_few_candidates(ref):
    profiles, catalog = ref
    with pytest.raises(ValueError):
        calibrate_bias(preset("default"), profiles[:5], catalog, REFERENCE_NOW - timedelta(days=30))


def test_ebnerd_base_rate_aggregates_inview_and_clicks():
    df = pd.DataFrame({
        "article_ids_inview": [[1, 2, 3, 4], [5, 6], [7, 8, 9, 10, 11, 12, 13, 14, 15, 16]],
        "article_ids_clicked": [[2], [5, 6], [9]],
    })
    r = ebnerd_base_rate(df)
    assert r["impressions"] == 3
    assert r["pooled_ctr"] == pytest.approx(4 / 16)
    assert r["mean_impression_ctr"] == pytest.approx((1 / 4 + 2 / 2 + 1 / 10) / 3)
    assert r["share_impressions_with_click"] == 1.0
    assert r["median_inview"] == 4
