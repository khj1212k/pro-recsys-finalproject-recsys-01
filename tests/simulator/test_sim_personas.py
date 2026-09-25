from collections import Counter

import numpy as np
import pytest

from sim.catalog import CATEGORY_CODES
from sim.personas import ARCHETYPE_BY_NAME, ARCHETYPES, PopulationConfig, generate_population


def test_archetypes_are_well_formed():
    assert 8 <= len(ARCHETYPES) <= 12
    assert len({a.name for a in ARCHETYPES}) == len(ARCHETYPES)
    for a in ARCHETYPES:
        assert set(a.category_mix) <= set(CATEGORY_CODES)
        assert sum(a.category_mix.values()) == pytest.approx(1.0)
        assert a.sessions_per_day in {1, 3, 6}
        assert a.core_categories <= set(a.category_mix)
        # drift must move the user to categories that were not already core,
        # otherwise "adapted" cannot be told apart from "never changed"
        target = ARCHETYPE_BY_NAME[a.drift_to]
        assert target.name != a.name
        assert not (a.core_categories & target.core_categories)


def test_population_is_deterministic_per_seed():
    cfg = PopulationConfig(n_users=120, seed=3)
    a, b = generate_population(cfg), generate_population(cfg)
    assert [u.profile for u in a] == [u.profile for u in b]
    assert [(u.email, u.join_day, u.drift_day) for u in a] == [(u.email, u.join_day, u.drift_day) for u in b]
    c = generate_population(PopulationConfig(n_users=120, seed=4))
    assert [u.profile for u in a] != [u.profile for u in c]


def test_population_flags_cold_start_drift_and_synthetic_identity():
    cfg = PopulationConfig(n_users=300, seed=0, n_days=7, late_join_frac=0.10, drift_frac=0.10, drift_day=3)
    users = generate_population(cfg)

    assert len(users) == 300
    assert len({u.email for u in users}) == 300
    assert all(u.is_synthetic and u.email.endswith("@sim.invalid") for u in users)
    assert all(len(u.password) >= 8 for u in users)

    late = [u for u in users if u.is_late_joiner]
    assert len(late) == 30
    assert all(1 <= u.join_day < cfg.n_days for u in late)

    drifters = [u for u in users if u.is_drifter]
    assert len(drifters) == 30
    for u in drifters:
        assert u.join_day == 0 and u.drift_day == 3
        before, after = u.profile_on(2), u.profile_on(3)
        assert before is u.profile and after is u.drift_profile
        assert after.archetype == ARCHETYPE_BY_NAME[before.archetype].drift_to

    counts = Counter(u.profile.archetype for u in users)
    assert set(counts) == {a.name for a in ARCHETYPES}


def test_dirichlet_noise_varies_users_but_keeps_archetype_signal():
    users = generate_population(PopulationConfig(n_users=400, seed=1))
    investors = [u.profile for u in users if u.profile.archetype == "investor"]
    prefs = np.array([p.category_pref for p in investors])
    assert np.allclose(prefs.sum(axis=1), 1.0)
    assert prefs.std(axis=0).max() > 0.03  # not identical copies
    econ = CATEGORY_CODES.index(200)
    assert (prefs.argmax(axis=1) == econ).mean() > 0.8


def test_onboarding_categories_come_from_the_profile():
    users = generate_population(PopulationConfig(n_users=50, seed=2))
    for u in users:
        cats = u.profile.top_categories()
        assert 1 <= len(cats) <= 3
        assert cats[0] == CATEGORY_CODES[int(np.argmax(u.profile.category_pref))]
