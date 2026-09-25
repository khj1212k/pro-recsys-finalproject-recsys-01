import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from sim.catalog import CATEGORY_CODES, PRESSES, Item
from sim.click_model import (
    FEATURE_NAMES,
    ClickModel,
    ClickModelConfig,
    ClickWeights,
    ExposureHistory,
    examination_prob,
    preset,
)
from sim.personas import Profile

NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


def make_profile(**overrides):
    cat = [0.0] * len(CATEGORY_CODES)
    cat[CATEGORY_CODES.index(200)] = 0.7
    cat[CATEGORY_CODES.index(300)] = 0.3
    press = [0.0] * len(PRESSES)
    press[PRESSES.index("매일경제")] = 1.0
    base = dict(
        archetype="investor",
        category_pref=tuple(cat),
        keywords=("반도체", "금리"),
        press_pref=tuple(press),
        sessions_per_day=3,
        eta=1.0,
        novelty=0.5,
        fatigue=1.0,
    )
    base.update(overrides)
    return Profile(**base)


def make_item(nid=1, category=200, keywords=("반도체", "수출"), age_h=0.0, raw=3, press=("매일경제",)):
    return Item(
        news_letter_id=nid,
        title="",
        sentence="",
        keywords=tuple(keywords),
        created_at=NOW - timedelta(hours=age_h),
        raw_news_count=raw,
        category_id=category,
        press_names=tuple(press),
    )


def test_examination_prob_is_power_law_of_rank():
    assert examination_prob(0, 1.0) == 1.0
    assert examination_prob(4, 1.0) == pytest.approx(1 / 5)
    assert examination_prob(4, 2.0) == pytest.approx(1 / 25)
    assert examination_prob(9, 0.0) == 1.0
    probs = [examination_prob(r, 0.8) for r in range(20)]
    assert all(a > b for a, b in zip(probs, probs[1:]))


def test_features_match_definitions():
    cfg = ClickModelConfig(tau_hours=24.0, reclick_weight=3.0)
    model = ClickModel(cfg)
    profile = make_profile()
    item = make_item(age_h=24.0, raw=3)
    hist = ExposureHistory()
    hist.exposures[1] = 2
    hist.clicks[1] = 1

    phi = model.features(item, profile, NOW, hist)

    assert FEATURE_NAMES == ("category", "keyword", "press", "freshness", "popularity", "repetition")
    assert phi[0] == pytest.approx(0.7)
    # item nouns {반도체, 수출} vs persona nouns {반도체, 금리} -> 1/3
    assert phi[1] == pytest.approx(1 / 3)
    assert phi[2] == pytest.approx(1.0)
    assert phi[3] == pytest.approx(math.exp(-1.0))
    assert phi[4] == pytest.approx(math.log1p(3))
    assert phi[5] == pytest.approx(math.log1p(2 + 3.0 * 1))


def test_unknown_press_and_category_are_neutral():
    model = ClickModel(ClickModelConfig())
    phi = model.features(make_item(category=None, press=()), make_profile(), NOW, ExposureHistory())
    assert phi[0] == pytest.approx(1 / len(CATEGORY_CODES))
    assert phi[2] == 0.0


def test_future_timestamps_do_not_exceed_full_freshness():
    model = ClickModel(ClickModelConfig())
    phi = model.features(make_item(age_h=-5.0), make_profile(), NOW, ExposureHistory())
    assert phi[3] == pytest.approx(1.0)


def test_click_prob_is_examination_times_sigmoid_with_user_modulated_weights():
    w = ClickWeights(category=2.0, keyword=10.0, press=1.0, freshness=1.5, popularity=0.3, repetition=-1.0)
    cfg = ClickModelConfig(weights=w, bias=-2.5, tau_hours=24.0)
    model = ClickModel(cfg)
    profile = make_profile(eta=1.3, novelty=0.25, fatigue=2.0)
    item = make_item(age_h=12.0)
    hist = ExposureHistory()
    hist.exposures[1] = 1
    phi = model.features(item, profile, NOW, hist)

    # novelty scales the freshness weight by 2*novelty, fatigue scales repetition
    wu = np.array([2.0, 10.0, 1.0, 1.5 * 2 * 0.25, 0.3, -1.0 * 2.0])
    expected = (1 / 4) ** 1.3 / (1 + math.exp(-(wu @ phi - 2.5)))

    assert model.click_prob(item, 3, profile, NOW, hist) == pytest.approx(expected)


def test_category_only_preset_ignores_keyword_overlap():
    model = ClickModel(preset("category_only"))
    profile = make_profile()
    overlap = make_item(keywords=("반도체", "금리"))
    disjoint = make_item(keywords=("야구", "축구"))
    p1 = model.click_prob(overlap, 0, profile, NOW, ExposureHistory())
    p2 = model.click_prob(disjoint, 0, profile, NOW, ExposureHistory())
    assert p1 == pytest.approx(p2)

    default = ClickModel(preset("default"))
    assert default.click_prob(overlap, 0, profile, NOW, ExposureHistory()) > default.click_prob(
        disjoint, 0, profile, NOW, ExposureHistory()
    )


def test_repetition_lowers_click_probability():
    model = ClickModel(ClickModelConfig())
    profile = make_profile()
    item = make_item()
    fresh = model.click_prob(item, 0, profile, NOW, ExposureHistory())
    hist = ExposureHistory()
    hist.record_view([item])
    hist.record_view([item])
    assert model.click_prob(item, 0, profile, NOW, hist) < fresh


def test_sampling_is_seeded_and_follows_position_bias():
    cfg = ClickModelConfig(weights=ClickWeights(0, 0, 0, 0, 0, 0), bias=0.0)  # sigmoid(0)=0.5
    model = ClickModel(cfg)
    profile = make_profile(eta=1.0)
    items = [make_item(nid=i) for i in range(5)]

    def run(seed):
        rng = np.random.default_rng(seed)
        return [model.sample_clicks(items, profile, NOW, ExposureHistory(), rng) for _ in range(4000)]

    a, b = run(7), run(7)
    assert a == b
    assert run(8) != a

    counts = np.zeros(5)
    for clicked in a:
        for idx in clicked:
            counts[idx] += 1
    rates = counts / len(a)
    assert rates[0] == pytest.approx(0.5, abs=0.03)
    assert rates[4] == pytest.approx(0.5 / 5, abs=0.02)


def test_view_depth_truncates_the_examined_list():
    cfg = replace(ClickModelConfig(weights=ClickWeights(0, 0, 0, 0, 0, 0), bias=5.0), view_depth=3)
    model = ClickModel(cfg)
    items = [make_item(nid=i) for i in range(10)]
    probs = model.list_probs(items, make_profile(eta=0.0), NOW, ExposureHistory())
    assert len(probs) == 3
