"""Position-based click model.

    P(click | item, rank r, user u) = (1 / (r + 1)) ** eta_u * sigmoid(w_u . phi + b)

phi = [category preference, keyword Jaccard (Kiwi nouns), press preference,
       exp(-age / tau), log1p(raw_news_count), repetition]

w_u is the global weight vector with two per-user modulations: the freshness
weight is scaled by 2 * novelty_u and the repetition weight by fatigue_u.

BGE-M3 similarity is deliberately NOT a feature: the recommender under test
ranks with those embeddings, and a simulator that clicks by the same cosine
would grade the recommender with its own answer key. Keyword/category overlap
still correlates with embedding similarity, so content-based rankers remain
structurally favored (ADR 0019).
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Dict, List, Sequence

import numpy as np

from sim.catalog import Item
from sim.personas import Profile
from sim.text import jaccard

FEATURE_NAMES = ("category", "keyword", "press", "freshness", "popularity", "repetition")


@dataclass(frozen=True)
class ClickWeights:
    category: float = 4.0
    keyword: float = 8.0
    press: float = 1.0
    freshness: float = 1.0
    popularity: float = 0.3
    repetition: float = -1.0

    def as_array(self) -> np.ndarray:
        return np.array([self.category, self.keyword, self.press, self.freshness, self.popularity, self.repetition])


@dataclass(frozen=True)
class ClickModelConfig:
    weights: ClickWeights = ClickWeights()
    bias: float = -4.0
    tau_hours: float = 24.0
    # A prior click counts as this many extra exposures in the repetition feature:
    # re-clicking an already-read newsletter should be rarer than re-seeing it.
    reclick_weight: float = 3.0
    view_depth: int = 20
    name: str = "custom"


# Biases were fitted with sim.calibration.calibrate_bias on sim.reference (300
# profiles, synthetic catalog) to a 2% random-ranking CTR; see ADR 0019.
# tests/simulator/test_sim_calibration.py re-runs the fit and fails on drift.
_PRESETS: Dict[str, ClickModelConfig] = {
    "default": ClickModelConfig(weights=ClickWeights(), bias=-5.018, name="default"),
    "category_only": ClickModelConfig(weights=ClickWeights(keyword=0.0), bias=-4.624, name="category_only"),
}


def preset(name: str) -> ClickModelConfig:
    try:
        return _PRESETS[name]
    except KeyError:
        raise ValueError(f"unknown click-model preset {name!r}; choose from {sorted(_PRESETS)}") from None


def preset_names() -> List[str]:
    return sorted(_PRESETS)


def examination_prob(rank: int, eta: float) -> float:
    return (1.0 / (rank + 1)) ** eta


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


@dataclass
class ExposureHistory:
    exposures: Dict[int, int] = field(default_factory=lambda: defaultdict(int))
    clicks: Dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def record_view(self, items: Sequence[Item]) -> None:
        for it in items:
            self.exposures[it.news_letter_id] += 1

    def record_click(self, item_id: int) -> None:
        self.clicks[item_id] += 1


class ClickModel:
    def __init__(self, cfg: ClickModelConfig):
        self.cfg = cfg
        self._w = cfg.weights.as_array()

    def with_bias(self, bias: float) -> "ClickModel":
        return ClickModel(replace(self.cfg, bias=bias))

    def features(self, item: Item, profile: Profile, now: datetime, hist: ExposureHistory) -> np.ndarray:
        age_h = max(0.0, (now - item.created_at).total_seconds() / 3600.0)
        nid = item.news_letter_id
        return np.array([
            profile.pref_for(item.category_id),
            jaccard(item.noun_set, profile.keyword_nouns),
            profile.press_for(item.press_names),
            math.exp(-age_h / self.cfg.tau_hours),
            math.log1p(max(item.raw_news_count, 0)),
            math.log1p(hist.exposures.get(nid, 0) + self.cfg.reclick_weight * hist.clicks.get(nid, 0)),
        ])

    def user_weights(self, profile: Profile) -> np.ndarray:
        w = self._w.copy()
        w[3] *= 2.0 * profile.novelty
        w[5] *= profile.fatigue
        return w

    def attractiveness(self, item: Item, profile: Profile, now: datetime, hist: ExposureHistory) -> float:
        z = float(self.user_weights(profile) @ self.features(item, profile, now, hist)) + self.cfg.bias
        return _sigmoid(z)

    def click_prob(self, item: Item, rank: int, profile: Profile, now: datetime, hist: ExposureHistory) -> float:
        return examination_prob(rank, profile.eta) * self.attractiveness(item, profile, now, hist)

    def list_probs(self, items: Sequence[Item], profile: Profile, now: datetime, hist: ExposureHistory) -> List[float]:
        shown = items[: self.cfg.view_depth]
        return [self.click_prob(it, r, profile, now, hist) for r, it in enumerate(shown)]

    def sample_clicks(
        self, items: Sequence[Item], profile: Profile, now: datetime, hist: ExposureHistory, rng: np.random.Generator
    ) -> List[int]:
        """Independent Bernoulli draw per examined rank; returns clicked indices."""
        probs = self.list_probs(items, profile, now, hist)
        if not probs:
            return []
        u = rng.random(len(probs))
        return [i for i, (p, x) in enumerate(zip(probs, u)) if x < p]

    def choose_top(
        self, items: Sequence[Item], profile: Profile, now: datetime, hist: ExposureHistory,
        rng: np.random.Generator, k: int,
    ) -> List[int]:
        """Noisy forced choice of k items (onboarding picks, load-test click task).

        Gumbel-top-k on log-attractiveness = Plackett-Luce sampling without
        replacement; position is ignored because the UI asks the user to choose.
        """
        if not items or k <= 0:
            return []
        logits = np.log([max(self.attractiveness(it, profile, now, hist), 1e-12) for it in items])
        g = logits + rng.gumbel(size=len(items))
        return [int(i) for i in np.argsort(-g)[:k]]
