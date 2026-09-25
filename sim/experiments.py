"""Metric-validity grid behind ADR 0019: toy policies x click-model presets x seeds.

    python -m sim.experiments --out-dir out/sim_grid --workers 6

The policies are fake-app test doubles with known behavior; the grid checks that
each metric moves in the direction the policy design implies (e.g. static batch
-> no within-day reaction). It says nothing about the production recommender.
"""

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List

import numpy as np

from sim.fake_app import POLICIES
from sim.run import main as run_main

KEY_METRICS = (
    ("cold_start", "first_view_coverage"),
    ("cold_start", "first_view_onboarding_category_share"),
    ("reactivity", "after_click_jaccard_mean"),
    ("reactivity", "similar_share_before_click"),
    ("reactivity", "similar_share_after_click"),
    ("reactivity", "similar_share_lift"),
    ("drift", "pre_drift_new_core_share"),
    ("drift", "adapted_rate"),
    ("drift", "requests_to_adapt_median"),
    ("serving", "empty_rate"),
    ("serving", "fallback_rate"),
    ("errors", "error_rate"),
    ("engagement", "ctr_top_k"),
    ("engagement", "sessions_with_click_rate"),
)


def _one(job: dict) -> str:
    out = Path(job["out_dir"]) / f"{job['catalog']}__{job['policy']}__{job['preset']}__s{job['seed']}.json"
    if not out.exists():
        argv = ["--target", "fake", "--policy", job["policy"], "--preset", job["preset"], "--seed", str(job["seed"]),
                "--users", str(job["users"]), "--days", str(job["days"]), "--catalog", job["catalog"], "--out", str(out)]
        if job.get("team_archive_dir"):
            argv += ["--team-archive-dir", job["team_archive_dir"], "--calibrate"]
        run_main(argv)
    return str(out)


def summarize(paths: List[str]) -> Dict[str, dict]:
    groups: Dict[str, List[dict]] = {}
    for p in paths:
        rep = json.loads(Path(p).read_text(encoding="utf-8"))
        key = f"{rep['args']['policy']}/{rep['args']['preset']}"
        groups.setdefault(key, []).append(rep["metrics"])
    summary = {}
    for key, ms in sorted(groups.items()):
        row = {"n_seeds": len(ms)}
        for section, name in KEY_METRICS:
            # .get: reports written before a metric was added simply lack it
            vals = [v for v in (m.get(section, {}).get(name) for m in ms) if v is not None]
            row[f"{section}.{name}"] = (
                {"mean": float(np.mean(vals)), "min": float(np.min(vals)), "max": float(np.max(vals))} if vals else None
            )
        summary[key] = row
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--policies", nargs="+", default=list(POLICIES))
    p.add_argument("--presets", nargs="+", default=["default", "category_only"])
    p.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2])
    p.add_argument("--users", type=int, default=300)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--catalog", default="synthetic", choices=["synthetic", "team_archive"])
    p.add_argument("--team-archive-dir", default=None)
    p.add_argument("--workers", type=int, default=4)
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    jobs = [dict(policy=pol, preset=pre, seed=s, users=args.users, days=args.days, catalog=args.catalog,
                 team_archive_dir=args.team_archive_dir, out_dir=str(args.out_dir))
            for pol in args.policies for pre in args.presets for s in args.seeds]
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        paths = list(ex.map(_one, jobs))
    summary = summarize(paths)
    (args.out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
