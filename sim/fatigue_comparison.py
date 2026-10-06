"""E10 [SIM]: the impression-fatigue rule, counting only (`log`) against enforced (`enforce`).

Pre-registered in ADR 0025 ("사전 등록" 4 and its supplement A1.4). The rule drops from the candidates
what the user was shown 3 times or more in the last 48 hours without clicking.

    python -m sim.fatigue_comparison --runs-dir out/runs --out out/fatigue_v1.json --workers 4

Two arms per seed on the real serving path, identical except RECSYS_FATIGUE_MODE. The `log` arm is
the policy-A run of E9 (sim.ope_validation), so with a shared --runs-dir it is not repeated.

Verdict rule (fixed before any run):
  (1) repeat-impression rate: the 95% CI of (log - enforce) lies above 0, and
  (2) the reactivity metrics are not worse: the CI of (enforce - log) is not entirely on the bad
      side - above 0 for after_click_jaccard (the list changes less after a click), below 0 for
      similar_share_lift.
The CIs come from a paired bootstrap over (seed, user) clusters.

Every number here is a property of the simulator. The click model has a hand-written repetition
penalty, so a click-rate difference between the arms is true by construction and is not a claim.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from sim.serving_runs import (
    FATIGUE_HOURS,
    FATIGUE_MIN_IMPRESSIONS,
    REACTIVITY_K,
    SLATE,
    RunData,
    add_run_arguments,
    experiment_meta,
    per_user_sums,
    policy_a,
    policy_requests,
    repeat_flags,
    run_all,
    run_summary,
)

LABEL = "[SIM] 시뮬레이터 안의 값. 추천 정확도나 서비스 클릭률이 아니다 (ADR 0019, ADR 0025 A1)"
REGISTERED = {"n_users": 300, "n_days": 7, "seeds": [0, 1, 2]}
ARMS = ("log", "enforce")
N_BOOT = 1000
BOOT_SEED = 0
EXPLORE_SLOTS = 2
MET_TEXT = ("enforce 조건 충족: 반복 노출 비율이 줄었고(구간 하한 > 0) "
            "반응성 지표가 나빠진 것은 검출되지 않았다.")

# per-(seed, user) sums; every reported metric is a ratio (or a difference of ratios) of these
COMPONENTS = ("slots", "repeats", "rule_repeats", "clicks", "jaccard_sum", "pairs", "before_sum", "before_n",
              "after_sum", "after_n", "cold_requests", "cold_ild_sum", "cold_entropy_sum",
              "cold_path_requests", "cold_path_ild_sum", "cold_path_entropy_sum",
              "answers", "answer_slots", "answer_repeats", "fallback_answers", "short_slates", "policy_answers")
C = {name: i for i, name in enumerate(COMPONENTS)}


def _f(x) -> Optional[float]:
    x = float(x)
    return x if np.isfinite(x) else None


def _ratio(num, den):
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)


METRICS = {
    # name: function of the summed components (works on one row of sums or on a matrix of resamples)
    "repeat_impression_rate": lambda s: _ratio(s[..., C["repeats"]], s[..., C["slots"]]),
    "rule_blocked_impression_rate": lambda s: _ratio(s[..., C["rule_repeats"]], s[..., C["slots"]]),
    "after_click_jaccard_mean": lambda s: _ratio(s[..., C["jaccard_sum"]], s[..., C["pairs"]]),
    "similar_share_lift": lambda s: (_ratio(s[..., C["after_sum"]], s[..., C["after_n"]])
                                     - _ratio(s[..., C["before_sum"]], s[..., C["before_n"]])),
    "cold_user_ild": lambda s: _ratio(s[..., C["cold_ild_sum"]], s[..., C["cold_requests"]]),
    "cold_user_category_entropy": lambda s: _ratio(s[..., C["cold_entropy_sum"]], s[..., C["cold_requests"]]),
    "cold_path_ild": lambda s: _ratio(s[..., C["cold_path_ild_sum"]], s[..., C["cold_path_requests"]]),
    "cold_path_category_entropy": lambda s: _ratio(s[..., C["cold_path_entropy_sum"]],
                                                   s[..., C["cold_path_requests"]]),
    "ctr": lambda s: _ratio(s[..., C["clicks"]], s[..., C["slots"]]),
    # what the fatigue rule does to the answers themselves (ADR 0025 A1.6)
    "repeat_impression_rate_all_answers": lambda s: _ratio(s[..., C["answer_repeats"]], s[..., C["answer_slots"]]),
    "fallback_answer_share": lambda s: _ratio(s[..., C["fallback_answers"]], s[..., C["answers"]]),
    "short_slate_share": lambda s: _ratio(s[..., C["short_slates"]], s[..., C["policy_answers"]]),
    "slots_per_policy_answer": lambda s: _ratio(s[..., C["slots"]], s[..., C["policy_answers"]]),
}
JUDGED = ("repeat_impression_rate", "after_click_jaccard_mean", "similar_share_lift")


def user_components(run: RunData) -> np.ndarray:
    """(n_users, len(COMPONENTS)) sums for one run."""
    n_users = len(run["user_eta"])
    # Every slate the policy made, whatever its size: the rule shortens slates, and reading the 20-slot
    # ones only would drop the requests where it bit hardest (ADR 0025 A1.6).
    requests = policy_requests(run, "eps-uniform-v1", full_slate_only=False)
    slots = requests[run["slot_req"]]
    repeat_all, rule_all = repeat_flags(run)
    repeat, rule = repeat_all & slots, rule_all & slots
    out = np.zeros((n_users, len(COMPONENTS)))

    def put(name, users, values):
        out[:, C[name]] = per_user_sums(users, np.asarray(values, dtype=np.float64), n_users)

    su = run["slot_user"]
    put("slots", su[slots], np.ones(int(slots.sum())))
    put("repeats", su, repeat)
    put("rule_repeats", su, rule)
    put("clicks", su[slots], run["slot_click"][slots])

    pu = run["pair_user"]
    put("jaccard_sum", pu, run["pair_jaccard"])
    put("pairs", pu, np.ones(len(pu)))
    for side in ("before", "after"):
        values = run[f"pair_{side}"]
        ok = np.isfinite(values)
        put(f"{side}_sum", pu[ok], values[ok])
        put(f"{side}_n", pu[ok], np.ones(int(ok.sum())))

    # Cold users = users who joined during the simulation (ADR 0019); cold path = answers the service
    # itself labelled cold_start_* (no long-term and no short-term vector yet).
    ru = run["req_user"]
    late = requests & run["user_late"][ru]
    cold_sources = [i for i, name in enumerate(run.meta["sources"]) if name.startswith("cold_start")]
    cold_path = requests & np.isin(run["req_source"], cold_sources)
    for prefix, mask in (("cold", late), ("cold_path", cold_path)):
        mask = mask & np.isfinite(run["req_ild"])  # a slate of one newsletter has no pairwise distance
        put(f"{prefix}_requests", ru[mask], np.ones(int(mask.sum())))
        put(f"{prefix}_ild_sum", ru[mask], run["req_ild"][mask])
        put(f"{prefix}_entropy_sum", ru[mask], run["req_cat_entropy"][mask])

    # All answers, fallbacks included: what the user actually saw.
    put("answers", ru, np.ones(len(ru)))
    put("answer_slots", su, np.ones(len(su)))
    put("answer_repeats", su, repeat_all)
    put("fallback_answers", ru, ~requests)
    put("policy_answers", ru, requests)
    put("short_slates", ru, requests & (run["req_slate_size"] < SLATE))
    return out


def arm_description(run: RunData) -> dict:
    requests = policy_requests(run, "eps-uniform-v1", full_slate_only=False)
    fatigued = run["req_fatigued"][requests]
    eligible = run["req_eligible"][requests]
    return {
        "requests": int(len(requests)),
        "requests_analyzed": int(requests.sum()),
        "requests_excluded": int((~requests).sum()),
        "short_slates": int((requests & (run["req_slate_size"] < SLATE)).sum()),
        "slate_size_min": int(run["req_slate_size"][requests].min()) if requests.any() else None,
        "fatigued_count": {"mean": _f(np.nanmean(fatigued)), "median": _f(np.nanmedian(fatigued)),
                           "max": _f(np.nanmax(fatigued)),
                           "share_of_requests_with_any": _f(np.mean(fatigued > 0))},
        "eligible_count": {"mean": _f(eligible.mean()), "min": _f(eligible.min())},
        "explore_pool_size_mean": _f(np.nanmean(run["req_pool"][requests])),
        "driver_reactivity": run.meta["driver"]["reactivity"],
        "run": run_summary(run),
    }


def _ci(values: np.ndarray) -> List[Optional[float]]:
    if not np.isfinite(values).any():
        return [None, None]
    lo, hi = np.nanquantile(values, [0.025, 0.975])
    return [_f(lo), _f(hi)]


def compare(components: Dict[str, np.ndarray]) -> dict:
    """components[arm] is (n_clusters, len(COMPONENTS)); row k is the same (seed, user) in both arms."""
    n = len(components["log"])
    rng = np.random.default_rng(BOOT_SEED)
    idx = rng.integers(0, n, size=(N_BOOT, n))
    totals = {arm: components[arm].sum(axis=0) for arm in ARMS}
    resampled = {arm: components[arm][idx].sum(axis=1) for arm in ARMS}  # the same clusters in both arms
    out = {}
    for name, fn in METRICS.items():
        point = {arm: float(fn(totals[arm])) for arm in ARMS}
        boot = {arm: fn(resampled[arm]) for arm in ARMS}
        out[name] = {
            "log": _f(point["log"]),
            "enforce": _f(point["enforce"]),
            "log_ci95": _ci(boot["log"]),
            "enforce_ci95": _ci(boot["enforce"]),
            "enforce_minus_log": _f(point["enforce"] - point["log"]),
            "enforce_minus_log_ci95": _ci(boot["enforce"] - boot["log"]),
        }
    return out


def judge(metrics: dict) -> dict:
    """ADR 0025 A1.4."""
    repeat_lo, repeat_hi = metrics["repeat_impression_rate"]["enforce_minus_log_ci95"]
    jac_lo, _ = metrics["after_click_jaccard_mean"]["enforce_minus_log_ci95"]
    _, lift_hi = metrics["similar_share_lift"]["enforce_minus_log_ci95"]
    undetermined = any(v is None for v in (repeat_lo, repeat_hi, jac_lo, lift_hi))
    # (log - enforce) has its lower bound above 0  <=>  (enforce - log) has its upper bound below 0
    reduced = (not undetermined) and repeat_hi < 0
    jaccard_worse = (not undetermined) and jac_lo > 0
    lift_worse = (not undetermined) and lift_hi < 0
    ok = reduced and not jaccard_worse and not lift_worse
    return {
        "repeat_rate_reduced": bool(reduced),
        "log_minus_enforce_repeat_rate_ci95": [None if repeat_hi is None else -repeat_hi,
                                               None if repeat_lo is None else -repeat_lo],
        "after_click_jaccard_worse": bool(jaccard_worse),
        "similar_share_lift_worse": bool(lift_worse),
        "undetermined": bool(undetermined),
        "enforce_condition_met": bool(ok),
        "text": (MET_TEXT if ok else "enforce 조건 미충족."),
    }


def run_experiment(args) -> dict:
    seeds = list(args.seeds)
    registered = {"n_users": args.users, "n_days": args.days, "seeds": seeds} == REGISTERED
    meta = experiment_meta(args, "E10 fatigue rule: log vs enforce", "sim.fatigue_comparison",
                           "docs/adr/0025-logging-v2-exploration-and-ope.md 사전 등록 4, 보완 A1.4", registered)
    meta["label"] = LABEL
    meta["rule"] = {"fatigue_hours": FATIGUE_HOURS, "fatigue_min_impressions": FATIGUE_MIN_IMPRESSIONS,
                    "reactivity_k": REACTIVITY_K, "explore_slots": EXPLORE_SLOTS,
                    "bootstrap": {"n": N_BOOT, "seed": BOOT_SEED, "cluster": "(seed, user), paired"}}

    specs = {
        "log": {s: policy_a(s, args.users, args.days, EXPLORE_SLOTS, "log", with_targets=True) for s in seeds},
        "enforce": {s: policy_a(s, args.users, args.days, EXPLORE_SLOTS, "enforce", with_targets=False)
                    for s in seeds},
    }
    runs = run_all([spec for by_seed in specs.values() for spec in by_seed.values()], args.runs_dir, args.workers)

    per_seed_components = {arm: {s: user_components(runs[spec.name]) for s, spec in by_seed.items()}
                           for arm, by_seed in specs.items()}
    pooled = {arm: np.concatenate([per_seed_components[arm][s] for s in seeds]) for arm in ARMS}
    metrics = compare(pooled)
    by_seed = {
        str(s): {name: {arm: _f(fn(per_seed_components[arm][s].sum(axis=0))) for arm in ARMS}
                 for name, fn in METRICS.items()}
        for s in seeds
    }
    counts = {arm: {name: _f(pooled[arm][:, C[name]].sum()) for name in COMPONENTS} for arm in ARMS}
    meta["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return {
        "meta": meta,
        "clusters": int(len(pooled["log"])),
        "metrics": metrics,
        "metrics_by_seed": by_seed,
        "counts": counts,
        "arms": {arm: {str(s): arm_description(runs[spec.name]) for s, spec in by_seed.items()}
                 for arm, by_seed in specs.items()},
        "judged_metrics": list(JUDGED),
        "verdict": judge(metrics),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs-dir", type=Path, required=True, help="per-run tables (cached by run name)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--users", type=int, default=REGISTERED["n_users"])
    p.add_argument("--days", type=int, default=REGISTERED["n_days"])
    p.add_argument("--seeds", type=int, nargs="+", default=REGISTERED["seeds"])
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--prereg-commit", default=None, help="commit of ADR 0025 A1 (recorded in the report)")
    p.add_argument("--git-sha", default=None, help="code commit when not running in GitHub Actions")
    add_run_arguments(p)
    args = p.parse_args(argv)
    result = run_experiment(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"E10 [SIM] verdict: {result['verdict']['text']} "
          f"(registered config: {result['meta']['registered_config']})")
    return 0  # the verdict is a result, not an error


if __name__ == "__main__":
    raise SystemExit(main())
