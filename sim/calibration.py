"""Base-rate calibration of the click model's bias term.

The bias b is solved so that a *uniformly random* top-k ranking gets a target
expected CTR. Anchoring on the random policy keeps the base rate independent of
whichever recommender is later put under test; the oracle policy (rank by the
simulator's own attractiveness) is reported as the ceiling and checked against
a plausible range. Targets and their sources are in ADR 0019.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from sim.catalog import Catalog, Item
from sim.click_model import ClickModel, ClickModelConfig, ExposureHistory, examination_prob
from sim.personas import Profile


@dataclass(frozen=True)
class CalibrationTarget:
    random_ctr: float = 0.02
    oracle_ctr_range: Tuple[float, float] = (0.06, 0.12)
    k: int = 10


@dataclass
class CalibrationResult:
    preset: str
    bias: float
    random_ctr: float
    oracle_ctr: float
    oracle_in_range: bool
    n_profiles: int
    n_candidates: int
    catalog_source: str
    target_random_ctr: float
    oracle_ctr_range: Tuple[float, float]
    k: int

    def to_dict(self) -> dict:
        return asdict(self)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * z))


class _Precomputed:
    """w_u . phi for every (profile, candidate) and exam(r) for every (profile, rank).

    Neither depends on b, so the bisection below is pure numpy.
    """

    def __init__(self, model: ClickModel, profiles: Sequence[Profile], items: Sequence[Item], now: datetime, k: int):
        hist = ExposureHistory()
        self.z = np.empty((len(profiles), len(items)))
        for u, p in enumerate(profiles):
            w = model.user_weights(p)
            for j, it in enumerate(items):
                self.z[u, j] = w @ model.features(it, p, now, hist)
        self.k = min(k, len(items))
        self.exam = np.array([[examination_prob(r, p.eta) for r in range(self.k)] for p in profiles])


def _random_ctr(pre: _Precomputed, idx: np.ndarray, b: float) -> float:
    # idx: [n_profiles, n_lists, k] candidate indices
    z = np.take_along_axis(pre.z[:, None, :], idx, axis=2)
    return float((pre.exam[:, None, :] * _sigmoid(z + b)).mean())


def _oracle_ctr(pre: _Precomputed, b: float) -> float:
    top = -np.sort(-pre.z, axis=1)[:, : pre.k]
    return float((pre.exam * _sigmoid(top + b)).mean())


def calibrate_bias(
    cfg: ClickModelConfig,
    profiles: Sequence[Profile],
    catalog: Catalog,
    now: datetime,
    target: CalibrationTarget = CalibrationTarget(),
    n_lists: int = 5,
    seed: int = 0,
    lo: float = -15.0,
    hi: float = 5.0,
    tol: float = 1e-5,
) -> CalibrationResult:
    items = catalog.candidates(now)
    if len(items) < target.k:
        raise ValueError(f"need at least {target.k} candidates at {now}, got {len(items)}")
    model = ClickModel(cfg)
    pre = _Precomputed(model, profiles, items, now, target.k)
    rng = np.random.default_rng([seed, 31])
    idx = np.stack([
        np.stack([rng.choice(len(items), size=pre.k, replace=False) for _ in range(n_lists)])
        for _ in profiles
    ])
    # expected CTR is strictly increasing in b, so bisection converges
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _random_ctr(pre, idx, mid) < target.random_ctr:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    b = 0.5 * (lo + hi)
    oracle = _oracle_ctr(pre, b)
    lo_r, hi_r = target.oracle_ctr_range
    return CalibrationResult(
        preset=cfg.name,
        bias=b,
        random_ctr=_random_ctr(pre, idx, b),
        oracle_ctr=oracle,
        oracle_in_range=lo_r <= oracle <= hi_r,
        n_profiles=len(profiles),
        n_candidates=len(items),
        catalog_source=catalog.source,
        target_random_ctr=target.random_ctr,
        oracle_ctr_range=target.oracle_ctr_range,
        k=pre.k,
    )


# ---------------------------------------------------------------------------
# EB-NeRD base rate (local, license-restricted data: aggregate numbers only)
# ---------------------------------------------------------------------------

def ebnerd_base_rate(behaviors) -> Dict[str, float]:
    """Aggregate click-through of an EB-NeRD behaviors frame.

    Needs only `article_ids_inview` and `article_ids_clicked`. EB-NeRD releases
    impressions that contain at least one click, so this is an upper bound on
    the unconditional per-item CTR.
    """
    n_inview = behaviors["article_ids_inview"].map(len).to_numpy()
    n_click = behaviors["article_ids_clicked"].map(len).to_numpy()
    return {
        "impressions": int(len(behaviors)),
        "inview_total": int(n_inview.sum()),
        "clicks_total": int(n_click.sum()),
        "pooled_ctr": float(n_click.sum() / n_inview.sum()),
        "mean_impression_ctr": float(np.mean(n_click / n_inview)),
        "share_impressions_with_click": float(np.mean(n_click > 0)),
        "median_inview": float(np.median(n_inview)),
    }


def load_ebnerd_behaviors(split_dir: Path):
    """Reads only the two id-list columns - never article text."""
    import pandas as pd

    return pd.read_parquet(Path(split_dir) / "behaviors.parquet", columns=["article_ids_inview", "article_ids_clicked"])


# ---------------------------------------------------------------------------
# Team synthetic data (the circular LLM-inferred clicks this simulator replaces)
# ---------------------------------------------------------------------------

def team_click_rate(ctr_logs) -> Dict[str, float]:
    """Click rate of the team's synthetic CTR log (user_id, news_letter_id, is_clicked)."""
    per_user = ctr_logs.groupby("user_id")["is_clicked"].agg(["mean", "size"])
    return {
        "impressions": int(len(ctr_logs)),
        "clicks": int(ctr_logs["is_clicked"].sum()),
        "pooled_ctr": float(ctr_logs["is_clicked"].mean()),
        "n_users": int(len(per_user)),
        "impressions_per_user_median": float(per_user["size"].median()),
        "user_ctr_min": float(per_user["mean"].min()),
        "user_ctr_median": float(per_user["mean"].median()),
        "user_ctr_max": float(per_user["mean"].max()),
    }


def main(argv=None) -> int:
    """Base-rate report for ADR 0019: preset calibration + EB-NeRD + team data.

        python -m sim.calibration --ebnerd-dir data/benchmarks/ebnerd/ebnerd_small \\
            --team-ctr-csv data/team_archive/synthetic_dataset/synthetic_ctr_logs.csv

    Prints aggregates only (EB-NeRD is research-licensed; no article fields are read).
    """
    import argparse
    import json

    import pandas as pd

    from sim.click_model import preset, preset_names
    from sim.reference import REFERENCE_NOW, reference_catalog, reference_profiles

    p = argparse.ArgumentParser()
    p.add_argument("--ebnerd-dir", type=Path, default=None, help="EB-NeRD split root (with train/ and validation/)")
    p.add_argument("--team-ctr-csv", type=Path, default=None)
    args = p.parse_args(argv)

    report: Dict[str, object] = {"presets": {}}
    for name in preset_names():
        r = calibrate_bias(preset(name), reference_profiles(), reference_catalog(), REFERENCE_NOW)
        report["presets"][name] = {**r.to_dict(), "shipped_bias": preset(name).bias}
    if args.ebnerd_dir is not None:
        report["ebnerd"] = {split: ebnerd_base_rate(load_ebnerd_behaviors(args.ebnerd_dir / split))
                            for split in ("train", "validation") if (args.ebnerd_dir / split).is_dir()}
    if args.team_ctr_csv is not None:
        report["team_archive"] = team_click_rate(pd.read_csv(args.team_ctr_csv, usecols=["user_id", "is_clicked"]))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
