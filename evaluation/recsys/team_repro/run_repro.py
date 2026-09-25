"""v2: 팀 베이스라인 재현 실험의 최상위 오케스트레이터 (어드버서리얼 리뷰 이후
재작성 - 이전 버전은 git 이력에서 v1 커밋들로 확인 가능하다).

바뀐 것 (요구사항 1~8 대응):
  - team_split 프로토콜의 모든 arm/시드/버전이 정확히 같은 고정 정답 구간
    시작 시각(answer_start)을 공유한다(딱 한 번 계산해 pipeline.py 서브프로세스에
    넘긴다) - 베이스라인도 이 값을 그대로 쓴다.
  - 헤드라인은 이제 point-in-time(1차)과 as-written/leaky(2차) 두 행을 함께
    낸다. team-final은 실제 evaluate_results.py 정답 정의(NOW()-6DAYS) 행도
    추가로 낸다.
  - label_assumption(all_rows) decomposition은 제거했다(퇴화된 실험 - 정답 라벨이
    없어 AUC/부트스트랩이 무의미했다). 이유는 v2 리포트 본문에 남긴다.
  - negatives(impression) arm은 이제 answer_start를 공유하고, lambdarank 그룹을
    user_id 단위로 묶는다(pipeline.py가 negative_source=="impression"일 때
    자동 적용).
  - candidate_pool: padded_400은 실제로 400건을 쓰고(pipeline.py 자체 assert로
    확인), small_recent_15는 정답을 풀 안으로 제한한다.
  - generator_split 모델 행은 ctr_logs_train.csv만으로 학습한다(진짜 cold-item
    평가).
  - 캐시 키에 하네스/엔진 git SHA + config_hash를 포함한다.
  - 부트스트랩은 유저만이 아니라 시드 x 유저를 함께 재표본하는 nested
    bootstrap을 쓴다(metrics.nested_bootstrap_*).

결과: reports/recsys/team_repro_v2.json(전체 원자료) - make_report_v2.py가 이를
읽어 한국어 리포트(team_repro_v2.md)를 만든다.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

TEAM_REPRO_DIR = Path(__file__).resolve().parent
WORKTREE_ROOT = TEAM_REPRO_DIR.parents[2]  # .../evaluation/recsys/team_repro -> worktree root
VENV_PYTHON = WORKTREE_ROOT / ".venv" / "bin" / "python"
RESULTS_DIR = Path("/tmp/team_repro_runs_v2")
REPORT_DIR = WORKTREE_ROOT / "reports" / "recsys"

ARCHIVE_ROOT = Path("/tmp/team_repro_code_archive")
ENGINE_ROOTS = {
    "current": str(WORKTREE_ROOT / "ai_workspace" / "recommend_engine"),
    "team-final": str(ARCHIVE_ROOT / "team-final" / "ai_workspace" / "recommend_engine"),
    "fix-snapshot": str(ARCHIVE_ROOT / "fix-snapshot" / "ai_workspace" / "recommend_engine"),
}
CODE_REFS = {"current": "HEAD", "team-final": "team-final", "fix-snapshot": "port/fix-snapshot"}

sys.path.insert(0, str(TEAM_REPRO_DIR))
import file_loader as FL  # noqa: E402
import baselines as BL  # noqa: E402
import metrics as M  # noqa: E402
import categories as CAT  # noqa: E402
import protocol as PR  # noqa: E402
import config_loader as CFG  # noqa: E402

# Evaluator는 src.core.evaluator에 numpy 외 의존성이 없다(팀 코드 수정 없이 그대로 재사용,
# torch 스텁도 필요 없다). 베이스라인 평가 전용으로 "current" 엔진 루트에서 한 번만 import한다.
sys.path.insert(0, ENGINE_ROOTS["current"])
from src.core.evaluator import Evaluator  # noqa: E402

import numpy as np  # noqa: E402


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def git_sha(worktree: str, ref: str) -> str:
    out = subprocess.run(
        ["git", "-C", worktree, "rev-parse", ref], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def ensure_code_archives() -> None:
    """team-final/fix-snapshot 소스를 `git archive`로 /tmp에 풀어둔다(존재하지
    않으면). 워크트리를 추가로 만들지 않는다."""
    for version, ref in CODE_REFS.items():
        if version == "current":
            continue
        target = ARCHIVE_ROOT / version
        if (target / "ai_workspace").exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            f"git -C {WORKTREE_ROOT} archive {ref} | tar -x -C {target}",
            shell=True, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git archive 실패 ({ref}): {proc.stderr}")


def run_pipeline(*, out_tag: str, answer_start: Optional[str] = None, **kw) -> dict:
    out_path = RESULTS_DIR / f"{out_tag}.json"
    if out_path.exists():
        return json.loads(out_path.read_text(encoding="utf-8"))

    cmd = [
        str(VENV_PYTHON), str(TEAM_REPRO_DIR / "pipeline.py"),
        "--engine-root", kw["engine_root"],
        "--version", kw["version"],
        "--protocol", kw["protocol"],
        "--label-mode", kw["label_mode"],
        "--leakage-mode", kw["leakage_mode"],
        "--candidate-pool", kw["candidate_pool"],
        "--negative-source", kw["negative_source"],
        "--objective-override", kw["objective_override"],
        "--code-sha", kw["code_sha"],
        "--harness-sha", kw["harness_sha"],
        "--seed", str(kw["seed"]),
        "--out", str(out_path),
    ]
    if answer_start:
        cmd += ["--answer-start", answer_start]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(WORKTREE_ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[FAIL] {out_tag} ({time.time()-t0:.1f}s)", file=sys.stderr)
        print(proc.stdout[-4000:], file=sys.stderr)
        print(proc.stderr[-4000:], file=sys.stderr)
        raise RuntimeError(f"pipeline.py 실패: {out_tag}")
    print(f"[OK] {out_tag} ({time.time()-t0:.1f}s)", flush=True)
    return json.loads(out_path.read_text(encoding="utf-8"))


MAX_WORKERS = int(os.environ.get("TEAM_REPRO_WORKERS", "3"))


def run_many_seeds(seeds: List[int], code_shas: Dict[str, str], harness_sha: str, config_hashes: Dict, answer_start: Optional[str], **kw) -> List[dict]:
    """seed별 실행은 서로 완전히 독립적이다(각자 고유한 out_tag 파일에 쓴다) - 여러
    시드를 동시에 서브프로세스로 띄워 벽시계 시간을 줄인다(캐시 히트는 즉시
    반환되므로 워커 풀이 낭비되지 않는다). 순서는 항상 `seeds` 순서로 반환한다."""
    version = kw["version"]
    code_sha = code_shas[version]
    cfg_hash = config_hashes.get((version, kw["objective_override"]), "unk")

    def _one(seed: int) -> dict:
        tag = "__".join([
            kw["version"], kw["protocol"], kw["label_mode"], kw["leakage_mode"],
            kw["candidate_pool"], kw["negative_source"], kw["objective_override"],
            f"seed{seed}", f"h{harness_sha[:10]}", f"c{code_sha[:10]}", f"cfg{cfg_hash}", "v2",
        ])
        return run_pipeline(
            out_tag=tag, answer_start=answer_start, seed=seed,
            engine_root=ENGINE_ROOTS[version], code_sha=code_sha, harness_sha=harness_sha, **kw,
        )

    if MAX_WORKERS <= 1 or len(seeds) <= 1:
        return [_one(s) for s in seeds]

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(seeds))) as pool:
        futures = {pool.submit(_one, s): i for i, s in enumerate(seeds)}
        results_by_idx: Dict[int, dict] = {}
        for fut in as_completed(futures):
            results_by_idx[futures[fut]] = fut.result()
    return [results_by_idx[i] for i in range(len(seeds))]


def summarize_field(results: List[dict], field: str) -> dict:
    if not results:
        return {}
    keys = results[0][field]["aggregate_metrics"].keys()
    out = {}
    for k in keys:
        vals = [r[field]["aggregate_metrics"][k] for r in results if k in r[field]["aggregate_metrics"]]
        out[k] = {"mean": float(np.mean(vals)), "std": float(np.std(vals)), "values": vals}
    out["n_seeds"] = len(results)
    out["elapsed_sec_total"] = round(sum(r["elapsed_sec"] for r in results), 1)
    best_iters = [r.get("best_iteration") for r in results if r.get("best_iteration") is not None]
    if best_iters:
        out["best_iteration"] = best_iters
    n_distinct = [r.get("n_distinct_scores_primary") for r in results if r.get("n_distinct_scores_primary") is not None]
    if n_distinct:
        out["n_distinct_scores_primary"] = n_distinct
    return out


def pooled_per_user_by_seed(results: List[dict], field: str) -> List[Dict[int, Dict[str, float]]]:
    out = []
    for r in results:
        out.append({int(uid): m for uid, m in r[field]["per_user_metrics"].items()})
    return out


def compute_baselines_for_protocol(bundle, protocol: str, answer_start=None, seed: int = 42) -> dict:
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
        cutoff = bundle.dataset_end_time.to_pydatetime()
    else:
        # v2 REQUIRED CHANGE #1: 헤드라인/모델과 정확히 동일한 고정 answer_start를 쓴다
        # (v1은 여기서 80% 지점을 다시 계산해 시드/모델 쪽과 몇 초씩 드리프트했다 - MINOR).
        cutoff = answer_start
        train_logs = logs[logs["timestamp"] < cutoff]
        valid_logs = logs[logs["timestamp"] >= cutoff]
        ground_truth = valid_logs.groupby("user_id")["news_letter_id"].apply(set).to_dict()

    evaluator = Evaluator(k_values=[5, 10, 20])
    all_recs = BL.compute_all_baselines(
        bundle, category_map, candidate_ids, user_ids, train_logs, cutoff, top_k=20, seed=seed
    )
    out = {}
    for name, recs in all_recs.items():
        per_user = M.per_user_metrics(evaluator, recs, ground_truth, category_map)
        entry = {"aggregate": M.aggregate(per_user), "per_user": per_user}
        if protocol != "generator_split":
            clicks_before = bundle.ctr_logs[(bundle.ctr_logs["is_clicked"] == 1) & (bundle.ctr_logs["timestamp"] < cutoff)]
            cold_ids, warm_ids = PR.cold_warm_split(user_ids, clicks_before, cutoff)
            entry["cold"] = PR.split_metrics_by_group(per_user, cold_ids)
            entry["warm"] = PR.split_metrics_by_group(per_user, warm_ids)
            seen = PR.seen_items_by_user(clicks_before, cutoff)
            filtered_recs = PR.filter_seen(recs, seen)
            filtered_per_user = M.per_user_metrics(evaluator, filtered_recs, ground_truth, category_map)
            entry["seen_filtered"] = {"aggregate": M.aggregate(filtered_per_user), "per_user": filtered_per_user}
            entry["seen_share_top5"] = PR.seen_share_in_topk(recs, seen, k=5)
        out[name] = entry
    return out


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ensure_code_archives()

    bundle = FL.load_archive_bundle()
    clicks = bundle.ctr_logs[bundle.ctr_logs["is_clicked"] == 1]
    answer_start_ts = PR.compute_fixed_answer_start(clicks, val_ratio=0.2)
    answer_start = answer_start_ts.isoformat()
    print(f"고정 answer_start (team_split): {answer_start}")

    harness_sha = git_sha(str(WORKTREE_ROOT), "HEAD")
    code_shas = {
        "current": harness_sha,
        "team-final": git_sha(str(WORKTREE_ROOT), "team-final"),
        "fix-snapshot": git_sha(str(WORKTREE_ROOT), "port/fix-snapshot"),
    }
    print(f"harness_sha(=current) {harness_sha[:10]} / team-final {code_shas['team-final'][:10]} / fix-snapshot {code_shas['fix-snapshot'][:10]}")

    config_hashes = {}
    config_overrides_by = {}
    for version in ["current", "team-final", "fix-snapshot"]:
        for override in [None, "binary"]:
            _cfg, chash, overrides = CFG.load_version_config(ENGINE_ROOTS[version], seed=42, top_k=20, objective_override=override)
            config_hashes[(version, override or "none")] = chash
            config_overrides_by[(version, override or "none")] = overrides

    # v2: v1은 헤드라인 5시드/보조 3시드로 arm마다 시드 수가 달랐다. v2는 point-in-time
    # 프로토콜이 시드마다 학습(train_all)까지 다시 도는 이중 추론 구조라 arm당 비용이
    # 커져서, 모든 arm에 동일한 3시드를 쓴다 - 대신 어디서도 "n=3"을 숨기지 않는다
    # (요구사항 8: n을 결과표에 항상 명시).
    if os.environ.get("TEAM_REPRO_QUICK"):
        seeds_main = [42]
        seeds_fixsnap = [42]
    else:
        seeds_main = [42, 43, 44]
        # fix-snapshot은 point-in-time cutoff을 메모이즈하지 않는 코드(모듈 docstring/
        # compute_history_embedding 참고)라 학습 피처 생성이 훨씬 느리다 - headline
        # 외 다른 arm에는 등장하지 않으므로, 여기서만 시드를 2개로 줄인다(n=2를 리포트에
        # 그대로 명시한다 - 요구사항 8).
        seeds_fixsnap = [42, 43]
    seeds5 = seeds3 = seeds_main

    common = dict(
        protocol="team_split", label_mode="clicks_only", leakage_mode="fixed",
        candidate_pool="full_195", negative_source="random", objective_override="none",
        harness_sha=harness_sha, code_shas=code_shas, config_hashes=config_hashes, answer_start=answer_start,
    )

    print("=" * 70)
    print("1) 헤드라인: 3개 코드 버전 x team_split, point-in-time(1차) + as-written(2차)")
    print("=" * 70)
    headline = {}
    for version, seeds in [("current", seeds5), ("team-final", seeds5), ("fix-snapshot", seeds_fixsnap)]:
        results = run_many_seeds(seeds=seeds, version=version, **common)
        headline[version] = {
            "runs": results,
            "primary_summary": summarize_field(results, "primary"),
            "as_written_summary": summarize_field(results, "as_written"),
        }

    team_final_written_runs = headline["team-final"]["runs"]

    print("=" * 70)
    print("2) generator_split: 학습을 ctr_logs_train.csv로 제한한 모델 (current, team-final)")
    print("=" * 70)
    gen_common = dict(common)
    gen_common["protocol"] = "generator_split"
    generator_split = {}
    for version in ["current", "team-final"]:
        results = run_many_seeds(seeds=seeds3, version=version, **{**gen_common, "answer_start": None})
        generator_split[version] = {"runs": results, "summary": summarize_field(results, "primary")}

    print("=" * 70)
    print("3) 베이스라인 (team_split - 고정 answer_start 공유 / generator_split)")
    print("=" * 70)
    baseline_results = {
        "team_split": compute_baselines_for_protocol(bundle, "team_split", answer_start=answer_start_ts),
        "generator_split": compute_baselines_for_protocol(bundle, "generator_split"),
    }

    print("=" * 70)
    print("4) 분해 실험 (current 코드, point-in-time 1차 프로토콜 위에서 단일 변수 통제)")
    print("=" * 70)
    decomposition = {}

    leaky = run_many_seeds(seeds=seeds5, version="current", **{**common, "leakage_mode": "leaky"})
    decomposition["leakage"] = {
        "fixed": headline["current"]["runs"], "leaky": leaky,
        "fixed_summary": headline["current"]["primary_summary"], "leaky_summary": summarize_field(leaky, "primary"),
    }

    small = run_many_seeds(seeds=seeds3, version="current", **{**common, "candidate_pool": "small_recent_15"})
    padded = run_many_seeds(seeds=seeds3, version="current", **{**common, "candidate_pool": "padded_400"})
    decomposition["candidate_pool"] = {
        "full_195": headline["current"]["runs"][:3], "small_recent_15": small, "padded_400": padded,
        "full_195_summary": summarize_field(headline["current"]["runs"][:3], "primary"),
        "small_recent_15_summary": summarize_field(small, "primary"),
        "padded_400_summary": summarize_field(padded, "primary"),
    }

    impression = run_many_seeds(seeds=seeds5, version="current", **{**common, "negative_source": "impression"})
    decomposition["negatives"] = {
        "random": headline["current"]["runs"], "impression": impression,
        "random_summary": headline["current"]["primary_summary"], "impression_summary": summarize_field(impression, "primary"),
    }

    binary = run_many_seeds(seeds=seeds5, version="current", **{**common, "objective_override": "binary"})
    decomposition["objective"] = {
        "lambdarank": headline["current"]["runs"], "binary": binary,
        "lambdarank_summary": headline["current"]["primary_summary"], "binary_summary": summarize_field(binary, "primary"),
    }
    # v2 REQUIRED CHANGE #3: label_assumption(all_rows) decomposition은 제거했다 -
    # 퇴화된 실험이라(모든 유저가 195건 전부를 '클릭'한 것으로 취급되어 negative
    # sampler가 negative를 하나도 못 찾고, AUC undefined, best_iter=1, 5개 시드가
    # 전부 동일한 값을 낸다) 신뢰구간을 보고하는 것 자체가 실측을 가장한다.

    print("=" * 70)
    print("5) 중첩 부트스트랩 (시드 x 유저, 1000회)")
    print("=" * 70)
    bootstrap = {}
    for name, arms in [
        ("leakage", ("fixed", "leaky")),
        ("negatives", ("random", "impression")),
        ("objective", ("lambdarank", "binary")),
    ]:
        a_key, b_key = arms
        pu_a = pooled_per_user_by_seed(decomposition[name][a_key], "primary")
        pu_b = pooled_per_user_by_seed(decomposition[name][b_key], "primary")
        bootstrap[name] = {}
        for metric_key in ["mrr", "precision@5", "ndcg@5", "coverage@5"]:
            bootstrap[name][metric_key] = M.nested_bootstrap_paired_diff(pu_a, pu_b, metric_key, n_boot=1000, seed=0)

    pu_full = pooled_per_user_by_seed(decomposition["candidate_pool"]["full_195"], "primary")
    pu_small = pooled_per_user_by_seed(decomposition["candidate_pool"]["small_recent_15"], "primary")
    pu_padded = pooled_per_user_by_seed(decomposition["candidate_pool"]["padded_400"], "primary")
    bootstrap["candidate_pool"] = {}
    for metric_key in ["mrr", "precision@5", "ndcg@5", "coverage@5"]:
        bootstrap["candidate_pool"][f"full_vs_small_{metric_key}"] = M.nested_bootstrap_paired_diff(pu_full, pu_small, metric_key, n_boot=1000, seed=0)
        bootstrap["candidate_pool"][f"full_vs_padded_{metric_key}"] = M.nested_bootstrap_paired_diff(pu_full, pu_padded, metric_key, n_boot=1000, seed=0)

    # 헤드라인 각 버전의 1차/2차 단일-조건 신뢰구간 (시드 x 유저)
    ci_headline = {}
    for version in headline:
        pu_primary = pooled_per_user_by_seed(headline[version]["runs"], "primary")
        pu_aw = pooled_per_user_by_seed(headline[version]["runs"], "as_written")
        ci_headline[version] = {
            "primary": {mk: M.nested_bootstrap_ci(pu_primary, mk, n_boot=1000, seed=0) for mk in ["mrr", "precision@5", "ndcg@5", "coverage@5"]},
            "as_written": {mk: M.nested_bootstrap_ci(pu_aw, mk, n_boot=1000, seed=0) for mk in ["mrr", "precision@5", "ndcg@5", "coverage@5"]},
        }
        pu_primary_diff = pu_primary
        pu_aw_diff = pu_aw
        ci_headline[version]["leak_effect"] = {
            mk: M.nested_bootstrap_paired_diff(pu_primary_diff, pu_aw_diff, mk, n_boot=1000, seed=0)
            for mk in ["mrr", "precision@5", "ndcg@5", "coverage@5"]
        }

    print("=" * 70)
    print("6) team-final-as-written (실제 evaluate_results.py 정답 정의)")
    print("=" * 70)
    tf_written_summary = {}
    tf_written_runs = [r["team_final_written"] for r in team_final_written_runs if "team_final_written" in r]
    if tf_written_runs:
        keys = tf_written_runs[0]["aggregate_metrics"].keys()
        for k in keys:
            vals = [r["aggregate_metrics"][k] for r in tf_written_runs if k in r["aggregate_metrics"]]
            tf_written_summary[k] = {"mean": float(np.mean(vals)), "std": float(np.std(vals)), "values": vals}
        tf_written_summary["n_seeds"] = len(tf_written_runs)
        tf_written_summary["note"] = tf_written_runs[0].get("note")

    print("=" * 70)
    print("7) 데이터 구조 / 기저율 진단 (요구사항 7)")
    print("=" * 70)
    clicks_before_answer = bundle.ctr_logs[(bundle.ctr_logs["is_clicked"] == 1) & (bundle.ctr_logs["timestamp"] < answer_start_ts)]
    all_user_ids = bundle.users["user_id"].tolist()
    cold_ids, warm_ids = PR.cold_warm_split(all_user_ids, clicks_before_answer, answer_start_ts)
    answer_logs = bundle.ctr_logs[(bundle.ctr_logs["is_clicked"] == 1) & (bundle.ctr_logs["timestamp"] >= answer_start_ts)]
    n_evaluated_users = answer_logs["user_id"].nunique()
    n_answer_clicks = len(answer_logs)
    n_candidates_full = len(bundle.newsletters)
    random_p5_base_rate = float(np.mean([
        len(g) / n_candidates_full for _, g in answer_logs.groupby("user_id")["news_letter_id"].apply(set).items()
    ])) if n_evaluated_users else float("nan")

    exposures_per_persona = bundle.ctr_logs.groupby("user_id").size()
    session_span = bundle.ctr_logs.groupby("user_id")["timestamp"].agg(lambda s: (s.max() - s.min()).total_seconds())

    data_structure = {
        "n_users_total": len(bundle.users),
        "n_newsletters": len(bundle.newsletters),
        "n_ctr_logs": len(bundle.ctr_logs),
        "n_clicks": int((bundle.ctr_logs["is_clicked"] == 1).sum()),
        "click_rate": float((bundle.ctr_logs["is_clicked"] == 1).mean()),
        "dataset_window_start": bundle.dataset_start_time.isoformat(),
        "dataset_window_end": bundle.dataset_end_time.isoformat(),
        "dataset_window_seconds": float((bundle.dataset_end_time - bundle.dataset_start_time).total_seconds()),
        "exposures_per_persona_min": int(exposures_per_persona.min()),
        "exposures_per_persona_max": int(exposures_per_persona.max()),
        "session_span_seconds_mean": float(session_span.mean()),
        "answer_start": answer_start,
        "n_cold_users": len(cold_ids),
        "n_warm_users": len(warm_ids),
        "n_evaluated_users_in_answer_window": int(n_evaluated_users),
        "n_answer_clicks": int(n_answer_clicks),
        "random_p5_base_rate_mean_answer_density": random_p5_base_rate,
    }

    print("=" * 70)
    print("8) 메타데이터 (git SHA, 데이터 sha256, 카테고리 복원)")
    print("=" * 70)
    cat_result = CAT.recover_categories()
    category_recovery = {
        "chosen_k": cat_result.chosen_k,
        "loo_accuracy_by_k": {str(k): v for k, v in sorted(cat_result.loo_scores.items())},
        "n_direct_labels": cat_result.n_direct_for_loo,
        "source_counts": dict(cat_result.source_counts),
        "loo_ci_chosen_k_wilson95": list(cat_result.loo_ci),
        "loo_ci_note": "chosen_k는 9개 k 후보 중 최선값이라(best-of-9) 이 구간조차 낙관적일 수 있음 - 다중비교 보정 아님.",
    }

    meta = {
        "generated_at": datetime.now().isoformat(),
        "report_version": "v2",
        "superseded_report": "team_repro_v1 (BLOCKER 다수로 unsound 판정, 상단 배너 참고)",
        "git": {
            "harness_sha": harness_sha,
            "current_branch": subprocess.run(
                ["git", "-C", str(WORKTREE_ROOT), "branch", "--show-current"], capture_output=True, text=True,
            ).stdout.strip(),
            "team_final_sha": code_shas["team-final"],
            "fix_snapshot_sha": code_shas["fix-snapshot"],
        },
        "config_hashes": {f"{v}|{o}": h for (v, o), h in config_hashes.items()},
        "config_overrides": {f"{v}|{o}": ov for (v, o), ov in config_overrides_by.items() if ov},
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
        "seeds": {
            "headline_current_team_final": seeds5,
            "headline_fix_snapshot": seeds_fixsnap,
            "decomposition_secondary": seeds3,
        },
        "category_recovery": category_recovery,
        "answer_start": answer_start,
    }

    final = {
        "meta": meta,
        "data_structure": data_structure,
        "headline": {
            v: {"primary_summary": d["primary_summary"], "as_written_summary": d["as_written_summary"]}
            for v, d in headline.items()
        },
        "headline_ci": ci_headline,
        "team_final_written_summary": tf_written_summary,
        "generator_split": {v: {"summary": d["summary"]} for v, d in generator_split.items()},
        "baselines": {
            proto: {name: {k2: v2 for k2, v2 in r.items() if k2 != "per_user"} for name, r in res.items()}
            for proto, res in baseline_results.items()
        },
        "decomposition_summary": {
            name: {k: v for k, v in d.items() if k.endswith("_summary")} for name, d in decomposition.items()
        },
        "decomposition_bootstrap": bootstrap,
    }

    (REPORT_DIR / "team_repro_v2.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n결과 저장: {REPORT_DIR / 'team_repro_v2.json'}")

    raw_all = {
        "headline": headline, "generator_split": generator_split, "decomposition": decomposition,
        "baselines_full": baseline_results,
    }
    (REPORT_DIR / "team_repro_v2_raw.json").write_text(
        json.dumps(raw_all, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(f"원자료 저장: {REPORT_DIR / 'team_repro_v2_raw.json'}")


if __name__ == "__main__":
    main()
