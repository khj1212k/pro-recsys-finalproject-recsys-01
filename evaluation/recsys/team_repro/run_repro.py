"""v2.1: 팀 베이스라인 재현 실험의 최상위 오케스트레이터.

v2(v1 어드버서리얼 리뷰 이후 재작성): 모든 arm/시드/버전이 고정 answer_start 하나를
공유하고, point-in-time(1차)과 as-written(2차) 추론을 함께 내며, label_assumption
arm을 제거하고, generator_split 학습을 ctr_logs_train.csv로 제한하고, 시드 x 유저
nested bootstrap을 쓴다(자세한 이력은 git log). v2.1은 v2 재검토(blocker 2 /
major 4 / minor 다수)를 반영한다:

  - team-final-as-written 정답을 '창 안의 클릭'으로 바로잡고(pipeline.py), 같은
    정의의 무작위 추천 기준값을 함께 낸다 - v2의 '노출 행 전체' 정의는 무작위도
    1.0이 되는 정의 오류로 이름 붙여 기록만 남긴다.
  - 조기 종료가 1라운드에서 멈춘(best_iteration=1) 퇴화 시드를 표시하고, 동점을
    무작위로 깬 추첨 평균(--tie-draws)과 고정 100라운드 학습 민감도 arm, binary
    objective 위의 누출 분해를 추가했다.
  - padded_400 arm은 제거(학습 negative까지 바꿔 풀 크기 효과로 읽을 수 없음).
    small_recent_15는 전체 풀과의 '효과' 대신 같은 풀·같은 정답 위의 풀 내부
    베이스라인과 비교한다.
  - 모델-베이스라인 쌍 비교 CI, 모델의 cold/warm·seen/unshown 필터 지표, 버전 간
    비교(team-final - current)와 누출 효과의 차이(DiD)를 계산한다.
  - 같은 모델을 공유하는 비교(누출 효과)는 시드를 쌍으로 재표본한다.
  - 캐시 키에 작업 트리 변경(diff 해시)·데이터 sha256·answer_start·top_k·추첨
    횟수·라운드 정책을 넣고, 코드 아카이브는 ref SHA가 바뀌면 다시 푼다.
  - 시드: current/team-final 5개, fix-snapshot 3개(느림). 로컬 부하 제한을 위해
    기본 워커 1개, `--max-new-runs N`으로 새 실행 수를 끊어 여러 번에 나눠 돌릴 수
    있다(완료된 실행은 캐시에서 이어받음).

v2.2는 v2.1 재검토(major 2)를 반영한다:

  - v2.1이 '퇴화'라고 부른 lambdarank 조기 종료의 1라운드 종료는 조기 종료 지표의
    아티팩트였다(LightGBM NDCG가 동점을 행 순서로 깨고, 엔진의 학습 데이터 생성이
    positive를 먼저 쌓는다). 모든 arm이 조기 종료용 inner-valid를 고정 시드로 섞어
    쓰고(pipeline.py --es-valid-order shuffled), 엔진 순서 그대로 조기 종료한 v2.1
    동작은 `current_es_engine_order` 진단 arm으로만 남긴다. 실행마다 기록되는
    es_tie_diagnostic을 `early_stopping_tie_artefact`로 모은다.
  - 버전 비교(team-final - current)는 objective에 따라 조기 종료 동작이 달라 교란돼
    있었다. 같은 조건끼리 세 가지로 비교한다: 각 버전 config 그대로(섞은 조기 종료),
    둘 다 binary·AUC 조기 종료, 둘 다 조기 종료 없는 100라운드(`team_final_fixed100`
    arm 추가). v2.1의 비교(엔진 순서 조기 종료)는 재현해 표시만 한다.
  - 재현 대조 기준을 v2.1 원자료로 바꿨다: AUC·고정 라운드 arm은 섞기와 무관하게
    같아야 하고, NDCG 조기 종료 arm은 달라지는 게 정상이다.

결과: reports/recsys/team_repro_v2.json(요약) + team_repro_v2_raw.json(실행별 원자료).
make_report_v2.py가 요약 JSON만 읽어 한국어 리포트(team_repro_v2.md)를 만든다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

TEAM_REPRO_DIR = Path(__file__).resolve().parent
WORKTREE_ROOT = TEAM_REPRO_DIR.parents[2]  # .../evaluation/recsys/team_repro -> worktree root
VENV_PYTHON = WORKTREE_ROOT / ".venv" / "bin" / "python"
RESULTS_DIR = Path(os.environ.get("TEAM_REPRO_RESULTS_DIR", "/tmp/team_repro_runs_v2"))
REPORT_DIR = Path(os.environ.get("TEAM_REPRO_REPORT_DIR", str(WORKTREE_ROOT / "reports" / "recsys")))

ARCHIVE_ROOT = Path(os.environ.get("TEAM_REPRO_ARCHIVE_ROOT", "/tmp/team_repro_code_archive"))
ENGINE_ROOTS = {
    "current": str(WORKTREE_ROOT / "ai_workspace" / "recommend_engine"),
    "team-final": str(ARCHIVE_ROOT / "team-final" / "ai_workspace" / "recommend_engine"),
    "fix-snapshot": str(ARCHIVE_ROOT / "fix-snapshot" / "ai_workspace" / "recommend_engine"),
}
CODE_REFS = {"current": "HEAD", "team-final": "team-final", "fix-snapshot": "port/fix-snapshot"}
# 캐시 키에 넣을 '작업 트리 변경' 감시 대상(하네스 + current 엔진)
DIRTY_WATCH_PATHS = ["evaluation/recsys/team_repro", "ai_workspace/recommend_engine"]
# pipeline.py 서브프로세스가 실제로 읽는 하네스 파일 - 이 파일들의 '내용'이 캐시 키다.
PIPELINE_INPUT_FILES = ["pipeline.py", "file_loader.py", "config_loader.py", "metrics.py", "protocol.py"]

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

TOP_K = 20
TIE_DRAWS = 30  # 1차/2차 추론 동점 무작위 추첨 횟수
RANDOM_DRAWS = 100  # random 베이스라인 기대값 근사용 추첨 횟수
FIXED_ROUNDS = 100  # 고정 라운드 민감도 arm (사전 선언값 - 정답 구간으로 고르지 않았다)
ES_VALID_ORDER_DEFAULT = "shuffled"  # 조기 종료용 inner-valid 행 순서(pipeline.py --es-valid-order)
LOW_BEST_ITERATION_MAX = 5  # pipeline.LOW_BEST_ITERATION_MAX와 같은 값(테스트로 고정)
BOOT_METRICS = ["mrr", "precision@5", "ndcg@5", "coverage@5"]
MAX_WORKERS = int(os.environ.get("TEAM_REPRO_WORKERS", "1"))


class RunBudgetExhausted(RuntimeError):
    """--max-new-runs 한도에 도달 - 완료된 실행은 캐시에 남아 있으니 다시 실행하면 이어간다."""


_BUDGET = {"max_new": None, "started": 0}
_BUDGET_LOCK = threading.Lock()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def git_sha(worktree: str, ref: str) -> str:
    out = subprocess.run(
        ["git", "-C", worktree, "rev-parse", ref], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def working_tree_state(worktree: str, paths: Sequence[str]) -> dict:
    """HEAD SHA만으로는 커밋 안 된 수정이 캐시 키에 안 잡힌다(v2 리뷰: v2 코드가 v1
    SHA로 캐시된 실행 10건). 감시 경로의 `git diff HEAD` + untracked 파일 내용을
    해시해 키에 넣는다. dirty=False면 해시는 빈 문자열의 해시다."""
    diff = subprocess.run(
        ["git", "-C", worktree, "diff", "HEAD", "--", *paths], capture_output=True, text=True, check=True
    ).stdout
    untracked = subprocess.run(
        ["git", "-C", worktree, "ls-files", "--others", "--exclude-standard", "--", *paths],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    h = hashlib.sha256(diff.encode("utf-8"))
    for rel in sorted(untracked):
        p = Path(worktree) / rel
        if p.is_file() and "__pycache__" not in rel:
            h.update(rel.encode("utf-8"))
            h.update(p.read_bytes())
    dirty = bool(diff.strip()) or any("__pycache__" not in u for u in untracked)
    return {"dirty": dirty, "diff_hash": h.hexdigest()[:16]}


def content_hash(paths: Sequence[Path], root: Path) -> str:
    """파일 내용(+상대 경로)의 sha256. 커밋 여부와 무관하게 실제로 실행될 코드를 식별한다
    - HEAD SHA를 키로 쓰면 커밋 안 된 수정은 못 잡고(v2 리뷰), 반대로 리포트 스크립트만
    바꾼 커밋에도 모든 실행이 무효화된다."""
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(str(p.relative_to(root)).encode("utf-8"))
        h.update(p.read_bytes())
    return h.hexdigest()


def harness_input_hash() -> str:
    return content_hash([TEAM_REPRO_DIR / f for f in PIPELINE_INPUT_FILES], TEAM_REPRO_DIR)


def engine_content_hash(engine_root: str) -> str:
    root = Path(engine_root)
    files = [p for p in (root / "src").rglob("*.py") if "__pycache__" not in p.parts]
    files += [root / "config" / "config.yaml"]
    return content_hash(files, root)


def ensure_code_archives() -> Dict[str, str]:
    """team-final/fix-snapshot 소스를 `git archive`로 ARCHIVE_ROOT에 풀어둔다. 대상
    ref의 SHA를 `.archive_sha`에 적어두고, 다르면(ref가 옮겨졌거나 예전 아카이브)
    지우고 다시 푼다 - v2는 존재 여부만 봐서 오래된 아카이브를 그대로 썼다."""
    shas = {}
    for version, ref in CODE_REFS.items():
        if version == "current":
            continue
        sha = git_sha(str(WORKTREE_ROOT), ref)
        shas[version] = sha
        target = ARCHIVE_ROOT / version
        marker = target / ".archive_sha"
        if (target / "ai_workspace").exists() and marker.exists() and marker.read_text().strip() == sha:
            continue
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            f"git -C {WORKTREE_ROOT} archive {sha} | tar -x -C {target}",
            shell=True, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git archive 실패 ({ref}): {proc.stderr}")
        marker.write_text(sha)
    return shas


def run_pipeline(*, out_tag: str, answer_start: Optional[str] = None, **kw) -> dict:
    out_path = RESULTS_DIR / f"{out_tag}.json"
    if out_path.exists():
        return json.loads(out_path.read_text(encoding="utf-8"))

    with _BUDGET_LOCK:
        if _BUDGET["max_new"] is not None and _BUDGET["started"] >= _BUDGET["max_new"]:
            raise RunBudgetExhausted(out_tag)
        _BUDGET["started"] += 1

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
        "--top-k", str(kw.get("top_k", TOP_K)),
        "--tie-draws", str(kw.get("tie_draws", 0)),
        "--fixed-rounds", str(kw.get("fixed_rounds", 0)),
        "--es-valid-order", kw.get("es_valid_order", ES_VALID_ORDER_DEFAULT),
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


def cache_tag(seed: int, harness_sha: str, code_sha: str, cfg_hash: str, key_extras: Optional[dict], **kw) -> str:
    """캐시 태그 = 사람이 읽을 수 있는 설정 필드 + (작업 트리 diff 해시, 데이터 해시,
    answer_start, top_k, 추첨 횟수, 라운드 정책, 조기 종료용 inner-valid 행 순서)의 요약 해시."""
    es_order = kw.get("es_valid_order", ES_VALID_ORDER_DEFAULT)
    extras = dict(key_extras or {})
    extras.update({
        "answer_start": kw.get("answer_start"),
        "top_k": kw.get("top_k", TOP_K),
        "tie_draws": kw.get("tie_draws", 0),
        "fixed_rounds": kw.get("fixed_rounds", 0),
        "es_valid_order": es_order,
    })
    digest = hashlib.sha256(json.dumps(extras, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    return "__".join([
        kw["version"], kw["protocol"], kw["label_mode"], kw["leakage_mode"],
        kw["candidate_pool"], kw["negative_source"], kw["objective_override"],
        f"fr{kw.get('fixed_rounds', 0)}", f"es{es_order}", f"seed{seed}", f"h{harness_sha[:10]}", f"c{code_sha[:10]}",
        f"cfg{cfg_hash}", f"k{digest}", "v2_2",
    ])


def run_many_seeds(
    seeds: List[int], code_shas: Dict[str, str], harness_sha: str, config_hashes: Dict,
    answer_start: Optional[str], key_extras: Optional[dict] = None,
    provenance_shas: Optional[Dict[str, str]] = None, **kw,
) -> List[dict]:
    """seed별 실행은 서로 독립이다(각자 고유한 캐시 파일). 순서는 항상 `seeds` 순서.

    harness_sha / code_shas: 캐시 키에 들어가는 식별자. main()은 여기에 git SHA가 아니라
    **내용 해시**(harness_input_hash, current 엔진의 engine_content_hash, 아카이브 버전은
    ref SHA)를 넘긴다. provenance_shas: 실행 결과 JSON에 기록할 실제 git SHA(없으면 키와 같음)."""
    version = kw["version"]
    code_sha = code_shas[version]
    cfg_hash = config_hashes.get((version, kw["objective_override"]), "unk")
    prov = provenance_shas or {}

    def _one(seed: int) -> dict:
        tag = cache_tag(seed, harness_sha, code_sha, cfg_hash, key_extras, answer_start=answer_start, **kw)
        return run_pipeline(
            out_tag=tag, answer_start=answer_start, seed=seed, engine_root=ENGINE_ROOTS[version],
            code_sha=prov.get(version, code_sha), harness_sha=prov.get("harness", harness_sha), **kw,
        )

    if MAX_WORKERS <= 1 or len(seeds) <= 1:
        return [_one(s) for s in seeds]

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(seeds))) as pool:
        futures = {pool.submit(_one, s): i for i, s in enumerate(seeds)}
        results_by_idx: Dict[int, dict] = {}
        for fut in as_completed(futures):
            results_by_idx[futures[fut]] = fut.result()
    return [results_by_idx[i] for i in range(len(seeds))]


# ------------------------------------------------------------------ 요약/부트스트랩 유틸


def _field(run: dict, path: str) -> Optional[dict]:
    """"primary", "primary.tie_random", "as_written.tie_random", "primary.unshown_filtered" 등."""
    cur = run
    for part in path.split("."):
        if cur is None:
            return None
        cur = cur.get(part)
    return cur


def _agg_of(block: Optional[dict]) -> dict:
    if not block:
        return {}
    return block.get("aggregate_metrics") or block.get("aggregate_mean") or {}


def summarize_field(results: List[dict], field: str) -> dict:
    blocks = [_field(r, field) for r in results]
    if not results or any(b is None for b in blocks):
        return {}
    aggs = [_agg_of(b) for b in blocks]
    keys = [k for k in aggs[0] if k != "num_users"]
    out = {}
    for k in keys:
        vals = [a[k] for a in aggs if k in a]
        out[k] = {"mean": float(np.mean(vals)), "std": float(np.std(vals)), "values": vals}
    out["n_seeds"] = len(results)
    out["seeds"] = [r.get("seed") for r in results]
    out["num_users"] = aggs[0].get("num_users")
    out["elapsed_sec_total"] = round(sum(r.get("elapsed_sec", 0.0) for r in results), 1)
    out["best_iteration"] = [r.get("best_iteration") for r in results]
    out["n_trees"] = [r.get("n_trees") for r in results]
    out["rounds_policy"] = results[0].get("rounds_policy")
    out["es_valid_order"] = results[0].get("es_valid_order")
    out["degenerate_single_tree"] = [bool(r.get("degenerate_single_tree")) for r in results]
    out["n_degenerate_seeds"] = int(sum(bool(r.get("degenerate_single_tree")) for r in results))
    out["n_low_iteration_seeds"] = int(sum(
        1 for r in results if r.get("best_iteration") is not None and r["best_iteration"] <= LOW_BEST_ITERATION_MAX
    ))
    out["n_distinct_scores_primary"] = [r.get("n_distinct_scores_primary") for r in results]
    out["top_tie_size_eval_users_median"] = [r.get("top_tie_size_eval_users_median") for r in results]
    if blocks and isinstance(blocks[0], dict) and "aggregate_min" in blocks[0]:
        out["tie_draw_range"] = {
            k: [float(min(b["aggregate_min"][k] for b in blocks)), float(max(b["aggregate_max"][k] for b in blocks))]
            for k in keys if k in blocks[0]["aggregate_min"]
        }
    return out


def per_user_by_seed(results: List[dict], field: str) -> List[Dict[int, Dict[str, float]]]:
    out = []
    for r in results:
        block = _field(r, field)
        out.append({int(uid): m for uid, m in (block or {}).get("per_user_metrics", {}).items()})
    return out


def diff_per_user_by_seed(a: List[Dict[int, dict]], b: List[Dict[int, dict]]) -> List[Dict[int, dict]]:
    """같은 시드 인덱스의 두 per-user 지표 차이(b - a) - 누출 효과 자체를 버전 간 비교(DiD)."""
    out = []
    for pa, pb in zip(a, b):
        common = set(pa) & set(pb)
        out.append({u: {k: pb[u][k] - pa[u][k] for k in pa[u] if k in pb[u]} for u in common})
    return out


def boot_pair(a_runs, a_field, b_runs, b_field, paired_seeds=False, metrics=BOOT_METRICS) -> dict:
    pa = per_user_by_seed(a_runs, a_field)
    pb = per_user_by_seed(b_runs, b_field)
    return {
        mk: M.nested_bootstrap_paired_diff(pa, pb, mk, n_boot=1000, seed=0, paired_seeds=paired_seeds)
        for mk in metrics
    }


def boot_model_vs_baseline(model_runs, model_field, baseline_per_user: Dict[int, dict], metrics=("mrr", "precision@5")) -> dict:
    """모델(시드 x 유저) - 베이스라인(결정적 1개 '시드'): 유저는 쌍으로, 모델 시드는
    재표본. 결과 effect는 model - baseline (b - a 규약에 맞춰 a=베이스라인)."""
    pm = per_user_by_seed(model_runs, model_field)
    pbase = [{int(u): m for u, m in baseline_per_user.items()}]
    return {mk: M.nested_bootstrap_paired_diff(pbase, pm, mk, n_boot=1000, seed=0) for mk in metrics}


# ------------------------------------------------------------------ 베이스라인


def _avg_per_user(per_user_list: List[Dict[int, Dict[str, float]]]) -> Dict[int, Dict[str, float]]:
    if len(per_user_list) == 1:
        return per_user_list[0]
    users = set.intersection(*[set(p) for p in per_user_list]) if per_user_list else set()
    out = {}
    for u in users:
        keys = per_user_list[0][u].keys()
        out[u] = {k: float(np.mean([p[u][k] for p in per_user_list])) for k in keys}
    return out


def evaluate_baseline_variants(evaluator, recs_list, ground_truth, category_map, seen=None, shown=None, groups=None) -> dict:
    """recs_list: 결정적 베이스라인은 [recs] 하나, random은 추첨 여러 개(유저별 평균 =
    기대값 근사). plain/seen_filtered/unshown_filtered 변형을 같은 방식으로 평균낸다."""
    def _pu(transform):
        return _avg_per_user([
            M.per_user_metrics(evaluator, transform(r), ground_truth, category_map) for r in recs_list
        ])

    per_user = _pu(lambda r: r)
    entry = {"aggregate": M.aggregate(per_user), "per_user": per_user, "n_draws": len(recs_list)}
    if groups:
        for gname, gids in groups.items():
            entry[gname] = PR.split_metrics_by_group(per_user, gids)
    if seen is not None:
        pu_seen = _pu(lambda r: PR.filter_seen(r, seen))
        entry["seen_filtered"] = {"aggregate": M.aggregate(pu_seen), "per_user": pu_seen}
        entry["seen_share_top5"] = float(np.mean([PR.seen_share_in_topk(r, seen, k=5) for r in recs_list]))
    if shown is not None:
        pu_shown = _pu(lambda r: PR.filter_seen(r, shown))
        entry["unshown_filtered"] = {"aggregate": M.aggregate(pu_shown), "per_user": pu_shown}
        # top-K 리스트에서 사후 제거하므로 5건 미만으로 줄 수 있다(P@5는 그래도 5로 나눈다).
        eval_users = [u for u in ground_truth if ground_truth[u]]
        lens = [len(PR.filter_seen(r, shown).get(u, [])) for r in recs_list for u in eval_users if u in r]
        entry["unshown_filtered"]["list_len_stats"] = {
            "mean_len": float(np.mean(lens)) if lens else None,
            "share_len_lt_5": float(np.mean([n < 5 for n in lens])) if lens else None,
        }
    return entry


def compute_baselines(
    bundle, protocol: str, answer_start=None, candidate_ids: Optional[Sequence[int]] = None,
    random_draws: int = RANDOM_DRAWS,
) -> dict:
    category_map = FL.category_ids_by_newsletter(bundle)
    if candidate_ids is None:
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
        seen = shown = groups = None
    else:
        cutoff = answer_start
        train_logs = logs[logs["timestamp"] < cutoff]
        valid_logs = logs[logs["timestamp"] >= cutoff]
        ground_truth = valid_logs.groupby("user_id")["news_letter_id"].apply(set).to_dict()
        clicks_before = logs[logs["timestamp"] < cutoff]
        cold_ids, warm_ids = PR.cold_warm_split(user_ids, clicks_before, cutoff)
        groups = {"cold": cold_ids, "warm": warm_ids}
        seen = PR.seen_items_by_user(clicks_before, cutoff)
        shown = PR.shown_items_by_user(bundle.ctr_logs, cutoff)
    ground_truth = PR.restrict_ground_truth_to_pool(ground_truth, candidate_ids)

    evaluator = Evaluator(k_values=[5, 10, 20])
    det = BL.compute_all_baselines(bundle, category_map, candidate_ids, user_ids, train_logs, cutoff, top_k=TOP_K, seed=0)
    out = {}
    for name, recs in det.items():
        if name == "random":
            recs_list = [BL.random_baseline(user_ids, candidate_ids, TOP_K, seed=s) for s in range(random_draws)]
        else:
            recs_list = [recs]
        out[name] = evaluate_baseline_variants(evaluator, recs_list, ground_truth, category_map, seen, shown, groups)
    answer_density = [len(v) / len(candidate_ids) for v in ground_truth.values()]
    meta = {
        "n_candidates": len(candidate_ids),
        "n_evaluated_users": len(ground_truth),
        "mean_answer_density": float(np.mean(answer_density)) if answer_density else None,
        "random_draws": random_draws,
        "cold_fallback": "popularity(cutoff 이전 클릭 수) - cosine_history/onboarding에서 히스토리/선택이 없는 유저",
    }
    return {"meta": meta, "baselines": out}


def _load_harness_pipeline():
    """하네스 pipeline.py를 고유 이름으로 로드한다 - 최상위 이름 `pipeline`은 ai_workspace의
    pipeline 패키지와 충돌할 수 있다(테스트 수집 순서 의존 실패의 원인이었다)."""
    import importlib.util

    name = "team_repro_pipeline"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TEAM_REPRO_DIR / "pipeline.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def team_final_written_random_reference(bundle, random_draws: int = RANDOM_DRAWS) -> dict:
    """team-final-as-written과 같은 정답 정의 위의 무작위 추천 기대값 - 그 행이 무작위보다
    얼마나 높은지 보려면 이 기준값이 같이 있어야 한다(v2 리뷰 BLOCKER 수정)."""
    PIPE = _load_harness_pipeline()  # 순수 함수만 쓴다(엔진 import 없음)

    category_map = FL.category_ids_by_newsletter(bundle)
    candidate_ids = bundle.newsletters["news_letter_id"].tolist()
    user_ids = bundle.users["user_id"].tolist()
    evaluator = Evaluator(k_values=[5, 10, 20])
    recs_list = [BL.random_baseline(user_ids, candidate_ids, TOP_K, seed=s) for s in range(random_draws)]
    out = {}
    for key, clicks_only in [("clicks_only", True), ("all_rows_definition_error", False)]:
        gt = PIPE.team_final_written_ground_truth(bundle.ctr_logs, clicks_only=clicks_only)
        entry = evaluate_baseline_variants(evaluator, recs_list, gt, category_map)
        entry.pop("per_user", None)
        entry["mean_answers_per_user"] = float(np.mean([len(v) for v in gt.values()]))
        entry["n_users"] = len(gt)
        out[key] = entry
    return out


# ------------------------------------------------------------------ 조기 종료 동점 아티팩트 요약


def _diag_cell(run: dict, frame: str, iteration, metric: str = "ndcg@5") -> dict:
    diag = run.get("es_tie_diagnostic") or {}
    return ((((diag.get("frames") or {}).get(frame) or {}).get("by_iteration") or {}).get(str(iteration)) or {}).get(metric) or {}


def tie_diag_rows(results: List[dict]) -> List[dict]:
    """실행별 es_tie_diagnostic을 리포트용 한 줄로 편다. NDCG로 조기 종료한 실행만 값이 있다."""
    rows = []
    for r in results:
        diag = r.get("es_tie_diagnostic")
        bi = r.get("best_iteration")
        row = {
            "seed": r.get("seed"), "es_valid_order": r.get("es_valid_order"), "best_iteration": bi,
            "recorded_ndcg5_at_best": ((r.get("best_score_inner_valid") or {}).get("valid_0") or {}).get("ndcg@5"),
            "has_diagnostic": bool(diag),
        }
        if diag:
            frames = diag.get("frames") or {}
            it1_engine = _diag_cell(r, "engine_row_order", 1)
            best_engine = _diag_cell(r, "engine_row_order", bi)
            row.update({
                "positive_first_share_engine_order": (frames.get("engine_row_order") or {}).get("positive_first_share"),
                "positive_first_share_as_evaluated": (frames.get("as_evaluated") or {}).get("positive_first_share"),
                "n_groups": it1_engine.get("n_groups"),
                "iter1_engine_order_ndcg5": it1_engine.get("data_order"),
                "iter1_reversed_order_ndcg5": it1_engine.get("reversed_order"),
                "iter1_tie_expected_ndcg5": it1_engine.get("tie_expected"),
                "iter1_tied_positive_group_share": it1_engine.get("tied_positive_group_share"),
                "best_engine_order_ndcg5": best_engine.get("data_order"),
                "best_tie_expected_ndcg5": best_engine.get("tie_expected"),
                "best_tied_positive_group_share": best_engine.get("tied_positive_group_share"),
            })
        rows.append(row)
    return rows


def early_stopping_tie_artefact(runs: Dict[str, List[dict]], gen_runs: Dict[str, List[dict]]) -> dict:
    """조기 종료 지표의 동점 순서 아티팩트를 한곳에 모은다.

    paired: 같은 시드의 '엔진 순서 조기 종료'(current_es_engine_order)와 '섞은 조기 종료'(current)를
    나란히 놓는다. 두 실행은 학습 데이터·시드가 같아 같은 트리 열을 만들고, 몇 번째 트리에서
    멈추는지만 다르다 - 그래서 섞은 쪽 실행에서 '엔진 순서로 계산한 NDCG'를 1라운드와 선택된
    라운드 두 지점에서 비교할 수 있다. 1라운드 값이 더 크면 LightGBM은 1라운드를 고를 수밖에 없다."""
    eng = {r["seed"]: r for r in runs.get("current_es_engine_order", [])}
    paired = []
    for r in runs.get("current", []):
        e = eng.get(r["seed"])
        if e is None:
            continue
        shuf_row = tie_diag_rows([r])[0]
        eng_row = tie_diag_rows([e])[0]
        it1_engine = shuf_row.get("iter1_engine_order_ndcg5")
        best_engine = shuf_row.get("best_engine_order_ndcg5")
        paired.append({
            "seed": r["seed"],
            "engine_order_best_iteration": e.get("best_iteration"),
            "shuffled_best_iteration": r.get("best_iteration"),
            "engine_order_recorded_ndcg5": eng_row.get("recorded_ndcg5_at_best"),
            "shuffled_recorded_ndcg5": shuf_row.get("recorded_ndcg5_at_best"),
            "iter1_engine_order_ndcg5": it1_engine,
            "iter1_tie_expected_ndcg5": shuf_row.get("iter1_tie_expected_ndcg5"),
            "iter1_reversed_order_ndcg5": shuf_row.get("iter1_reversed_order_ndcg5"),
            "iter1_tied_positive_group_share": shuf_row.get("iter1_tied_positive_group_share"),
            "shuffled_best_engine_order_ndcg5": best_engine,
            "shuffled_best_tie_expected_ndcg5": shuf_row.get("best_tie_expected_ndcg5"),
            "iter1_inflation": (it1_engine - shuf_row["iter1_tie_expected_ndcg5"])
            if it1_engine is not None and shuf_row.get("iter1_tie_expected_ndcg5") is not None else None,
            "engine_order_iter1_beats_tie_fair_best": (it1_engine > best_engine)
            if it1_engine is not None and best_engine is not None else None,
            "tie_expected_improves_after_iter1": (shuf_row["best_tie_expected_ndcg5"] > shuf_row["iter1_tie_expected_ndcg5"])
            if shuf_row.get("best_tie_expected_ndcg5") is not None and shuf_row.get("iter1_tie_expected_ndcg5") is not None else None,
            "positive_first_share_engine_order": shuf_row.get("positive_first_share_engine_order"),
            "positive_first_share_shuffled": shuf_row.get("positive_first_share_as_evaluated"),
            "primary_mrr_engine_order": ((e.get("primary") or {}).get("aggregate_metrics") or {}).get("mrr"),
            "primary_mrr_shuffled": ((r.get("primary") or {}).get("aggregate_metrics") or {}).get("mrr"),
        })

    def _count(rows, key, pred):
        return int(sum(1 for x in rows if x.get(key) is not None and pred(x[key])))

    infl = [x["iter1_inflation"] for x in paired if x.get("iter1_inflation") is not None]
    summary = {
        "n_seeds": len(paired),
        "n_engine_order_stops_at_1": _count(paired, "engine_order_best_iteration", lambda b: b <= 1),
        "n_shuffled_stops_at_1": _count(paired, "shuffled_best_iteration", lambda b: b <= 1),
        "n_engine_order_low_iteration": _count(paired, "engine_order_best_iteration", lambda b: b <= LOW_BEST_ITERATION_MAX),
        "n_shuffled_low_iteration": _count(paired, "shuffled_best_iteration", lambda b: b <= LOW_BEST_ITERATION_MAX),
        "iter1_inflation_mean": float(np.mean(infl)) if infl else None,
        "iter1_inflation_min": float(np.min(infl)) if infl else None,
        "iter1_inflation_max": float(np.max(infl)) if infl else None,
        "n_engine_order_iter1_beats_tie_fair_best": _count(paired, "engine_order_iter1_beats_tie_fair_best", bool),
        "n_tie_expected_improves_after_iter1": _count(paired, "tie_expected_improves_after_iter1", bool),
        # 엔진 순서에서 1라운드 종료한 시드가 '엔진 순서 1라운드 NDCG > 엔진 순서 선택 라운드 NDCG'인 시드와 같은가
        "collapse_seeds_engine_order": [x["seed"] for x in paired if (x.get("engine_order_best_iteration") or 0) <= 1],
        "iter1_beats_best_seeds": [x["seed"] for x in paired if x.get("engine_order_iter1_beats_tie_fair_best")],
    }

    by_arm = {}
    arm_sources = {**{k: v for k, v in runs.items()}, **{f"generator_split {k}": v for k, v in gen_runs.items()}}
    for name, rs in arm_sources.items():
        rows = tie_diag_rows(rs)
        if not any(x["has_diagnostic"] for x in rows):
            continue
        by_arm[name] = {
            "es_valid_order": rows[0].get("es_valid_order"),
            "rank_group_key": (rs[0].get("es_tie_diagnostic") or {}).get("rank_group_key"),
            "n_inner_valid_groups": [x.get("n_groups") for x in rows],
            "best_iteration": [x.get("best_iteration") for x in rows],
            "n_stops_at_1": _count(rows, "best_iteration", lambda b: b <= 1),
            "n_low_iteration": _count(rows, "best_iteration", lambda b: b <= LOW_BEST_ITERATION_MAX),
            "positive_first_share_engine_order": [x.get("positive_first_share_engine_order") for x in rows],
            "positive_first_share_as_evaluated": [x.get("positive_first_share_as_evaluated") for x in rows],
            "iter1_engine_order_ndcg5": [x.get("iter1_engine_order_ndcg5") for x in rows],
            "iter1_tie_expected_ndcg5": [x.get("iter1_tie_expected_ndcg5") for x in rows],
            "best_tie_expected_ndcg5": [x.get("best_tie_expected_ndcg5") for x in rows],
            "recorded_ndcg5_at_best": [x.get("recorded_ndcg5_at_best") for x in rows],
        }
    return {
        "note": (
            "LightGBM NDCG는 동점을 행 순서로 깬다. engine_order = 엔진이 만든 inner-valid 순서(positive가 그룹 첫 행), "
            "tie_expected = 동점 블록 안 순서가 무작위일 때의 기대값(행 순서와 무관). ndcg5는 inner-valid NDCG@5."
        ),
        "paired_current": paired,
        "summary": summary,
        "by_arm": by_arm,
    }


# ------------------------------------------------------------------ 버전 비교 (같은 조건끼리)

# (키, team-final 쪽 arm, current 쪽 arm, 조건, 두 arm 사이에 남는 차이, 같은 조건 비교인가)
VERSION_PAIRS = [
    (
        "as_configured", "team-final", "current",
        "각 버전의 config 그대로: team-final은 binary·AUC 조기 종료, current는 lambdarank·NDCG 조기 종료(섞은 inner-valid)",
        "코드 + objective + 조기 종료 지표", False,
    ),
    (
        "same_objective_binary_es", "team-final", "binary",
        "둘 다 binary objective·AUC 조기 종료(current에 objective만 덮어씀)",
        "코드", True,
    ),
    (
        "fixed100_rounds", "team_final_fixed100", "fixed100",
        "둘 다 조기 종료 없이 100라운드(objective는 각 버전 config 그대로)",
        "코드 + objective", True,
    ),
    (
        "v2_1_engine_order_es", "team-final", "current_es_engine_order",
        "v2.1이 한 비교의 재현: current를 엔진 행 순서 그대로 조기 종료(동점 아티팩트로 1라운드 종료 시드 포함)",
        "코드 + objective + 조기 종료 지표 + 동점 아티팩트", False,
    ),
]


def version_comparison_pairs(runs: Dict[str, List[dict]]) -> dict:
    """team-final - current를 여러 조건으로 비교한다. effect = team-final 쪽 - current 쪽.
    1차/2차 격차는 서로 다른 모델이라 시드 독립 재표본, 누출 효과(2차-1차)는 같은 모델이라
    arm 안에서 시드 쌍, 누출 효과의 차이(DiD)는 arm 사이 시드 독립이다."""
    out = {"order": [p[0] for p in VERSION_PAIRS], "pairs": {}}
    for key, tf_arm, cur_arm, condition, differs, like_with_like in VERSION_PAIRS:
        tf, cur = runs[tf_arm], runs[cur_arm]
        leak_cur = diff_per_user_by_seed(per_user_by_seed(cur, "primary"), per_user_by_seed(cur, "as_written"))
        leak_tf = diff_per_user_by_seed(per_user_by_seed(tf, "primary"), per_user_by_seed(tf, "as_written"))
        out["pairs"][key] = {
            "team_final_arm": tf_arm, "current_arm": cur_arm,
            "condition": condition, "remaining_differences": differs, "like_with_like": like_with_like,
            "team_final_n_trees": [r.get("n_trees") for r in tf],
            "current_n_trees": [r.get("n_trees") for r in cur],
            "team_final_objective": tf[0].get("objective_used"), "current_objective": cur[0].get("objective_used"),
            "team_final_rounds_policy": tf[0].get("rounds_policy"), "current_rounds_policy": cur[0].get("rounds_policy"),
            "primary": boot_pair(cur, "primary", tf, "primary", metrics=["mrr", "precision@5"]),
            "as_written": boot_pair(cur, "as_written", tf, "as_written", metrics=["mrr", "precision@5"]),
            "leak_effect_team_final": boot_pair(tf, "primary", tf, "as_written", paired_seeds=True, metrics=["mrr", "precision@5"]),
            "leak_effect_current": boot_pair(cur, "primary", cur, "as_written", paired_seeds=True, metrics=["mrr", "precision@5"]),
            "leak_effect_did": {
                mk: M.nested_bootstrap_paired_diff(leak_cur, leak_tf, mk, n_boot=1000, seed=0) for mk in ["mrr", "precision@5"]
            },
        }
    return out


# ------------------------------------------------------------------ v2.1 재현 대조

V21_RAW_COMMIT = "67a5db2"  # v2.1 리포트 원자료를 생성·커밋한 커밋


def reproduction_check_vs_v21(runs: Dict[str, List[dict]], gen_runs: Dict[str, List[dict]]) -> dict:
    """v2.2 실행과 v2.1 원자료(git에 커밋된 team_repro_v2_raw.json)를 같은 시드끼리 대조한다.

    v2.2에서 바뀐 것은 조기 종료용 inner-valid 행 순서뿐이다. 그래서
      - AUC로 조기 종료하는 arm(team-final, binary, leaky_binary)과 조기 종료가 없는 arm
        (fixed100, leaky_fixed100)은 v2.1과 소수 4자리까지 같아야 하고,
      - 엔진 순서 그대로 조기 종료하는 진단 arm(current_es_engine_order)은 v2.1의 current와 같아야 하며,
      - NDCG로 조기 종료하는 나머지 arm은 달라질 수 있다(선택되는 라운드가 바뀌면 달라지고,
        안 바뀌면 같다 - 어느 쪽이든 기대에 어긋나지 않는다)."""
    try:
        raw = json.loads(subprocess.run(
            ["git", "-C", str(WORKTREE_ROOT), "show", f"{V21_RAW_COMMIT}:reports/recsys/team_repro_v2_raw.json"],
            capture_output=True, text=True, check=True,
        ).stdout)
    except Exception as e:  # noqa: BLE001 - 대조는 부가 정보라 실패해도 본 실행은 계속
        return {"error": str(e)}
    old = raw["runs"]
    old_gen = raw["generator_split_runs"]
    # (이름, v2.1 실행, v2.2 실행, 비교 필드, 같아야 하는가)
    pairs = [
        ("team-final (AUC 조기 종료)", old["team-final"], runs["team-final"], ["primary", "as_written"], True),
        ("binary (AUC 조기 종료)", old["binary"], runs["binary"], ["primary", "as_written"], True),
        ("leaky_binary (AUC 조기 종료)", old["leaky_binary"], runs["leaky_binary"], ["primary", "as_written"], True),
        ("fixed100 (조기 종료 없음)", old["fixed100"], runs["fixed100"], ["primary", "as_written"], True),
        ("leaky_fixed100 (조기 종료 없음)", old["leaky_fixed100"], runs["leaky_fixed100"], ["primary", "as_written"], True),
        ("current_es_engine_order vs v2.1 current (엔진 순서 조기 종료)", old["current"], runs["current_es_engine_order"], ["primary", "as_written"], True),
        ("current (NDCG 조기 종료, 섞음)", old["current"], runs["current"], ["primary", "as_written"], False),
        ("leaky (NDCG 조기 종료, 섞음)", old["leaky"], runs["leaky"], ["primary"], False),
        ("small_recent_15 (NDCG 조기 종료, 섞음)", old["small_recent_15"], runs["small_recent_15"], ["primary"], False),
        ("impression (NDCG 조기 종료, 섞음)", old["impression"], runs["impression"], ["primary"], False),
        ("fix-snapshot (NDCG 조기 종료, 섞음)", old["fix-snapshot"], runs["fix-snapshot"], ["primary", "as_written"], False),
        ("generator_split current (NDCG 조기 종료, 섞음)", old_gen["current"], gen_runs["current"], ["primary"], False),
        ("generator_split team-final (AUC 조기 종료)", old_gen["team-final"], gen_runs["team-final"], ["primary"], True),
    ]
    out = {"v21_raw_commit": V21_RAW_COMMIT, "arms": {}}
    for name, old_runs, new_runs, fields, expect_same in pairs:
        new_by_seed = {r["seed"]: r for r in new_runs}
        diffs, seeds, bi_old, bi_new = [], [], [], []
        for o in old_runs:
            new = new_by_seed.get(o["seed"])
            if new is None:
                continue
            seeds.append(o["seed"])
            bi_old.append(o.get("best_iteration"))
            bi_new.append(new.get("best_iteration"))
            for f in fields:
                for mk in ("mrr", "precision@5"):
                    diffs.append(abs(new[f]["aggregate_metrics"][mk] - o[f]["aggregate_metrics"][mk]))
        identical = bool(diffs) and max(diffs) < 5e-5
        out["arms"][name] = {
            "shared_seeds": seeds,
            "max_abs_diff_mrr_p5": float(max(diffs)) if diffs else None,
            "identical_to_4dp": identical,
            "expected_identical": expect_same,
            "violates_expectation": bool(diffs) and expect_same and not identical,
            "best_iteration_v21": bi_old,
            "best_iteration_v22": bi_new,
        }
    return out


# ------------------------------------------------------------------ 데이터 진단


def data_structure_diagnostics(bundle, answer_start_ts, answer_start: str) -> dict:
    logs = bundle.ctr_logs
    clicks = logs[logs["is_clicked"] == 1]
    clicks_before = clicks[clicks["timestamp"] < answer_start_ts]
    all_user_ids = bundle.users["user_id"].tolist()
    cold_ids, warm_ids = PR.cold_warm_split(all_user_ids, clicks_before, answer_start_ts)
    answer_logs = clicks[clicks["timestamp"] >= answer_start_ts]
    answers = answer_logs.groupby("user_id")["news_letter_id"].apply(set).to_dict()
    n_candidates_full = len(bundle.newsletters)
    shown_before = PR.shown_items_by_user(logs, answer_start_ts)
    n_answers_shown_before = sum(len(a & shown_before.get(u, set())) for u, a in answers.items())
    density_unshown = [
        len(a) / max(1, n_candidates_full - len(shown_before.get(u, set()))) for u, a in answers.items()
    ]
    eval_cold = [u for u in answers if u in cold_ids]
    exposures_per_persona = logs.groupby("user_id").size()
    session_span = logs.groupby("user_id")["timestamp"].agg(lambda s: (s.max() - s.min()).total_seconds())

    # generator split 구조: valid 아이템이 정확히 '가장 최근 생성' 아이템인가
    nl = bundle.newsletters
    valid_ids = set(bundle.generator_valid_keys["news_letter_id"].unique().tolist())
    train_ids = set(bundle.generator_train_keys["news_letter_id"].unique().tolist())
    newest = set(nl.sort_values("created_at", ascending=False)["news_letter_id"].head(len(valid_ids)).tolist())
    gen = {
        "n_valid_items": len(valid_ids),
        "n_train_items": len(train_ids),
        "valid_id_range": [int(min(valid_ids)), int(max(valid_ids))] if valid_ids else None,
        "train_id_range": [int(min(train_ids)), int(max(train_ids))] if train_ids else None,
        "valid_items_are_exactly_newest": bool(valid_ids == newest),
        "train_items_max_created_at": str(nl[nl["news_letter_id"].isin(train_ids)]["created_at"].max()),
        "valid_items_min_created_at": str(nl[nl["news_letter_id"].isin(valid_ids)]["created_at"].min()),
    }

    return {
        "n_users_total": len(bundle.users),
        "n_newsletters": n_candidates_full,
        "n_ctr_logs": len(logs),
        "n_clicks": int(len(clicks)),
        "click_rate": float((logs["is_clicked"] == 1).mean()),
        "dataset_window_start": bundle.dataset_start_time.isoformat(),
        "dataset_window_end": bundle.dataset_end_time.isoformat(),
        "dataset_window_seconds": float((bundle.dataset_end_time - bundle.dataset_start_time).total_seconds()),
        "exposures_per_persona_min": int(exposures_per_persona.min()),
        "exposures_per_persona_max": int(exposures_per_persona.max()),
        "session_span_seconds_mean": float(session_span.mean()),
        "answer_start": answer_start,
        "n_cold_users": len(cold_ids),
        "n_warm_users": len(warm_ids),
        "n_evaluated_users_in_answer_window": len(answers),
        "n_evaluated_cold_users": len(eval_cold),
        "n_evaluated_warm_users": len(answers) - len(eval_cold),
        "n_answer_clicks": int(len(answer_logs)),
        "random_p5_base_rate_mean_answer_density": float(np.mean([len(a) / n_candidates_full for a in answers.values()])) if answers else float("nan"),
        "n_answers_shown_before_answer_start": int(n_answers_shown_before),
        "answer_density_among_unshown_mean": float(np.mean(density_unshown)) if density_unshown else float("nan"),
        "generator_split": gen,
    }


# ------------------------------------------------------------------ main


SEEDS_MAIN = [42, 43, 44, 45, 46]
# 분해/민감도 요약에 넣는 arm (runs의 키). 헤드라인 3버전은 따로 요약한다.
DECOMP_ARMS = [
    "leaky", "binary", "leaky_binary", "fixed100", "leaky_fixed100", "impression", "small_recent_15",
    "current_es_engine_order", "team_final_fixed100",
]
# 추론 시점 누출 효과(2차 - 1차)를 설정별로 보는 arm
LEAK_EFFECT_ARMS = [
    "team-final", "team_final_fixed100", "fix-snapshot", "current", "binary", "fixed100", "current_es_engine_order",
]
SEEDS_FIXSNAP = [42, 43, 44]  # fix-snapshot은 point-in-time cutoff 메모이즈가 없어 실행당 약 2분


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-new-runs", type=int, default=None, help="이번 호출에서 새로 돌릴 pipeline 실행 수 상한(이어서 다시 실행하면 캐시에서 계속)")
    parser.add_argument("--allow-dirty", action="store_true", help="감시 경로에 커밋 안 된 변경이 있어도 최종 JSON을 쓴다(기본: 거부)")
    args = parser.parse_args(argv)
    _BUDGET["max_new"] = args.max_new_runs
    try:
        return _main(args)
    except RunBudgetExhausted as e:
        print(f"\n[부분 실행] 새 실행 {_BUDGET['started']}건 후 --max-new-runs 한도에 도달했다(다음: {e}). "
              "완료된 실행은 캐시에 있으니 같은 명령을 다시 실행하면 이어간다.")
        return 3


def _main(args) -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    archive_shas = ensure_code_archives()

    bundle = FL.load_archive_bundle()
    clicks = bundle.ctr_logs[bundle.ctr_logs["is_clicked"] == 1]
    answer_start_ts = PR.compute_fixed_answer_start(clicks, val_ratio=0.2)
    answer_start = answer_start_ts.isoformat()
    print(f"고정 answer_start (team_split): {answer_start}")

    harness_sha = git_sha(str(WORKTREE_ROOT), "HEAD")
    tree_state = working_tree_state(str(WORKTREE_ROOT), DIRTY_WATCH_PATHS)
    if tree_state["dirty"]:
        print(f"[경고] 감시 경로에 커밋 안 된 변경이 있다(diff 해시 {tree_state['diff_hash']}) - 캐시 키(내용 해시)에는 "
              "반영되지만 최종 JSON은 --allow-dirty 없이는 쓰지 않는다.")
    code_shas = {"current": harness_sha, **archive_shas}
    input_hashes = {
        "harness_pipeline_inputs": harness_input_hash(),
        "current_engine": engine_content_hash(ENGINE_ROOTS["current"]),
    }
    key_code_shas = {"current": input_hashes["current_engine"], **archive_shas}
    data_sha256 = {p.name: sha256_of(p) for p in sorted(FL.SYNTH_DIR.glob("*.csv"))}
    data_sha256["newsletters_bge_m3.npy"] = sha256_of(FL.EMB_DIR / "newsletters_bge_m3.npy")
    data_sha256["newsletter_categories.csv"] = sha256_of(FL.CATEGORIES_CSV)
    key_extras = {"data": sorted(v[:16] for v in data_sha256.values())}
    print(f"HEAD {harness_sha[:10]} / 하네스 입력 해시 {input_hashes['harness_pipeline_inputs'][:10]} / current 엔진 "
          f"{input_hashes['current_engine'][:10]} / team-final {code_shas['team-final'][:10]} / fix-snapshot {code_shas['fix-snapshot'][:10]}")

    config_hashes, config_overrides_by = {}, {}
    for version in ["current", "team-final", "fix-snapshot"]:
        for override in [None, "binary"]:
            _cfg, chash, overrides = CFG.load_version_config(ENGINE_ROOTS[version], seed=42, top_k=TOP_K, objective_override=override)
            config_hashes[(version, override or "none")] = chash
            config_overrides_by[(version, override or "none")] = overrides

    if os.environ.get("TEAM_REPRO_QUICK"):
        seeds_main, seeds_fixsnap = [42], [42]
    else:
        seeds_main, seeds_fixsnap = SEEDS_MAIN, SEEDS_FIXSNAP

    common = dict(
        protocol="team_split", label_mode="clicks_only", leakage_mode="fixed",
        candidate_pool="full_195", negative_source="random", objective_override="none",
        harness_sha=input_hashes["harness_pipeline_inputs"], code_shas=key_code_shas, config_hashes=config_hashes,
        answer_start=answer_start, key_extras=key_extras,
        provenance_shas={"harness": harness_sha, **code_shas},
        top_k=TOP_K, tie_draws=TIE_DRAWS, fixed_rounds=0, es_valid_order=ES_VALID_ORDER_DEFAULT,
    )

    def arm(seeds, **over):
        return run_many_seeds(seeds=seeds, **{**common, **over})

    print("=" * 70)
    print("1) 헤드라인: 3개 코드 버전 x team_split, point-in-time(1차) + as-written(2차)")
    print("=" * 70)
    runs = {
        "current": arm(seeds_main, version="current"),
        "team-final": arm(seeds_main, version="team-final"),
        "fix-snapshot": arm(seeds_fixsnap, version="fix-snapshot"),
    }

    print("=" * 70)
    print("2) 분해/민감도 arm (current 코드)")
    print("=" * 70)
    runs["leaky"] = arm(seeds_main, version="current", leakage_mode="leaky")
    runs["binary"] = arm(seeds_main, version="current", objective_override="binary")
    runs["leaky_binary"] = arm(seeds_main, version="current", objective_override="binary", leakage_mode="leaky")
    runs["fixed100"] = arm(seeds_main, version="current", fixed_rounds=FIXED_ROUNDS)
    runs["leaky_fixed100"] = arm(seeds_main, version="current", fixed_rounds=FIXED_ROUNDS, leakage_mode="leaky")
    runs["impression"] = arm(seeds_main, version="current", negative_source="impression")
    runs["small_recent_15"] = arm(seeds_main, version="current", candidate_pool="small_recent_15")

    print("=" * 70)
    print("2b) 조기 종료 아티팩트 진단 arm / 버전 비교용 고정 라운드 arm")
    print("=" * 70)
    # v2.1의 기준 arm과 같은 동작(엔진 행 순서 그대로 조기 종료) - 아티팩트를 수치로 보이는 용도로만 쓴다.
    runs["current_es_engine_order"] = arm(seeds_main, version="current", es_valid_order="engine")
    # team-final도 조기 종료 없이 같은 라운드 수로 학습 - 버전 비교를 같은 학습 길이끼리 하기 위해.
    runs["team_final_fixed100"] = arm(seeds_main, version="team-final", fixed_rounds=FIXED_ROUNDS)

    print("=" * 70)
    print("3) generator_split (학습을 ctr_logs_train.csv로 제한)")
    print("=" * 70)
    gen_runs = {
        v: arm(seeds_main, version=v, protocol="generator_split", answer_start=None, tie_draws=0)
        for v in ["current", "team-final"]
    }

    if tree_state["dirty"] and not args.allow_dirty:
        print("[중단] 모든 실행은 끝났지만 작업 트리가 dirty라 최종 JSON을 쓰지 않는다(커밋 후 다시 실행하면 캐시에서 즉시 집계).")
        return 4

    print("=" * 70)
    print("4) 베이스라인 (모델과 같은 answer_start/후보 풀/정답)")
    print("=" * 70)
    base_team = compute_baselines(bundle, "team_split", answer_start=answer_start_ts)
    base_gen = compute_baselines(bundle, "generator_split")
    small_pool = runs["small_recent_15"][0].get("n_candidates")
    cutoff_date = bundle.newsletters["created_at"].max().date()
    small_ids = bundle.newsletters[bundle.newsletters["created_at"].dt.date == cutoff_date]["news_letter_id"].tolist()
    assert len(small_ids) == small_pool, (len(small_ids), small_pool)
    base_small = compute_baselines(bundle, "team_split", answer_start=answer_start_ts, candidate_ids=small_ids)
    tfw_random = team_final_written_random_reference(bundle)

    print("=" * 70)
    print("5) 부트스트랩 (시드 x 유저, 1000회)")
    print("=" * 70)
    headline_versions = ["current", "team-final", "fix-snapshot"]
    headline = {}
    headline_ci = {}
    for v in headline_versions:
        rs = runs[v]
        headline[v] = {
            "primary_summary": summarize_field(rs, "primary"),
            "primary_tie_random_summary": summarize_field(rs, "primary.tie_random"),
            "as_written_summary": summarize_field(rs, "as_written"),
            "as_written_tie_random_summary": summarize_field(rs, "as_written.tie_random"),
            "primary_cold": _mean_of(rs, "primary.cold"),
            "primary_warm": _mean_of(rs, "primary.warm"),
            "primary_seen_filtered_summary": summarize_field(rs, "primary.seen_filtered"),
            "primary_unshown_filtered_summary": summarize_field(rs, "primary.unshown_filtered"),
            "primary_unshown_list_len": _unshown_len_summary(rs),
        }
        headline_ci[v] = {
            "primary": {mk: M.nested_bootstrap_ci(per_user_by_seed(rs, "primary"), mk, n_boot=1000, seed=0) for mk in BOOT_METRICS},
            "as_written": {mk: M.nested_bootstrap_ci(per_user_by_seed(rs, "as_written"), mk, n_boot=1000, seed=0) for mk in BOOT_METRICS},
            # 같은 ranker를 두 방식으로 추론 -> 시드 쌍 재표본
            "leak_effect": boot_pair(rs, "primary", rs, "as_written", paired_seeds=True),
            "leak_effect_tie_random": boot_pair(rs, "primary.tie_random", rs, "as_written.tie_random", paired_seeds=True, metrics=["mrr", "precision@5"]),
        }

    # 버전 비교: 같은 조건끼리 여러 쌍으로(서로 다른 모델 -> 시드 독립 재표본).
    version_comparison = version_comparison_pairs(runs)

    # 모델 설정별 추론 시점 누출 효과(2차 - 1차, 같은 모델이라 시드 쌍 재표본).
    model_leak_effects = {}
    for name in LEAK_EFFECT_ARMS:
        rs = runs[name]
        model_leak_effects[name] = {
            "primary_mrr_mean": float(np.mean([r["primary"]["aggregate_metrics"]["mrr"] for r in rs])),
            "as_written_mrr_mean": float(np.mean([r["as_written"]["aggregate_metrics"]["mrr"] for r in rs])),
            "n_trees": [r.get("n_trees") for r in rs],
            "best_iteration": [r.get("best_iteration") for r in rs],
            "objective": rs[0].get("objective_used"),
            "rounds_policy": rs[0].get("rounds_policy"),
            "es_valid_order": rs[0].get("es_valid_order"),
            "leak_effect": boot_pair(rs, "primary", rs, "as_written", paired_seeds=True, metrics=["mrr", "precision@5"]),
        }

    decomposition_summary = {
        name: {
            "primary": summarize_field(runs[name], "primary"),
            "primary_tie_random": summarize_field(runs[name], "primary.tie_random"),
            "as_written": summarize_field(runs[name], "as_written"),
            "primary_seen_filtered": summarize_field(runs[name], "primary.seen_filtered"),
            "primary_unshown_filtered": summarize_field(runs[name], "primary.unshown_filtered"),
            "primary_cold": _mean_of(runs[name], "primary.cold"),
            "primary_warm": _mean_of(runs[name], "primary.warm"),
            "primary_unshown_list_len": _unshown_len_summary(runs[name]),
        }
        for name in DECOMP_ARMS
    }
    decomposition_summary["impression"]["inner_split_groups_on_both_sides"] = [r.get("inner_split_groups_on_both_sides") for r in runs["impression"]]
    decomposition_summary["impression"]["n_train"] = [r.get("n_train") for r in runs["impression"]]
    decomposition_summary["random_negatives_n_train"] = [r.get("n_train") for r in runs["current"]]

    decomposition_bootstrap = {
        # (A, B): effect = B - A. 서로 다른 모델이라 시드 독립 재표본(보수적).
        "leakage_lambdarank_early_stopping": boot_pair(runs["current"], "primary", runs["leaky"], "primary"),
        "leakage_lambdarank_early_stopping_tie_random": boot_pair(runs["current"], "primary.tie_random", runs["leaky"], "primary.tie_random", metrics=["mrr", "precision@5"]),
        "leakage_binary": boot_pair(runs["binary"], "primary", runs["leaky_binary"], "primary"),
        "leakage_lambdarank_fixed100": boot_pair(runs["fixed100"], "primary", runs["leaky_fixed100"], "primary"),
        "objective_lambdarank_es_vs_binary": boot_pair(runs["current"], "primary", runs["binary"], "primary"),
        "objective_lambdarank_fixed100_vs_binary": boot_pair(runs["fixed100"], "primary", runs["binary"], "primary"),
        "rounds_es_vs_fixed100": boot_pair(runs["current"], "primary", runs["fixed100"], "primary"),
        "rounds_es_vs_fixed100_tie_random": boot_pair(runs["current"], "primary.tie_random", runs["fixed100"], "primary.tie_random", metrics=["mrr", "precision@5"]),
        "negatives_random_vs_impression": boot_pair(runs["current"], "primary", runs["impression"], "primary"),
        # 조기 종료용 inner-valid 행 순서만 다르다(엔진 순서 A -> 섞음 B). 같은 시드는 학습 데이터와
        # 트리 열을 공유하고 멈추는 지점만 달라 시드를 쌍으로 재표본한다.
        "es_valid_order_engine_vs_shuffled": boot_pair(
            runs["current_es_engine_order"], "primary", runs["current"], "primary", paired_seeds=True
        ),
        "es_valid_order_engine_vs_shuffled_tie_random": boot_pair(
            runs["current_es_engine_order"], "primary.tie_random", runs["current"], "primary.tie_random",
            paired_seeds=True, metrics=["mrr", "precision@5"],
        ),
    }

    # 모델 - 베이스라인 (같은 정답/후보/answer_start). 모델 변형 3개 x 베이스라인 전부.
    model_vs_baseline = {}
    for model_name, rs in [("current_lambdarank_es", runs["current"]), ("current_lambdarank_fixed100", runs["fixed100"]), ("current_binary", runs["binary"]), ("team_final", runs["team-final"])]:
        model_vs_baseline[model_name] = {}
        for bname, b in base_team["baselines"].items():
            entry = {
                "plain": boot_model_vs_baseline(rs, "primary", b["per_user"]),
                "unshown_filtered": boot_model_vs_baseline(rs, "primary.unshown_filtered", b["unshown_filtered"]["per_user"]),
            }
            if _field(rs[0], "primary.tie_random"):
                entry["plain_tie_random"] = boot_model_vs_baseline(rs, "primary.tie_random", b["per_user"])
            model_vs_baseline[model_name][bname] = entry

    small_vs_baseline = {}
    for bname, b in base_small["baselines"].items():
        small_vs_baseline[bname] = {
            "plain": boot_model_vs_baseline(runs["small_recent_15"], "primary", b["per_user"]),
            "plain_tie_random": boot_model_vs_baseline(runs["small_recent_15"], "primary.tie_random", b["per_user"]),
        }

    tfw_runs = runs["team-final"]
    tfw = {
        "clicks_only": summarize_field(tfw_runs, "team_final_written"),
        "all_rows_definition_error": summarize_field(tfw_runs, "team_final_written_all_rows_definition_error"),
        "random_reference": tfw_random,
        "mean_answers_per_user": {
            "clicks_only": tfw_runs[0]["team_final_written"]["mean_answers_per_user"],
            "all_rows_definition_error": tfw_runs[0]["team_final_written_all_rows_definition_error"]["mean_answers_per_user"],
        },
        "note_clicks_only": tfw_runs[0]["team_final_written"]["note"],
        "note_all_rows": tfw_runs[0]["team_final_written_all_rows_definition_error"]["note"],
    }
    tfw["model_minus_random_clicks_only"] = {
        mk: {
            "effect": tfw["clicks_only"][mk]["mean"] - tfw_random["clicks_only"]["aggregate"][mk],
            "model_mean": tfw["clicks_only"][mk]["mean"],
            "random_mean": tfw_random["clicks_only"]["aggregate"][mk],
        }
        for mk in ["mrr", "precision@5"]
    }

    print("=" * 70)
    print("6) 데이터 구조 / 메타데이터")
    print("=" * 70)
    data_structure = data_structure_diagnostics(bundle, answer_start_ts, answer_start)
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
        "report_version": "v2.2",
        "superseded_report": "team_repro_v1 (BLOCKER 다수로 unsound 판정, 상단 배너 참고)",
        "es_valid_order_default": ES_VALID_ORDER_DEFAULT,
        "low_best_iteration_max": LOW_BEST_ITERATION_MAX,
        "git": {
            "harness_sha": harness_sha,
            "harness_dirty": tree_state["dirty"],
            "harness_diff_hash": tree_state["diff_hash"],
            "input_content_hashes": input_hashes,
            "run_provenance_harness_shas": sorted({
                r.get("harness_sha") for rs in list(runs.values()) + list(gen_runs.values()) for r in rs
            }),
            "current_branch": subprocess.run(
                ["git", "-C", str(WORKTREE_ROOT), "branch", "--show-current"], capture_output=True, text=True,
            ).stdout.strip(),
            "team_final_sha": code_shas["team-final"],
            "fix_snapshot_sha": code_shas["fix-snapshot"],
        },
        "config_hashes": {f"{v}|{o}": h for (v, o), h in config_hashes.items()},
        "config_overrides": {f"{v}|{o}": ov for (v, o), ov in config_overrides_by.items() if ov},
        "data_sha256": data_sha256,
        "seeds": {
            "current_team_final_and_all_current_arms": seeds_main,
            "fix_snapshot": seeds_fixsnap,
        },
        "tie_draws": TIE_DRAWS,
        "random_baseline_draws": RANDOM_DRAWS,
        "fixed_rounds_sensitivity": FIXED_ROUNDS,
        "n_boot": 1000,
        "category_recovery": category_recovery,
        "answer_start": answer_start,
        "cache_key_fields": [
            "version", "protocol", "label_mode", "leakage_mode", "candidate_pool", "negative_source",
            "objective_override", "fixed_rounds", "seed",
            "harness pipeline-input content hash (pipeline/file_loader/config_loader/metrics/protocol .py, 커밋 여부 무관)",
            "engine code (current: src/*.py+config.yaml 내용 해시, team-final/fix-snapshot: 아카이브 ref SHA)",
            "config hash", "data sha256", "answer_start", "top_k", "tie_draws", "es_valid_order",
        ],
    }

    final = {
        "meta": meta,
        "data_structure": data_structure,
        "headline": headline,
        "headline_ci": headline_ci,
        "version_comparison": version_comparison,
        "model_leak_effects": model_leak_effects,
        "early_stopping_tie_artefact": early_stopping_tie_artefact(runs, gen_runs),
        "team_final_written": tfw,
        "generator_split": {v: {"summary": summarize_field(rs, "primary")} for v, rs in gen_runs.items()},
        "baselines": {
            "team_split": _strip_per_user(base_team),
            "generator_split": _strip_per_user(base_gen),
            "small_recent_15": _strip_per_user(base_small),
        },
        "model_vs_baseline": model_vs_baseline,
        "small_pool_model_vs_baseline": small_vs_baseline,
        "decomposition_summary": decomposition_summary,
        "decomposition_bootstrap": decomposition_bootstrap,
        "reproduction_check_vs_v21": reproduction_check_vs_v21(runs, gen_runs),
    }
    (REPORT_DIR / "team_repro_v2.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n결과 저장: {REPORT_DIR / 'team_repro_v2.json'}")

    raw_all = {
        "runs": runs, "generator_split_runs": gen_runs,
        "baselines_full": {"team_split": base_team, "generator_split": base_gen, "small_recent_15": base_small},
    }
    (REPORT_DIR / "team_repro_v2_raw.json").write_text(
        json.dumps(_round_floats(raw_all), ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(f"원자료 저장: {REPORT_DIR / 'team_repro_v2_raw.json'}")
    return 0


def _mean_of(results: List[dict], field: str) -> dict:
    blocks = [_field(r, field) for r in results]
    if not blocks or any(not b for b in blocks):
        return {}
    keys = [k for k in blocks[0] if k != "num_users"]
    out = {k: float(np.mean([b[k] for b in blocks])) for k in keys}
    out["num_users"] = blocks[0].get("num_users")
    out["n_seeds"] = len(blocks)
    return out


def _unshown_len_summary(results: List[dict]) -> dict:
    """unshown 필터 뒤 추천 리스트 길이(평가 유저, 시드 평균). top-K에서 사후 제거라 5건 미만이 될 수 있다."""
    stats = [(_field(r, "primary.unshown_filtered") or {}).get("list_len_stats") for r in results]
    stats = [st for st in stats if st and st.get("mean_len") is not None]
    if not stats:
        return {}
    n_users = [st["n_users"] for st in stats]
    lt5 = [st.get("n_users_len_lt_5", 0) for st in stats]
    return {
        "mean_len": float(np.mean([st["mean_len"] for st in stats])),
        "min_len": int(min(st["min_len"] for st in stats)),
        "share_len_lt_5": float(sum(lt5) / sum(n_users)) if sum(n_users) else None,
        "n_users": n_users[0],
    }


def _strip_per_user(base: dict) -> dict:
    def strip(d):
        if isinstance(d, dict):
            return {k: strip(v) for k, v in d.items() if k != "per_user"}
        return d
    return strip(base)


def _round_floats(obj, nd: int = 6):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round_floats(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, nd) for v in obj]
    return obj


if __name__ == "__main__":
    sys.exit(main())
