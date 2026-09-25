"""Run a behavior simulation and write a JSON report.

In-process fake app (no server needed):
    python -m sim.run --target fake --policy reactive --users 300 --days 7 --seed 0 --out out/sim.json

Against a running stack (disposable DB only - this creates users and click logs):
    python -m sim.run --target http://localhost:8000 --users 50 --days 3 \
        --day-end-cmd "docker compose exec -T airflow airflow dags trigger newsletter_ranking" \
        --out out/sim_stack.json
"""

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from typing import Optional

from sim.calibration import CalibrationTarget, calibrate_bias
from sim.catalog import Catalog, synthetic_catalog, team_archive_catalog
from sim.click_model import ClickModel, preset, preset_names
from sim.driver import ApiClient, SimulationConfig, VirtualClock, run_simulation
from sim.metrics import compute_metrics
from sim.personas import PopulationConfig, generate_population
from sim.reference import REFERENCE_START


def build_catalog(kind: str, n_days: int, seed: int, team_archive_dir: Optional[Path]) -> Catalog:
    if kind == "synthetic":
        return synthetic_catalog(n_days=n_days, seed=seed, start=REFERENCE_START)
    if kind == "team_archive":
        if team_archive_dir is None:
            raise SystemExit("--team-archive-dir is required for --catalog team_archive")
        return team_archive_catalog(team_archive_dir / "synthetic_dataset" / "newsletters_export.csv",
                                    team_archive_dir / "derived" / "newsletter_categories.csv",
                                    n_days=n_days, start=REFERENCE_START)
    raise SystemExit(f"unknown catalog {kind!r}")


def simulate_fake(args, users, model, catalog) -> dict:
    from fastapi.testclient import TestClient

    from sim.fake_app import FakeBackend, create_fake_app

    clock = VirtualClock(REFERENCE_START)
    backend = FakeBackend(catalog, policy=args.policy, clock=clock, seed=args.seed)
    with TestClient(create_fake_app(backend)) as client:
        log = run_simulation(
            users,
            make_api=lambda u, calls: ApiClient(client, calls=calls, user_index=u.index),
            model=model,
            cfg=SimulationConfig(n_days=args.days, start=REFERENCE_START, seed=args.seed),
            clock=clock,
            on_day_end=lambda day: backend.rebuild_batches(),
        )
    return compute_metrics(log, k=args.k)


def simulate_http(args, users, model) -> dict:
    import requests

    session = requests.Session()

    def day_end(day: int) -> None:
        if args.day_end_cmd:
            subprocess.run(args.day_end_cmd, shell=True, check=True)

    log = run_simulation(
        users,
        make_api=lambda u, calls: ApiClient(session, base_url=args.target, calls=calls, user_index=u.index),
        model=model,
        cfg=SimulationConfig(n_days=args.days, start=REFERENCE_START, seed=args.seed),
        on_day_end=day_end,
        wall_clock_features=True,
    )
    return compute_metrics(log, k=args.k)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", default="fake", help="'fake' or a base URL such as http://localhost:8000")
    p.add_argument("--policy", default="reactive", help="fake-app policy (ignored for http targets)")
    p.add_argument("--catalog", default="synthetic", choices=["synthetic", "team_archive"])
    p.add_argument("--team-archive-dir", type=Path, default=None)
    p.add_argument("--preset", default="default", choices=preset_names())
    p.add_argument("--calibrate", action="store_true",
                   help="re-fit the bias on this catalog (fake target) instead of the shipped preset bias")
    p.add_argument("--users", type=int, default=300)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--drift-day", type=int, default=3)
    p.add_argument("--drift-frac", type=float, default=0.10)
    p.add_argument("--late-join-frac", type=float, default=0.10)
    p.add_argument("--run-tag", default="sim")
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--day-end-cmd", default=None, help="shell command run after each virtual day (http target)")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)

    users = generate_population(PopulationConfig(
        n_users=args.users, seed=args.seed, n_days=args.days, late_join_frac=args.late_join_frac,
        drift_frac=args.drift_frac, drift_day=args.drift_day, run_tag=args.run_tag))
    cfg = preset(args.preset)
    report = {"args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}}

    if args.target == "fake":
        catalog = build_catalog(args.catalog, args.days, args.seed, args.team_archive_dir)
        if args.calibrate:
            cal = calibrate_bias(cfg, [u.profile for u in users], catalog, REFERENCE_START + timedelta(hours=12),
                                 CalibrationTarget(k=args.k))
            cfg = replace(cfg, bias=cal.bias)
            report["calibration"] = cal.to_dict()
        report["click_model"] = asdict(cfg)
        report["metrics"] = simulate_fake(args, users, ClickModel(cfg), catalog)
    else:
        report["click_model"] = asdict(cfg)
        report["metrics"] = simulate_http(args, users, ClickModel(cfg))

    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
