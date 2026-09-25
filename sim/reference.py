"""The fixed setup the shipped preset biases were calibrated on."""

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Tuple

from sim.catalog import Catalog, synthetic_catalog
from sim.personas import PopulationConfig, Profile, generate_population

REFERENCE_START = datetime(2026, 1, 5, tzinfo=timezone.utc)
REFERENCE_NOW = REFERENCE_START + timedelta(hours=12)


@lru_cache(maxsize=1)
def reference_profiles() -> Tuple[Profile, ...]:
    return tuple(u.profile for u in generate_population(PopulationConfig(n_users=300, seed=0)))


@lru_cache(maxsize=1)
def reference_catalog() -> Catalog:
    return synthetic_catalog(n_days=7, seed=0, start=REFERENCE_START)
