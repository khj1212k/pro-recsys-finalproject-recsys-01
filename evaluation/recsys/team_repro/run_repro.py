"""팀 베이스라인 재현 실험의 최상위 오케스트레이터.

버전(team-final/fix-snapshot/current)마다 pipeline.py를 별도 서브프로세스로 실행한다
(세 버전이 `src.data.data_loader` 등 동일한 모듈 경로를 쓰므로, 한 프로세스에서 여러
버전을 import하면 sys.modules 충돌이 생긴다 - pipeline.py 모듈 docstring 참고).
베이스라인은 recommend_engine을 전혀 쓰지 않으므로 이 프로세스 안에서 직접 계산한다.

결과: reports/recsys/team_repro_v1.json (전체 원자료) + reports/recsys/team_repro_v1.md
(한국어 리포트)를 만든다.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

TEAM_REPRO_DIR = Path(__file__).resolve().parent
WORKTREE_ROOT = TEAM_REPRO_DIR.parents[2]  # .../evaluation/recsys/team_repro -> worktree root
VENV_PYTHON = WORKTREE_ROOT / ".venv" / "bin" / "python"
RESULTS_DIR = Path("/tmp/team_repro_runs")
REPORT_DIR = WORKTREE_ROOT / "reports" / "recsys"

ENGINE_ROOTS = {
    "current": str(WORKTREE_ROOT / "ai_workspace" / "recommend_engine"),
    "team-final": "/private/tmp/repro-team/ai_workspace/recommend_engine",
    "fix-snapshot": "/private/tmp/repro-fix/ai_workspace/recommend_engine",
}

sys.path.insert(0, str(TEAM_REPRO_DIR))
import file_loader as FL  # noqa: E402
import baselines as BL  # noqa: E402
import metrics as M  # noqa: E402
import categories as CAT  # noqa: E402

# Evaluator는 src.core.evaluator에 numpy 외 의존성이 없다(팀 코드 수정 없이 그대로 재사용,
# torch 스텁도 필요 없다 - src.core.__init__이 utils를 안 끌어온다). 베이스라인 평가 전용으로
# "current" 엔진 루트에서 한 번만 import한다(run_repro.py 프로세스는 다른 버전의 src.*를
# import하지 않으므로 pipeline.py처럼 서브프로세스로 격리할 필요가 없다).
sys.path.insert(0, ENGINE_ROOTS["current"])
from src.core.evaluator import Evaluator  # noqa: E402


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def git_sha(worktree: str, ref: str) -> str:
    out = subprocess.run(
        ["git", "-C", worktree, "rev-parse", ref], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def run_pipeline(
    version: str,
    protocol: str,
    label_mode: str,
    leakage_mode: str,
    candidate_pool: str,
    negative_source: str,
    objective_override: str,
    seed: int,
    tag: str,
) -> dict:
    out_path = RESULTS_DIR / f"{tag}.json"
    if out_path.exists():
        return json.loads(out_path.read_text(encoding="utf-8"))

    cmd = [
        str(VENV_PYTHON),
        str(TEAM_REPRO_DIR / "pipeline.py"),
        "--engine-root", ENGINE_ROOTS[version],
        "--version", version,
        "--protocol", protocol,
        "--label-mode", label_mode,
        "--leakage-mode", leakage_mode,
        "--candidate-pool", candidate_pool,
        "--negative-source", negative_source,
        "--objective-override", objective_override,
        "--seed", str(seed),
        "--out", str(out_path),
    ]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(WORKTREE_ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[FAIL] {tag} ({time.time()-t0:.1f}s)", file=sys.stderr)
        print(proc.stdout[-3000:], file=sys.stderr)
        print(proc.stderr[-3000:], file=sys.stderr)
        raise RuntimeError(f"pipeline.py 실패: {tag}")
    print(f"[OK] {tag} ({time.time()-t0:.1f}s)")
    return json.loads(out_path.read_text(encoding="utf-8"))


def run_many_seeds(seeds: List[int], **kwargs) -> List[dict]:
    results = []
    for seed in seeds:
        tag = "__".join(
            [
                kwargs["version"],
                kwargs["protocol"],
                kwargs["label_mode"],
                kwargs["leakage_mode"],
                kwargs["candidate_pool"],
                kwargs["negative_source"],
                kwargs["objective_override"],
                f"seed{seed}",
            ]
        )
        results.append(run_pipeline(seed=seed, tag=tag, **kwargs))
    return results


def summarize_seeds(results: List[dict]) -> dict:
    import numpy as np

    if not results:
        return {}
    keys = results[0]["aggregate_metrics"].keys()
    out = {}
    for k in keys:
        vals = [r["aggregate_metrics"][k] for r in results if k in r["aggregate_metrics"]]
        out[k] = {"mean": float(np.mean(vals)), "std": float(np.std(vals)), "values": vals}
    aucs = [r["auc"] for r in results if r["auc"] is not None]
    if aucs:
        out["auc"] = {"mean": float(np.mean(aucs)), "std": float(np.std(aucs)), "values": aucs}
    out["elapsed_sec_total"] = sum(r["elapsed_sec"] for r in results)
    out["n_seeds"] = len(results)
    return out


def pooled_per_user(results: List[dict]) -> Dict[int, Dict[str, float]]:
    """여러 시드의 결과를 유저별로 평균낸 per-user metric (부트스트랩 재표본 단위)."""
    import numpy as np

    per_user_all: Dict[int, Dict[str, List[float]]] = {}
    for r in results:
        for uid_str, m in r["per_user_metrics"].items():
            uid = int(uid_str)
            bucket = per_user_all.setdefault(uid, {})
            for k, v in m.items():
                bucket.setdefault(k, []).append(v)
    return {uid: {k: float(np.mean(v)) for k, v in bucket.items()} for uid, bucket in per_user_all.items()}


def compute_baselines_for_protocol(bundle, protocol: str, seed: int = 42) -> dict:
    pinned_now = bundle.dataset_end_time.to_pydatetime()
    category_map = FL.category_ids_by_newsletter(bundle)
    candidate_ids = bundle.newsletters["news_letter_id"].tolist()
    user_ids = bundle.users["user_id"].tolist()

    logs = bundle.ctr_logs[bundle.ctr_logs["is_clicked"] == 1][["user_id", "news_letter_id", "timestamp"]]

    if protocol == "generator_split":
        train_logs = bundle.generator_train_keys
        train_logs = train_logs[train_logs["is_clicked"] == 1][["user_id", "news_letter_id", "timestamp"]]
        gt_logs = bundle.generator_valid_keys
        gt_logs = gt_logs[gt_logs["is_clicked"] == 1]
        ground_truth = gt_logs.groupby("user_id")["news_letter_id"].apply(set).to_dict()
    else:
        # team_split: pipeline.py가 만든 것과 동일한 valid 기간(마지막 20%)을 근사하기 위해
        # 클릭 로그를 시간순 정렬 후 80% 지점 이후를 valid로 쓴다 (베이스라인은 학습이
        # 없으므로 'train'은 그 이전 구간의 클릭수 집계에만 쓰인다).
        sorted_logs = logs.sort_values("timestamp")
        split_idx = int(len(sorted_logs) * 0.8)
        train_logs = sorted_logs.iloc[:split_idx]
        valid_logs = sorted_logs.iloc[split_idx:]
        ground_truth = valid_logs.groupby("user_id")["news_letter_id"].apply(set).to_dict()

    evaluator = Evaluator(k_values=[5, 10, 20])
    all_recs = BL.compute_all_baselines(
        bundle, category_map, candidate_ids, user_ids, train_logs, pinned_now, top_k=20, seed=seed
    )
    out = {}
    for name, recs in all_recs.items():
        per_user = M.per_user_metrics(evaluator, recs, ground_truth, category_map)
        out[name] = {"aggregate": M.aggregate(per_user), "per_user": per_user}
    return out


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    bundle = FL.load_archive_bundle()

    print("=" * 70)
    print("1) 헤드라인 재현: 3개 코드 버전 x team_split 프로토콜 x 5 시드")
    print("=" * 70)
    import os
    if os.environ.get("TEAM_REPRO_QUICK"):
        seeds5 = [42]
        seeds3 = [42]
    else:
        seeds5 = [42, 43, 44, 45, 46]
        seeds3 = [42, 43, 44]

    headline = {}
    for version, n_seeds in [("current", seeds5), ("team-final", seeds5), ("fix-snapshot", seeds3)]:
        results = run_many_seeds(
            seeds=n_seeds,
            version=version,
            protocol="team_split",
            label_mode="clicks_only",
            leakage_mode="fixed",
            candidate_pool="full_195",
            negative_source="random",
            objective_override="none",
        )
        headline[version] = {"runs": results, "summary": summarize_seeds(results)}

    print("=" * 70)
    print("2) 생성기 자체 분할(generator_split) 프로토콜: current, team-final (3 시드)")
    print("=" * 70)
    generator_split = {}
    for version in ["current", "team-final"]:
        results = run_many_seeds(
            seeds=seeds3,
            version=version,
            protocol="generator_split",
            label_mode="clicks_only",
            leakage_mode="fixed",
            candidate_pool="full_195",
            negative_source="random",
            objective_override="none",
        )
        generator_split[version] = {"runs": results, "summary": summarize_seeds(results)}

    print("=" * 70)
    print("3) 베이스라인 (team_split / generator_split)")
    print("=" * 70)
    baseline_results = {
        "team_split": compute_baselines_for_protocol(bundle, "team_split"),
        "generator_split": compute_baselines_for_protocol(bundle, "generator_split"),
    }

    print("=" * 70)
    print("4) 분해 실험 (current 코드 위에서 단일 변수 통제)")
    print("=" * 70)
    decomposition = {}

    # (a) leakage: fixed vs leaky
    leaky = run_many_seeds(
        seeds=seeds5, version="current", protocol="team_split", label_mode="clicks_only",
        leakage_mode="leaky", candidate_pool="full_195", negative_source="random", objective_override="none",
    )
    decomposition["leakage"] = {
        "fixed": headline["current"]["runs"],
        "leaky": leaky,
        "fixed_summary": headline["current"]["summary"],
        "leaky_summary": summarize_seeds(leaky),
    }

    # (b) candidate pool
    small = run_many_seeds(
        seeds=seeds3, version="current", protocol="team_split", label_mode="clicks_only",
        leakage_mode="fixed", candidate_pool="small_recent_15", negative_source="random", objective_override="none",
    )
    padded = run_many_seeds(
        seeds=seeds3, version="current", protocol="team_split", label_mode="clicks_only",
        leakage_mode="fixed", candidate_pool="padded_400", negative_source="random", objective_override="none",
    )
    decomposition["candidate_pool"] = {
        "full_195": headline["current"]["runs"][:3],
        "small_recent_15": small,
        "padded_400": padded,
        "full_195_summary": summarize_seeds(headline["current"]["runs"][:3]),
        "small_recent_15_summary": summarize_seeds(small),
        "padded_400_summary": summarize_seeds(padded),
    }

    # (c) negatives: random vs impression
    impression = run_many_seeds(
        seeds=seeds5, version="current", protocol="team_split", label_mode="clicks_only",
        leakage_mode="fixed", candidate_pool="full_195", negative_source="impression", objective_override="none",
    )
    decomposition["negatives"] = {
        "random": headline["current"]["runs"],
        "impression": impression,
        "random_summary": headline["current"]["summary"],
        "impression_summary": summarize_seeds(impression),
    }

    # (d) label assumption: clicks_only vs all_rows
    all_rows = run_many_seeds(
        seeds=seeds5, version="current", protocol="team_split", label_mode="all_rows",
        leakage_mode="fixed", candidate_pool="full_195", negative_source="random", objective_override="none",
    )
    decomposition["label_assumption"] = {
        "clicks_only": headline["current"]["runs"],
        "all_rows": all_rows,
        "clicks_only_summary": headline["current"]["summary"],
        "all_rows_summary": summarize_seeds(all_rows),
    }

    # (e) objective: lambdarank(current) vs binary override
    binary = run_many_seeds(
        seeds=seeds5, version="current", protocol="team_split", label_mode="clicks_only",
        leakage_mode="fixed", candidate_pool="full_195", negative_source="random", objective_override="binary",
    )
    decomposition["objective"] = {
        "lambdarank": headline["current"]["runs"],
        "binary": binary,
        "lambdarank_summary": headline["current"]["summary"],
        "binary_summary": summarize_seeds(binary),
    }

    print("=" * 70)
    print("5) 부트스트랩 신뢰구간 (유저 리샘플 1000x)")
    print("=" * 70)
    bootstrap = {}
    for name, arms in [
        ("leakage", ("fixed", "leaky")),
        ("negatives", ("random", "impression")),
        ("label_assumption", ("clicks_only", "all_rows")),
        ("objective", ("lambdarank", "binary")),
    ]:
        a_key, b_key = arms
        pu_a = pooled_per_user(decomposition[name][a_key])
        pu_b = pooled_per_user(decomposition[name][b_key])
        bootstrap[name] = {}
        for metric_key in ["mrr", "precision@5", "ndcg@5", "coverage@5"]:
            bootstrap[name][metric_key] = M.bootstrap_paired_diff(pu_a, pu_b, metric_key, n_boot=1000, seed=0)

    pu_full = pooled_per_user(decomposition["candidate_pool"]["full_195"])
    pu_small = pooled_per_user(decomposition["candidate_pool"]["small_recent_15"])
    pu_padded = pooled_per_user(decomposition["candidate_pool"]["padded_400"])
    bootstrap["candidate_pool"] = {}
    for metric_key in ["mrr", "precision@5", "ndcg@5", "coverage@5"]:
        bootstrap["candidate_pool"][f"full_vs_small_{metric_key}"] = M.bootstrap_paired_diff(
            pu_full, pu_small, metric_key, n_boot=1000, seed=0
        )
        bootstrap["candidate_pool"][f"full_vs_padded_{metric_key}"] = M.bootstrap_paired_diff(
            pu_full, pu_padded, metric_key, n_boot=1000, seed=0
        )

    # 단일 조건 CI (headline 각 버전)
    ci_headline = {}
    for version in headline:
        pu = pooled_per_user(headline[version]["runs"])
        ci_headline[version] = {
            mk: M.bootstrap_ci(pu, mk, n_boot=1000, seed=0)
            for mk in ["mrr", "precision@5", "ndcg@5", "coverage@5"]
        }

    print("=" * 70)
    print("6) 메타데이터 (git SHA, 데이터 sha256)")
    print("=" * 70)
    # categories.py가 이미 만들어 둔 newsletter_categories.csv를 다시 계산해 LOO
    # 정확도/선택된 k/소스별 건수를 리포트에 남긴다(스펙 1번: "k는 ... LOO 교차검증으로
    # 선택한다. report LOO accuracy and per-source counts"). 195건 기준 1초 내외로
    # 끝나는 가벼운 진단이라 run_pipeline처럼 캐시할 필요는 없다.
    cat_result = CAT.recover_categories()
    category_recovery = {
        "chosen_k": cat_result.chosen_k,
        "loo_accuracy_by_k": {str(k): v for k, v in sorted(cat_result.loo_scores.items())},
        "n_direct_labels": sum(1 for r in cat_result.rows if r["source"] not in ("knn", "unlabeled_no_embedding")),
        "source_counts": dict(cat_result.source_counts),
    }

    meta = {
        "generated_at": datetime.now().isoformat(),
        "git": {
            "current_head": git_sha(str(WORKTREE_ROOT), "HEAD"),
            "current_branch": subprocess.run(
                ["git", "-C", str(WORKTREE_ROOT), "branch", "--show-current"],
                capture_output=True, text=True,
            ).stdout.strip(),
            "team_final_sha": git_sha("/private/tmp/repro-team", "HEAD"),
            "fix_snapshot_sha": git_sha("/private/tmp/repro-fix", "HEAD"),
        },
        "data_sha256": {
            "synthetic_ctr_logs.csv": sha256_of(FL.SYNTH_DIR / "synthetic_ctr_logs.csv"),
            "newsletters_export.csv": sha256_of(FL.SYNTH_DIR / "newsletters_export.csv"),
            "newsletters_bge_m3.npy": sha256_of(FL.EMB_DIR / "newsletters_bge_m3.npy"),
            "newsletter_categories.csv": sha256_of(FL.CATEGORIES_CSV),
        },
        "dataset_window": {
            "start": bundle.dataset_start_time.isoformat(),
            "end": bundle.dataset_end_time.isoformat(),
            "n_users": len(bundle.users),
            "n_newsletters": len(bundle.newsletters),
            "n_ctr_logs": len(bundle.ctr_logs),
            "n_clicks": int((bundle.ctr_logs["is_clicked"] == 1).sum()),
        },
        "seeds": {"headline": seeds5, "decomposition_secondary": seeds3},
        "category_recovery": category_recovery,
    }

    final = {
        "meta": meta,
        "headline": {v: {"summary": d["summary"]} for v, d in headline.items()},
        "headline_ci": ci_headline,
        "generator_split": {v: {"summary": d["summary"]} for v, d in generator_split.items()},
        "baselines": {
            proto: {name: r["aggregate"] for name, r in res.items()} for proto, res in baseline_results.items()
        },
        "decomposition_summary": {
            name: {k: v for k, v in d.items() if k.endswith("_summary")} for name, d in decomposition.items()
        },
        "decomposition_bootstrap": bootstrap,
    }

    (REPORT_DIR / "team_repro_v1.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n결과 저장: {REPORT_DIR / 'team_repro_v1.json'}")

    # 전체 원자료(모든 seed의 개별 run 결과)도 별도 보관 (리포트 작성/재검증용)
    raw_all = {"headline": headline, "generator_split": generator_split, "decomposition": decomposition}
    (REPORT_DIR / "team_repro_v1_raw.json").write_text(
        json.dumps(raw_all, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(f"원자료 저장: {REPORT_DIR / 'team_repro_v1_raw.json'}")


if __name__ == "__main__":
    main()
