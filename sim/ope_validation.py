"""E9 [SIM]: do the off-policy estimators recover a target policy's click rate from policy A's logs?

Pre-registered in ADR 0025 ("사전 등록" 2 and its supplement A1). Nothing in this module may be
tuned on its own output: the worlds, the target policies, the truth, the estimators, the verdict
rule and the single retry are fixed there.

    python -m sim.ope_validation --runs-dir out/runs --out out/ope_validation.json --workers 4

Per seed: one week under logging policy A (active heuristic + MMR, 2 of 20 slots explored) gives
the logs; each target policy B (random, reactive, shadow_recency) is then run for real in the same
world with the same seed, which gives its measured click rate. The estimators of
evaluation/recsys/ope.py are applied to A's logs and compared with that measurement.
If the registered rule fails, policy A is run once more with 4 exploration slots (ε = 0.2), the same
rule is applied, and both results are reported.

Every number here is a property of the simulator. None of it says how good a recommender is.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from evaluation.recsys.ope import (
    SlotLog,
    fit_position_bias_eta,
    position_based_slate,
    position_ctr,
    power_law_theta,
    replay_exploration,
    replay_position_based,
    snips_slate,
)
from sim.serving_app import TARGET_POLICIES
from sim.serving_runs import (
    SLATE,
    RunData,
    bootstrap_ratio,
    experiment_meta,
    per_user_sums,
    policy_a,
    policy_b,
    policy_requests,
    run_all,
    run_summary,
)

LABEL = "[SIM] 시뮬레이터 안의 값. 추천 정확도나 서비스 클릭률이 아니다 (ADR 0019, ADR 0025 A1)"
REGISTERED = {"n_users": 300, "n_days": 7, "seeds": [0, 1, 2]}
PRIMARY = "replay_position_based"
FALLBACK = "position_based_slate"
ESTIMATORS = (PRIMARY, "replay_exploration", "snips_slate", FALLBACK)
REL_ERR_MAX = 0.15
ESS_RATIO_MIN = 0.05
COVERAGE_MIN = 0.9
N_BOOT = 1000
BOOT_SEED = 0
FIRST_EXPLORE_SLOTS = 2
RETRY_EXPLORE_SLOTS = 4


def _f(x) -> Optional[float]:
    """JSON has no NaN: undefined values are null."""
    x = float(x)
    return x if np.isfinite(x) else None


def _rel(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b)


# -------------------------------------------------------------------------------- truth


def measured_value(run_b: RunData) -> dict:
    """A target policy's click rate per slot, measured by running it (ADR 0025 A1.3 "실측")."""
    requests = policy_requests(run_b, "deterministic")
    slots = requests[run_b["slot_req"]]
    n_users = len(run_b["user_eta"])
    clicks = per_user_sums(run_b["slot_user"][slots], run_b["slot_click"][slots], n_users)
    shown = per_user_sums(run_b["slot_user"][slots], np.ones(int(slots.sum())), n_users)
    lo, hi = bootstrap_ratio(clicks, shown, N_BOOT, BOOT_SEED)
    expected = run_b["slot_expected"][slots]
    return {
        "ctr": _f(clicks.sum() / shown.sum()) if shown.sum() else None,
        "ci95": [_f(lo), _f(hi)],
        "clicks": int(clicks.sum()),
        "slots": int(shown.sum()),
        "requests": int(requests.sum()),
        "requests_excluded": int((~requests).sum()),
        # what the click model expected of the slates this policy showed (its own users, its own history)
        "click_model_expected_ctr": _f(np.nanmean(expected)) if len(expected) else None,
    }


# ---------------------------------------------------------------------------- estimates


def logged_slots(run_a: RunData):
    """Policy A's analysis set as a SlotLog, plus the user of each request (in np.unique order)."""
    requests = policy_requests(run_a, "eps-uniform-v1")
    slots = requests[run_a["slot_req"]]
    if not np.all(np.isfinite(run_a["slot_prop"][slots])):
        raise RuntimeError("a slot of the analysis set has no propensity")
    log = SlotLog.from_columns(
        request=run_a["slot_req"][slots],
        position=run_a["slot_pos"][slots],
        item=run_a["slot_item"][slots],
        explored=run_a["slot_explored"][slots],
        propensity=run_a["slot_prop"][slots],
        reward=run_a["slot_click"][slots],
    )
    return log, requests, run_a["req_user"][np.unique(log.request)]


def _estimate_dict(est, users, measured: Optional[float], same_context: Optional[float]) -> dict:
    lo, hi = est.bootstrap_ci(N_BOOT, BOOT_SEED, cluster_of_request=users)
    value = _f(est.value)
    return {
        "value": value,
        "ci95": [_f(lo), _f(hi)],
        "n_slots": est.n_slots,
        "n_matched": est.n_matched,
        "ess": _f(est.ess),
        "ess_ratio": _f(est.ess_ratio),
        "coverage": _f(est.coverage),
        "rel_err": _rel(value, measured),
        "rel_err_vs_same_context": _rel(value, same_context),
        "ci_covers_measured": (None if measured is None or lo != lo else bool(lo <= measured <= hi)),
    }


def policy_a_summary(run_a: RunData, log: SlotLog, requests: np.ndarray, eta_hat: float) -> dict:
    explored = np.asarray(log.explored, dtype=bool)
    counts = position_ctr(log, SLATE)
    users_of_requests = run_a["req_user"][requests]
    eta = run_a["user_eta"][users_of_requests]
    pool = run_a["req_pool"][requests]
    eligible = run_a["req_eligible"][requests]
    n_explore = run_a["req_n_explore"][requests]
    return {
        "requests": int(len(requests)),
        "requests_analyzed": int(requests.sum()),
        "requests_excluded": int((~requests).sum()),
        "slots": len(log),
        "explore_slots": int(explored.sum()),
        "requests_by_explore_slots": {str(m): int((n_explore == m).sum()) for m in np.unique(n_explore)},
        "ctr": _f(log.reward.mean()),
        "ctr_explore_slots": _f(log.reward[explored].mean()) if explored.any() else None,
        "ctr_deterministic_slots": _f(log.reward[~explored].mean()) if (~explored).any() else None,
        "clicks": int(log.reward.sum()),
        "clicks_on_explore_slots": int(log.reward[explored].sum()),
        "click_model_expected_ctr": _f(np.nanmean(run_a["slot_expected"][requests[run_a["slot_req"]]])),
        "eta_hat": _f(eta_hat),
        "simulator_eta": {"request_weighted_mean": _f(eta.mean()), "min": _f(eta.min()), "max": _f(eta.max())},
        "explore_position_clicks": [int(c) for c in counts.clicks],
        "explore_position_slots": [int(c) for c in counts.slots],
        "explore_pool_size": {"mean": _f(pool.mean()), "min": _f(pool.min()), "max": _f(pool.max())},
        "eligible_count": {"mean": _f(eligible.mean()), "min": _f(eligible.min()), "max": _f(eligible.max())},
        "cache_hits": int(run_a["req_cache_hit"][requests].sum()),
        "shadow_scored_slot_share": _f(run_a["slot_shadow_scored"][requests[run_a["slot_req"]]].mean()),
        "driver": run_a.meta["driver"],
        "counters": run_a.meta["counters"],
    }


def estimate_target(run_a: RunData, log: SlotLog, users: np.ndarray, theta: np.ndarray, name: str,
                    measured: dict) -> dict:
    slates = run_a[f"tgt_{name}"]
    unique_requests = np.unique(log.request)
    target = {int(r): [int(i) for i in slates[r] if i >= 0] for r in unique_requests}
    if any(len(s) != SLATE for s in target.values()):
        raise RuntimeError(f"target {name}: a slate is not {SLATE} long")
    expected = run_a[f"tgt_expected_{name}"][unique_requests]
    same_context = _f(np.nansum(expected) / (SLATE * np.isfinite(expected).sum()))
    estimates = {
        PRIMARY: replay_position_based(log, target, theta),
        "replay_exploration": replay_exploration(log, target),
        "snips_slate": snips_slate(log, target),
        FALLBACK: position_based_slate(log, target, theta),
    }
    out = {e: _estimate_dict(est, users, measured["ctr"], same_context) for e, est in estimates.items()}
    explore_slots = int(np.asarray(log.explored, dtype=bool).sum())
    out[FALLBACK]["ess_over_explore_slots"] = _f(estimates[FALLBACK].ess / explore_slots) if explore_slots else None

    # how much of the target slate policy A's deterministic slots already show (the rest is in the explore pool)
    det_items: Dict[int, set] = {}
    for req, item, was_explored in zip(log.request, log.item, log.explored):
        if not was_explored:
            det_items.setdefault(int(req), set()).add(int(item))
    in_det = np.asarray([len(det_items.get(r, set()) & set(target[r])) for r in target], dtype=np.float64)
    return {
        "measured": measured,
        "same_context_expected_ctr": same_context,
        "state_shift_rel": _rel(same_context, measured["ctr"]),
        "target_items_in_deterministic_slots_mean": _f(in_det.mean()),
        "target_items_in_explore_pool_mean": _f(SLATE - in_det.mean()),
        "estimators": out,
    }


# ------------------------------------------------------------------------------ verdict


def judge_target(per_seed: Sequence[dict]) -> dict:
    """ADR 0025 A1.3: one verdict estimator per target (the primary one unless its 3-seed mean coverage
    is below 0.9), mean relative error over the seeds <= 0.15, ESS ratio >= 0.05 on every seed."""

    def column(estimator: str, key: str) -> List[Optional[float]]:
        return [s["estimators"][estimator][key] for s in per_seed]

    def mean(values: List[Optional[float]]) -> Optional[float]:
        return None if any(v is None for v in values) or not values else float(np.mean(values))

    primary_coverage = mean(column(PRIMARY, "coverage"))
    estimator = PRIMARY if primary_coverage is not None and primary_coverage >= COVERAGE_MIN else FALLBACK
    rel_errs, ess_ratios = column(estimator, "rel_err"), column(estimator, "ess_ratio")
    mean_rel_err = mean(rel_errs)
    min_ess_ratio = None if any(v is None for v in ess_ratios) else float(min(ess_ratios))
    rel_ok = mean_rel_err is not None and mean_rel_err <= REL_ERR_MAX
    ess_ok = min_ess_ratio is not None and min_ess_ratio >= ESS_RATIO_MIN
    return {
        "primary_coverage_mean": primary_coverage,
        "explore_support_short": estimator == FALLBACK,
        "verdict_estimator": estimator,
        "rel_err_by_seed": rel_errs,
        "rel_err_mean": mean_rel_err,
        "rel_err_ok": bool(rel_ok),
        "ess_ratio_by_seed": ess_ratios,
        "ess_ratio_min": min_ess_ratio,
        "ess_ok": bool(ess_ok),
        "passed": bool(rel_ok and ess_ok),
        "summary_by_estimator": {
            e: {
                "rel_err_mean": mean(column(e, "rel_err")),
                "rel_err_vs_same_context_mean": mean(column(e, "rel_err_vs_same_context")),
                "coverage_mean": mean(column(e, "coverage")),
                "ess_ratio_min": (None if any(v is None for v in column(e, "ess_ratio"))
                                  else float(min(column(e, "ess_ratio")))),
            }
            for e in ESTIMATORS
        },
        "state_shift_rel_mean": mean([s["state_shift_rel"] for s in per_seed]),
    }


def analyze_stage(a_runs: Dict[int, RunData], measured: Dict[str, Dict[int, dict]], explore_slots: int) -> dict:
    seeds = sorted(a_runs)
    policy_a_by_seed, per_target = {}, {name: [] for name in TARGET_POLICIES}
    for seed in seeds:
        run_a = a_runs[seed]
        log, requests, users = logged_slots(run_a)
        eta_hat = fit_position_bias_eta(position_ctr(log, SLATE))
        # Without a click on an exploration slot there is nothing to fit (only in tiny smoke runs).
        theta = power_law_theta(SLATE, eta_hat if np.isfinite(eta_hat) else 0.0)
        policy_a_by_seed[str(seed)] = policy_a_summary(run_a, log, requests, eta_hat)
        for name in TARGET_POLICIES:
            row = estimate_target(run_a, log, users, theta, name, measured[name][seed])
            per_target[name].append({"seed": seed, **row})
    targets = {name: {"per_seed": rows, "verdict": judge_target(rows)} for name, rows in per_target.items()}
    return {
        "explore_slots": explore_slots,
        "policy_a": policy_a_by_seed,
        "targets": targets,
        "passed": bool(all(t["verdict"]["passed"] for t in targets.values())),
    }


def final_verdict(first: dict, retry: Optional[dict]) -> dict:
    if first["passed"]:
        return {"e9": "pass", "passed_at_explore_slots": first["explore_slots"], "retry_run": False,
                "adr_status": "채택됨",
                "text": "탐색 2칸(운영 기본값)에서 세 타깃 모두 사전 등록 기준을 통과했다."}
    if retry is None:
        raise RuntimeError("the first stage failed and the registered retry was not run")
    if retry["passed"]:
        return {"e9": "pass_on_retry", "passed_at_explore_slots": retry["explore_slots"], "retry_run": True,
                "adr_status": "채택됨",
                "text": "탐색 4칸에서 통과, 운영 기본값 2칸에서는 실패."}
    return {"e9": "fail", "passed_at_explore_slots": None, "retry_run": True, "adr_status": "제안됨",
            "text": "탐색 2칸과 재실험(탐색 4칸) 모두 사전 등록 기준을 통과하지 못했다."}


# ---------------------------------------------------------------------------------- run


def run_meta(args, registered: bool) -> dict:
    meta = experiment_meta(args, "E9 OPE validation", "sim.ope_validation",
                           "docs/adr/0025-logging-v2-exploration-and-ope.md 사전 등록 2, 보완 A1", registered)
    meta["label"] = LABEL
    meta["rule"] = {"rel_err_max": REL_ERR_MAX, "ess_ratio_min": ESS_RATIO_MIN, "coverage_min": COVERAGE_MIN,
                    "primary": PRIMARY, "fallback": FALLBACK,
                    "bootstrap": {"n": N_BOOT, "seed": BOOT_SEED, "cluster": "user"}}
    return meta


def run_experiment(args) -> dict:
    seeds = list(args.seeds)
    registered = {"n_users": args.users, "n_days": args.days, "seeds": seeds} == REGISTERED
    meta = run_meta(args, registered)

    a_specs = {s: policy_a(s, args.users, args.days, FIRST_EXPLORE_SLOTS) for s in seeds}
    b_specs = {name: {s: policy_b(name, s, args.users, args.days) for s in seeds} for name in TARGET_POLICIES}
    specs = [*a_specs.values(), *(spec for by_seed in b_specs.values() for spec in by_seed.values())]
    runs = run_all(specs, args.runs_dir, args.workers)

    measured = {name: {s: measured_value(runs[spec.name]) for s, spec in by_seed.items()}
                for name, by_seed in b_specs.items()}
    first = analyze_stage({s: runs[spec.name] for s, spec in a_specs.items()}, measured, FIRST_EXPLORE_SLOTS)

    retry = None
    if not first["passed"]:
        retry_specs = {s: policy_a(s, args.users, args.days, RETRY_EXPLORE_SLOTS) for s in seeds}
        retry_runs = run_all(list(retry_specs.values()), args.runs_dir, args.workers)
        runs.update(retry_runs)
        retry = analyze_stage({s: retry_runs[spec.name] for s, spec in retry_specs.items()}, measured,
                              RETRY_EXPLORE_SLOTS)

    meta["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["runs"] = {name: run_summary(run) for name, run in sorted(runs.items())}
    return {
        "meta": meta,
        "explore_slots_2": first,
        "explore_slots_4_retry": retry,
        "verdict": final_verdict(first, retry),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs-dir", type=Path, required=True, help="per-run tables (cached by run name)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--users", type=int, default=REGISTERED["n_users"])
    p.add_argument("--days", type=int, default=REGISTERED["n_days"])
    p.add_argument("--seeds", type=int, nargs="+", default=REGISTERED["seeds"])
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--prereg-commit", default=None, help="commit of ADR 0025 A1 (recorded in the report)")
    p.add_argument("--git-sha", default=None, help="code commit when not running in GitHub Actions")
    args = p.parse_args(argv)
    result = run_experiment(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    v = result["verdict"]
    print(f"E9 [SIM] verdict: {v['e9']} - {v['text']} (registered config: {result['meta']['registered_config']})")
    return 0  # the verdict is a result, not an error


if __name__ == "__main__":
    raise SystemExit(main())
