"""Staged Locust runs (default 5 / 20 / 50 RPS) and a latency table per stage.

    python -m sim.loadtest --host http://localhost:8000 --rps 5 20 50 --duration 60s --out-dir out/load

Each stage is a separate headless Locust run with N = rps ActiveReaders (1 task/s
each) plus one Newcomer, `--reset-stats` so the account setup of readers during
ramp-up is excluded. Reported per endpoint: requests, failures, error rate,
achieved RPS, p50/p95/p99 (ms), from Locust's `<prefix>_stats.csv`; and per
/today endpoint the X-Rec-Source distribution with empty and fallback rates,
from the locustfile's `<prefix>_sources.json` (fallback rate is "-" when the API
sends no X-Rec-Source header, as on current main).
"""

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOCUSTFILE = Path(__file__).resolve().parent / "locustfile.py"


def _num(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):  # Locust writes "N/A" for endpoints without samples
        return None


def summarize_locust_csv(stats_csv: Path) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    with open(stats_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n = int(row["Request Count"])
            fails = int(row["Failure Count"])
            out[row["Name"]] = {
                "requests": n,
                "failures": fails,
                "error_rate": (fails / n) if n else None,
                "rps": _num(row["Requests/s"]),
                "p50_ms": _num(row["50%"]),
                "p95_ms": _num(row["95%"]),
                "p99_ms": _num(row["99%"]),
            }
    return out


def markdown_table(stages: Dict[str, Dict[str, dict]], endpoints: List[str]) -> str:
    lines = ["| 목표 RPS | 엔드포인트 | 요청 수 | 달성 RPS | p50 ms | p95 ms | p99 ms | 오류율 |",
             "|---|---|---|---|---|---|---|---|"]
    for stage, rows in stages.items():
        for ep in endpoints:
            r = rows.get(ep)
            if r is None:
                continue
            err = "-" if r["error_rate"] is None else f"{100 * r['error_rate']:.2f}%"
            fmt = lambda v, d=0: "-" if v is None else f"{v:.{d}f}"  # noqa: E731
            lines.append(f"| {stage} | {ep} | {r['requests']} | {fmt(r['rps'], 1)} | {fmt(r['p50_ms'])} | "
                         f"{fmt(r['p95_ms'])} | {fmt(r['p99_ms'])} | {err} |")
    return "\n".join(lines)


def source_table(stages: Dict[str, Dict[str, dict]]) -> str:
    lines = ["| 목표 RPS | 엔드포인트 | 응답 수 | 빈 응답률 | 폴백률 | X-Rec-Source 분포 |", "|---|---|---|---|---|---|"]
    pct = lambda v: "-" if v is None else f"{100 * v:.2f}%"  # noqa: E731
    for stage, rows in stages.items():
        for ep, r in rows.items():
            dist = ", ".join(f"{k}: {n}" for k, n in sorted(r["source_counts"].items(), key=lambda kv: -kv[1]))
            lines.append(f"| {stage} | {ep} | {r['responses']} | {pct(r['empty_rate'])} | "
                         f"{pct(r['fallback_rate'])} | {dist} |")
    return "\n".join(lines)


def run_stage(host: str, rps: int, duration: str, prefix: Path, env: dict) -> Tuple[Path, Path]:
    users = rps + 1  # rps readers + the fixed_count=1 newcomer
    cmd = [sys.executable, "-m", "locust", "-f", str(LOCUSTFILE), "--headless", "--host", host,
           "-u", str(users), "-r", str(users), "-t", duration, "--reset-stats", "--only-summary",
           "--csv", str(prefix), "--exit-code-on-error", "0"]
    sources = Path(f"{prefix}_sources.json")
    subprocess.run(cmd, check=True, env={**env, "SIM_SOURCES_OUT": str(sources)})
    return Path(f"{prefix}_stats.csv"), sources


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True)
    p.add_argument("--rps", nargs="+", type=int, default=[5, 20, 50])
    p.add_argument("--duration", default="60s")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--run-tag", default="load")
    args = p.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SIM_RUN_TAG": args.run_tag}
    stages, sources = {}, {}
    for rps in args.rps:
        stats, src = run_stage(args.host, rps, args.duration, args.out_dir / f"rps{rps}", env)
        stages[str(rps)] = summarize_locust_csv(stats)
        sources[str(rps)] = json.loads(src.read_text(encoding="utf-8")) if src.exists() else {}
    report = {"host": args.host, "duration": args.duration, "stages": stages, "rec_sources": sources}
    (args.out_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    table = markdown_table(stages, ["today", "click", "today_first_view", "signup", "onboarding_news", "Aggregated"])
    text = table + "\n\n" + source_table(sources) + "\n"
    (args.out_dir / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
