"""EB-NeRD v1.2 콜드 regime 실험 사슬 실행기 (E1–E4, E6–E8) — 단계별 체크포인트와 재개.

    python -m evaluation.recsys.ebnerd.run_cold --dataset ebnerd_small --root <EBNERD_ROOT> \
        --out-dir <OUT> --seeds 0 1 2 --n-boot 1000 --p2-sample 20000 --sub-cap 20000 --threads 2 \
        --resume --stage <fit|e1|e2p2|e8|e4|e2p1|assemble|all>

무엇을 어떻게 판정하는지는 실행 전에 ADR 0013 "A2 사전 등록"과 preregistration/cold-v1.2.yaml에 고정했다. 이 파일은
그 등록을 실행할 뿐이고, 표본·시드·arm·임계값을 전부 yaml에서 읽는다. 판정용 실행은 Colab CPU 런타임에서
scripts/m4_colab_driver.py가 단계마다 서브프로세스로 부른다(개발용 Mac에서는 돌리지 않는다).

단계와 산출물(<OUT> 아래):
  fit       models/<arm>_s<seed>.txt|.json, heuristic/fit_<set>_s<seed>.json
  e1        units/e1_{base,sub20,sub5,sub1}.npz   (요청별 지표 배열: 조건 x 풀 크기 x 방법 x seed)
  e2p2      units/e2p2_k{0,1,3,5,10,all}.npz
  e8        units/e8_{serving,harness}.npz
  e4        units/e4_p3.npz
  e2p1      units/e2p1_k{...}.npz
  assemble  ebnerd_v1_2_cold.json, ebnerd_v1_2_cold.md   (CI·쌍체 차이·기계 판정은 여기서 배열로부터 계산)

재현 게이트: e1의 첫 단위(e1_base)가 끝나면 바로 게이트(poolneg, 원본 조건·전체 풀의 nDCG@10 seed 평균이 v1 CI 안)를
계산해 <OUT>/reproduction_gate.json에 적는다. 증거 등급 실행에서 실패면 종료 코드 4로 멈춘다 — 게이트 실패는 등록상
실행 무효라서, 나머지 단계에 예산을 쓰기 전에 끝낸다. demo 등급 실행은 기록만 하고 계속 돈다.

재개: 단위(모델 하나, 조건 하나)가 끝날 때마다 파일을 원자적으로 쓰고 progress.json에 적는다. 다시 실행하면 설정 해시
(사전 등록 sha, 데이터 sha, seed, 표본 크기, 코드 SHA)가 같을 때만 끝난 단위를 건너뛴다. 다르면 거부한다.

EB-NeRD는 연구용 라이선스다. 요청별 배열에는 익명화한 유저 묶음 번호와 지표만 있고, 로그에는 집계 수치만 찍는다.
배열·모델 파일은 저장소에 커밋하지 않는다. 커밋하는 것은 집계 JSON과 표뿐이다.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import lightgbm as lgb
import numpy as np
import pandas as pd

from recsys_core import compute_features

from ..metrics import group_ids, ranking_metrics
from .candidate_config import HARNESS, SERVING, union_mask
from .cold_transforms import (
    SHRUNK_COLUMN,
    add_shrunk_ctr,
    behaviour_users,
    mask_training_popularity,
    p3_context,
    p3_task,
    pool_shrink_keep,
    rank_normalize,
    release_age_bucket,
    subsample_item_logs,
    subsample_request_indices,
    subsample_users,
)
from .cold_verdicts import (
    DEMO_GRADE,
    EVIDENCE_GRADE,
    cell_key,
    cold_verdicts,
    load_prereg,
    prereg_commit,
    prereg_sha256,
    reproduction_gate,
)
from .heuristic_fit import fit_pairwise_logistic, fitted_scores, heuristic_terms, prior_scores
from .loaders import ebnerd_root
from .models import (
    ABLATION,
    ALL_GROUPS,
    EARLY_STOPPING,
    NUM_BOOST_ROUND,
    V2_FEATURES,
    ModelSpec,
    baseline_scores,
    feature_matrix,
    train,
)
from .neural.cold import pop_mask_raw, truncate_logs
from .prepare import (
    Bench,
    RankTask,
    filter_candidates,
    impressions_in,
    load_bench,
    p1_task,
    p2_task,
    pool_negative_task,
    protocol_windows,
    seen_mask,
)
from .run_ebnerd import SPLIT_SEED, MetricBank, _git_sha, _iso, _json_default

log = logging.getLogger("ebnerd.cold")

P2_METRICS = ("ndcg@10", "recall@10", "mrr")
P1_CURVE_METRICS = ("ndcg@10", "auc")
BASELINES = ("random", "popularity_6h", "popularity_24h", "recency", "cosine_history")
REPORT_JSON = "ebnerd_v1_2_cold.json"
REPORT_MD = "ebnerd_v1_2_cold.md"
GATE_JSON = "reproduction_gate.json"
EXIT_CONFIG_MISMATCH = 3
EXIT_GATE_FAILED = 4      # scripts/m4_colab_driver.py의 RUN_COLD_EXIT_GATE와 같은 값(테스트가 묶는다)
# 드라이버가 manifest의 articles.original_sha256(올리기 전에 로컬에서 잰 원본 기사 파일의 sha256)을 넘기는 환경변수
ARTICLES_ORIGINAL_ENV = "M4_ARTICLES_ORIGINAL_SHA256"


class ConfigMismatch(RuntimeError):
    pass


class ReproductionGateFailed(RuntimeError):
    pass


# --- 체크포인트 저장소 -------------------------------------------------------------------------

def environment(threads: Optional[int] = None) -> dict:
    """계산이 돈 환경. 재개가 다른 환경에서 이뤄지면 progress.json에 둘 다 남는다."""
    import pyarrow

    return {"python": platform.python_version(), "machine": platform.machine(), "system": platform.system(),
            "numpy": np.__version__, "pandas": pd.__version__, "pyarrow": pyarrow.__version__,
            "lightgbm": lgb.__version__, "threads": threads}


def config_hash(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=_json_default).encode()).hexdigest()


class Store:
    """<out-dir>의 단위별 산출물과 progress.json. 파일은 임시 이름으로 쓴 뒤 바꿔 넣어, 중간에 죽어도 반쯤 쓴 단위가
    "완료"로 남지 않는다."""

    def __init__(self, out_dir: Path, config: dict, resume: bool, bench_info: Optional[dict] = None,
                 env: Optional[dict] = None):
        self.dir = Path(out_dir)
        self.path = self.dir / "progress.json"
        config = json.loads(json.dumps(config, default=_json_default))
        digest = config_hash(config)
        if self.path.exists():
            prev = json.loads(self.path.read_text())
            if prev.get("config_hash") != digest:
                old = prev.get("config", {})
                changed = sorted(k for k in set(old) | set(config) if old.get(k) != config.get(k))
                raise ConfigMismatch(f"설정 해시가 이전 실행과 다릅니다(달라진 항목: {changed}). 이어서 돌릴 수 없습니다.")
            if not resume:
                raise ConfigMismatch("이 out-dir에 이전 실행의 체크포인트가 있습니다. --resume으로 이어서 돌리거나 새 디렉터리를 쓰세요.")
            self.progress = prev
        else:
            self.progress = {"config_hash": digest, "config": config, "bench_info": bench_info, "environments": [],
                             "units": {}}
        if env and env not in self.progress.setdefault("environments", []):
            self.progress["environments"].append(env)
        for sub in ("units", "models", "heuristic"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self._flush()

    @classmethod
    def open_existing(cls, out_dir: Path) -> "Store":
        """이미 있는 체크포인트를 읽기만 한다(assemble 전용). 데이터 없이 내려받은 배열만으로 리포트를 다시 만들 때 쓴다."""
        self = cls.__new__(cls)
        self.dir = Path(out_dir)
        self.path = self.dir / "progress.json"
        if not self.path.exists():
            raise ConfigMismatch(f"{self.path}가 없습니다. assemble할 체크포인트가 없습니다.")
        self.progress = json.loads(self.path.read_text())
        return self

    def _flush(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.progress, indent=1, ensure_ascii=False, default=_json_default))
        os.replace(tmp, self.path)

    def done(self, unit: str) -> bool:
        info = self.progress["units"].get(unit)
        return bool(info) and all((self.dir / f).exists() for f in info.get("files", []))

    def _mark(self, unit: str, files: list[str], seconds: float):
        self.progress["units"][unit] = {"files": files, "seconds": round(seconds, 1)}
        self._flush()

    def save_arrays(self, unit: str, arrays: dict[str, np.ndarray], meta: dict, seconds: float):
        npz, js = f"units/{unit}.npz", f"units/{unit}.json"
        tmp = self.dir / f"units/{unit}.tmp.npz"
        np.savez_compressed(tmp, **arrays)
        os.replace(tmp, self.dir / npz)
        self.write_json(js, meta)
        self._mark(unit, [npz, js], seconds)

    def load_arrays(self, unit: str) -> tuple[dict[str, np.ndarray], dict]:
        with np.load(self.dir / f"units/{unit}.npz") as z:
            arrays = {k: z[k] for k in z.files}
        return arrays, json.loads((self.dir / f"units/{unit}.json").read_text())

    def write_json(self, rel: str, obj: dict):
        tmp = self.dir / (rel + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=_json_default))
        os.replace(tmp, self.dir / rel)

    def save_model(self, arm: str, seed: int, model, extra: dict, seconds: float):
        txt, js = f"models/{arm}_s{seed}.txt", f"models/{arm}_s{seed}.json"
        tmp = self.dir / (txt + ".tmp")
        model.booster.save_model(str(tmp))
        os.replace(tmp, self.dir / txt)
        self.write_json(js, {**model.summary(), "features": list(model.spec.features), **extra})
        self._mark(f"model:{arm}:s{seed}", [txt, js], seconds)

    def load_model(self, arm: str, seed: int) -> "StoredModel":
        info = json.loads((self.dir / f"models/{arm}_s{seed}.json").read_text())
        booster = lgb.Booster(model_file=str(self.dir / f"models/{arm}_s{seed}.txt"))
        return StoredModel(arm, seed, booster, int(info["best_iteration"]), list(info["features"]), info)

    def save_fit(self, name: str, seed: int, summary: dict, seconds: float):
        rel = f"heuristic/fit_{name}_s{seed}.json"
        self.write_json(rel, summary)
        self._mark(f"hfit:{name}:s{seed}", [rel], seconds)

    def load_fit(self, name: str, seed: int) -> dict:
        return json.loads((self.dir / f"heuristic/fit_{name}_s{seed}.json").read_text())


@dataclasses.dataclass
class StoredModel:
    arm: str
    seed: int
    booster: lgb.Booster
    best_iteration: int
    features: list[str]
    info: dict

    def predict(self, feats: pd.DataFrame, task: RankTask) -> np.ndarray:
        return self.booster.predict(feature_matrix(feats, task, self.features), num_iteration=self.best_iteration)


# --- 실행 컨텍스트 -----------------------------------------------------------------------------

@dataclasses.dataclass
class Run:
    args: argparse.Namespace
    prereg: dict
    bench: Bench
    windows: dict
    idx: dict
    store: Store
    seeds: list[int]
    _subsample_cache: dict = dataclasses.field(default_factory=dict)

    @property
    def off(self) -> dict:
        return self.prereg["seed_offsets"]

    @property
    def split_seed(self) -> int:
        return int(self.prereg["statistics"]["split_seed"])

    def features(self, ctx, req, label: str) -> pd.DataFrame:
        t0 = time.time()
        f = compute_features(ctx, req, groups=ALL_GROUPS)
        log.info("features %s: %d requests, %d pairs, %.1fs", label, req.n, len(f), time.time() - t0)
        return f

    def p2_idx(self) -> np.ndarray:
        rng = np.random.default_rng(self.split_seed + self.off["p2_sample"])
        test = self.idx["test"]
        return np.sort(rng.choice(test, size=min(self.args.p2_sample, len(test)), replace=False))

    def subsample(self, traffic: str) -> dict:
        """서브샘플 유저, 그 유저들의 인기도 로그, 평가 요청 인덱스."""
        if traffic not in self._subsample_cache:
            frac = self.prereg["conditions"]["subsample_fractions"][traffic]
            users = subsample_users(behaviour_users(self.bench), frac, self.split_seed + self.off["user_subsample"])
            clicks, views = subsample_item_logs(self.bench.imps, self.bench.catalog, users)
            idx = subsample_request_indices(self.bench.imps["validation"], self.windows["test"], users,
                                            cap=self.args.sub_cap, seed=self.split_seed + self.off["subsample_requests"])
            self._subsample_cache[traffic] = {"users": users, "clicks": clicks, "views": views, "idx": idx,
                                              "fraction": frac}
        return self._subsample_cache[traffic]

    def sub_ctx(self, split: str, traffic: str):
        s = self.subsample(traffic)
        return dataclasses.replace(self.bench.ctx[split], item_clicks=s["clicks"], item_inviews=s["views"])

    def arm_cfg(self, arm: str) -> dict:
        return self.prereg["arms"][arm]

    def arm_spec(self, arm: str) -> ModelSpec:
        cfg = self.arm_cfg(arm)
        if cfg["data"] == "inview":
            base = next(s for s in ABLATION if s.name == "ranker_v2")
            return dataclasses.replace(base, name=arm)
        feats = list(V2_FEATURES) + ([SHRUNK_COLUMN] if "shrunk_alpha" in cfg else [])
        return ModelSpec(arm, "pool_neg", "lambdarank", "request", feats,
                         f"풀 네거티브 arm(사전 등록): {json.dumps(cfg, ensure_ascii=False, default=str)}")

    def heuristic_weights(self) -> dict:
        return self.prereg["heuristics"]["prior_weights"]

    @property
    def tau(self) -> float:
        return float(self.prereg["heuristics"]["recency_tau_hours"])


def _anon_clusters(users: np.ndarray) -> np.ndarray:
    """부트스트랩용 유저 묶음 번호(0..). 체크포인트에 원 유저 id를 남기지 않는다."""
    return np.unique(users, return_inverse=True)[1].astype(np.int32)


def _stack(per_group: dict, metrics=P2_METRICS) -> np.ndarray:
    return np.stack([per_group[m] for m in metrics]).astype(np.float32)


def _task_meta(task: RankTask) -> dict:
    pool = task.req.n_candidates
    in_pool = np.bincount(task.req.pair_req[np.asarray(task.labels, bool)], minlength=task.req.n)
    npos = task.n_pos_total if task.n_pos_total is not None else in_pool
    return {"n_requests": int(task.req.n), "n_users": int(len(np.unique(task.group_user))),
            "pool_size_mean": float(pool.mean()) if len(pool) else 0.0,
            "pool_size_min": int(pool.min()) if len(pool) else 0, "pool_size_max": int(pool.max()) if len(pool) else 0,
            "recall_upper_bound": float(in_pool.sum() / max(float(np.sum(npos)), 1.0))}


# --- fit 단계 ----------------------------------------------------------------------------------

def _training_frames(run: Run, arm: str, fit, es, ctx, seed: int) -> tuple[tuple, tuple, dict]:
    """arm의 학습 시 변환을 fit·es에 적용한다. 마스크 난수는 fit -> es 순으로 한 스트림에서 뽑는다."""
    cfg, rc = run.arm_cfg(arm), run.prereg["features"]
    (tf, ff), (te, fe) = fit, es
    info: dict = {}
    if "train_mask" in cfg:
        m = cfg["train_mask"]
        rng = np.random.default_rng(run.off["train_mask"] + seed)
        ff, mf = mask_training_popularity(ff, tf.req, m["p"], rng, float(m["value"]))
        fe, me = mask_training_popularity(fe, te.req, m["p"], rng, float(m["value"]))
        info = {"masked_fit_request_share": float(mf.mean()), "masked_es_request_share": float(me.mean())}
    if "shrunk_alpha" in cfg:
        ff = add_shrunk_ctr(ff, tf.req, ctx, cfg["shrunk_alpha"])
        fe = add_shrunk_ctr(fe, te.req, ctx, cfg["shrunk_alpha"])
    if cfg.get("rank_normalize"):
        ff = rank_normalize(ff, tf.req.cand_ptr, rc["rank_columns"])
        fe = rank_normalize(fe, te.req.cand_ptr, rc["rank_columns"])
    return (tf, ff), (te, fe), info


def _train_arm(run: Run, arm: str, fit, es, ctx, seed: int):
    t0 = time.time()
    fit_v, es_v, info = _training_frames(run, arm, fit, es, ctx, seed)
    lgbm = run.prereg["lightgbm"]
    m = train(run.arm_spec(arm), fit_v, es_v, seed=seed, num_threads=run.args.threads,
              extra_params=dict(lgbm["extra_params"]))
    run.store.save_model(arm, seed, m, info, time.time() - t0)
    log.info("trained %s seed=%d best_it=%d es=%.4f %.0fs", arm, seed, m.best_iteration, m.best_score, m.seconds)


def stage_fit(run: Run):
    arms = run.prereg["arms"]
    ctx = run.bench.ctx["train"]
    e7 = run.prereg["e7"]
    by_window: dict = {}
    for a, c in arms.items():
        if c["data"] == "pool_neg":
            by_window.setdefault(int(c["window_h"]), []).append(a)
    inview = [a for a, c in arms.items() if c["data"] == "inview"]
    inview_sets = None
    for seed in run.seeds:
        for window_h, names in sorted(by_window.items()):
            todo = [a for a in names if not run.store.done(f"model:{a}:s{seed}")]
            fits = [n for n in e7["sets"] if not run.store.done(f"hfit:{n}:s{seed}")] if window_h == 48 else []
            if not todo and not fits:
                continue
            # v1 ranker_v2_poolneg와 같은 네거티브: default_rng(1000 + seed)에서 fit -> es 순으로 뽑는다.
            rng = np.random.default_rng(run.off["pool_negatives"] + seed)
            tf = pool_negative_task(run.bench, "train", run.idx["fit"], rng, window_h=window_h)
            te = pool_negative_task(run.bench, "train", run.idx["es"], rng, window_h=window_h)
            fit = (tf, run.features(ctx, tf.req, f"pool{window_h}-fit-s{seed}"))
            es = (te, run.features(ctx, te.req, f"pool{window_h}-es-s{seed}"))
            for a in todo:
                _train_arm(run, a, fit, es, ctx, seed)
            if "a" in fits:
                t0 = time.time()
                res = fit_pairwise_logistic(heuristic_terms(fit[1], run.tau), tf.labels, tf.req.cand_ptr,
                                            l2=e7["l2"], max_iter=e7["max_iter"], tol=e7["tol"])
                run.store.save_fit("a", seed, res.summary(), time.time() - t0)
                log.info("heuristic fit a seed=%d weights=%s", seed, np.round(res.weights, 4).tolist())
            if "b" in fits:
                _fit_heuristic_b(run, seed)
        todo = [a for a in inview if not run.store.done(f"model:{a}:s{seed}")]
        if todo:
            if inview_sets is None:  # 노출 네거티브 데이터는 seed와 무관하다
                tf, te = p1_task(run.bench, "train", run.idx["fit"]), p1_task(run.bench, "train", run.idx["es"])
                inview_sets = ((tf, run.features(ctx, tf.req, "inview-fit")), (te, run.features(ctx, te.req, "inview-es")))
            for a in todo:
                _train_arm(run, a, inview_sets[0], inview_sets[1], ctx, seed)


def _fit_heuristic_b(run: Run, seed: int):
    """(b) 세트: fit 창의 sub1 유저 요청, sub1 인기도. 네거티브는 (a)와 같은 규칙·같은 seed."""
    t0 = time.time()
    e7 = run.prereg["e7"]
    users = run.subsample("sub1")["users"]
    tr = run.bench.imps["train"]
    idx = run.idx["fit"][np.isin(tr.user_id[run.idx["fit"]], users)]
    if len(idx) == 0:
        res = fit_pairwise_logistic(np.zeros((0, 4)), np.zeros(0, bool), np.array([0]))
    else:
        rng = np.random.default_rng(run.off["pool_negatives"] + seed)
        task = pool_negative_task(run.bench, "train", idx, rng, window_h=48)
        feats = run.features(run.sub_ctx("train", "sub1"), task.req, f"hfit-b-s{seed}")
        res = fit_pairwise_logistic(heuristic_terms(feats, run.tau), task.labels, task.req.cand_ptr,
                                    l2=e7["l2"], max_iter=e7["max_iter"], tol=e7["tol"])
    run.store.save_fit("b", seed, {**res.summary(), "n_fit_requests": int(len(idx))}, time.time() - t0)
    log.info("heuristic fit b seed=%d requests=%d weights=%s", seed, len(idx), np.round(res.weights, 4).tolist())


# --- 조건 평가 공통 ----------------------------------------------------------------------------

def _eval_view(run: Run, arm: str, raw: pd.DataFrame, req, ctx, ptr: np.ndarray, pop_value: Optional[float]) -> pd.DataFrame:
    """arm이 받는 평가 입력: raw -> (축소 CTR 열 추가) -> (인기도 raw 값 강제) -> (요청 내 랭크). 순서가 정의의 일부다."""
    cfg = run.arm_cfg(arm)
    f = raw
    if "shrunk_alpha" in cfg:
        f = add_shrunk_ctr(f, req, ctx, cfg["shrunk_alpha"])
    if pop_value is not None:
        f = pop_mask_raw(f, value=pop_value, extra_columns=(SHRUNK_COLUMN,))
    if cfg.get("rank_normalize"):
        f = rank_normalize(f, ptr, run.prereg["features"]["rank_columns"])
    return f


def _non_model_scores(run: Run, feats: pd.DataFrame, seed: int, fits: dict) -> dict[str, np.ndarray]:
    base = baseline_scores(feats, len(feats), seed)
    out = {k: base[k] for k in BASELINES}
    w = run.heuristic_weights()
    out["heuristic_cold"] = prior_scores(feats, w, run.tau, with_popularity=False)
    out["heuristic_prior4"] = prior_scores(feats, w, run.tau, with_popularity=True)
    for name, by_seed in fits.items():
        out[f"heuristic_fit_{name}"] = fitted_scores(feats, by_seed[seed]["weights"], run.tau)
    return out


def _eval_grid(run: Run, task: RankTask, raw: pd.DataFrame, ctx, traffic: str, pop_value: Optional[float],
               arms: list[str], arrays: dict, extra_nan_arm: Optional[str] = None):
    """한 트래픽 조건에서 풀 크기 4단계 x 모든 방법 x seed의 요청별 지표를 arrays에 채운다."""
    labels, npos = np.asarray(task.labels, bool), task.n_pos_total
    full_ptr = task.req.cand_ptr
    pools = {"full": np.ones(len(labels), dtype=bool)}
    for n in run.prereg["conditions"]["pool_sizes"]:
        pools[str(n)] = pool_shrink_keep(labels, full_ptr, int(n), run.split_seed + run.off["pool_shrink"])
    sub = {p: (keep, filter_candidates(task, keep) if p != "full" else task) for p, keep in pools.items()}
    fits = {n: {s: run.store.load_fit(n, s) for s in run.seeds} for n in run.prereg["e7"]["sets"]}
    view = pop_mask_raw(raw, value=pop_value) if pop_value is not None else raw

    def add(method: str, seed: int, scores: np.ndarray):
        for p, (keep, t) in sub.items():
            m = ranking_metrics(scores[keep], labels[keep], t.req.cand_ptr, ks=(10,), n_pos_total=npos, seed=seed,
                                with_auc=False)
            arrays[f"{traffic}|{p}|{method}|{seed}"] = _stack(m)

    for seed in run.seeds:
        for name, sc in _non_model_scores(run, view, seed, fits).items():
            add(name, seed, sc)
    for arm in arms:
        cfg = run.arm_cfg(arm)
        models = [run.store.load_model(arm, s) for s in run.seeds]
        if cfg.get("rank_normalize"):
            # 랭크는 "모델이 받는 후보 풀" 안에서 계산하므로 줄인 풀마다 다시 만든다.
            for p, (keep, t) in sub.items():
                f = _eval_view(run, arm, raw[keep].reset_index(drop=True) if p != "full" else raw, t.req, ctx,
                               t.req.cand_ptr, pop_value)
                for m in models:
                    r = ranking_metrics(m.predict(f, t), t.labels, t.req.cand_ptr, ks=(10,), n_pos_total=npos,
                                        seed=m.seed, with_auc=False)
                    arrays[f"{traffic}|{p}|{arm}|{m.seed}"] = _stack(r)
            continue
        f = _eval_view(run, arm, raw, task.req, ctx, full_ptr, pop_value)
        for m in models:
            add(arm, m.seed, m.predict(f, task))
        if arm == extra_nan_arm and pop_value is not None:
            fn = _eval_view(run, arm, raw, task.req, ctx, full_ptr, float("nan"))
            for m in models:
                add(f"{arm}@nan", m.seed, m.predict(fn, task))
        log.info("scored %s|%s", traffic, arm)


def _p2_arms(run: Run) -> list[str]:
    return [a for a, c in run.prereg["arms"].items() if c["data"] == "pool_neg" and int(c["window_h"]) == 48]


def _unit(run: Run, unit: str, fn: Callable[[], tuple[dict, dict]]):
    if run.store.done(unit):
        log.info("skip %s (완료된 단위)", unit)
        return
    t0 = time.time()
    arrays, meta = fn()
    run.store.save_arrays(unit, arrays, meta, time.time() - t0)
    log.info("unit %s done %.0fs", unit, time.time() - t0)


# --- E1 (+E3, E6, E7 평가) ---------------------------------------------------------------------

def stage_e1(run: Run):
    arms = _p2_arms(run)

    def base():
        task = p2_task(run.bench, "validation", run.p2_idx(), window_h=48, exclude_seen=True)
        ctx = run.bench.ctx["validation"]
        raw = run.features(ctx, task.req, "e1-base")
        arrays = {"clusters": _anon_clusters(task.group_user)}
        _eval_grid(run, task, raw, ctx, "orig", None, arms, arrays)
        _eval_grid(run, task, raw, ctx, "pop0", 0.0, arms, arrays, extra_nan_arm="poolneg_masknan")
        return arrays, {"traffic": ["orig", "pop0"], **_task_meta(task)}

    _unit(run, "e1_base", base)
    enforce_reproduction_gate(run)
    for traffic in run.prereg["conditions"]["subsample_fractions"]:
        def sub(traffic=traffic):
            s = run.subsample(traffic)
            ctx = run.sub_ctx("validation", traffic)
            task = p2_task(run.bench, "validation", s["idx"], window_h=48, exclude_seen=True)
            raw = run.features(ctx, task.req, f"e1-{traffic}")
            arrays = {"clusters": _anon_clusters(task.group_user)}
            if task.req.n:
                _eval_grid(run, task, raw, ctx, traffic, None, arms, arrays)
            return arrays, {"traffic": [traffic], "fraction": s["fraction"], "subsample_users": int(len(s["users"])),
                            "subsample_clicks": int(len(s["clicks"])), "subsample_inviews": int(len(s["views"])),
                            **_task_meta(task)}
        _unit(run, f"e1_{traffic}", sub)


def gate_from_checkpoint(store: Store, prereg: dict, seeds: list[int]) -> dict:
    """e1_base 체크포인트만으로 재현 게이트를 계산한다(부트스트랩 없음).

    리포트가 쓰는 값과 같은 식이다: 요청별 지표를 seed 평균한 뒤 요청 평균(MetricBank.summary의 "mean"). 판정은
    cold_verdicts.reproduction_gate 하나가 한다.
    """
    g = prereg["reproduction_gate"]
    metric = prereg["statistics"]["metric"]
    key = cell_key(g["cell"]["traffic"], g["cell"]["pool"])
    arrays, _ = store.load_arrays("e1_base")
    bank = _bank(arrays, f"{key}|", [g["arm"]], seeds, n_boot=0)
    methods = {}
    if g["arm"] in bank.runs:
        methods[g["arm"]] = {metric: {"mean": float(np.nanmean(bank.seed_mean(g["arm"])[metric]))}}
    return reproduction_gate({"e1": {"cells": {key: {"methods": methods}}}}, prereg)


def enforce_reproduction_gate(run: Run) -> dict:
    """e1의 기준 칸이 나오자마자 재현 게이트를 본다.

    게이트 실패는 사전 등록(A2.9)상 실행 무효다. 그 판단에 필요한 값은 e1_base에 다 있으므로, 남은 단계에 예산을 쓰기
    전에 여기서 멈춘다. 증거 등급 조건을 만족하지 않는 실행(demo)은 어차피 판정에 쓰지 않으므로 기록만 하고 계속 돈다.
    """
    gate = gate_from_checkpoint(run.store, run.prereg, run.seeds)
    grade = evidence_grade(run.store.progress["config"], run.args.n_boot, run.prereg)["grade"]
    record = {**gate, "evidence_grade": grade, "enforced": grade == EVIDENCE_GRADE, "checked_after": "e1_base"}
    run.store.write_json(GATE_JSON, record)
    log.info("reproduction gate: %s (%s nDCG@10 seed mean=%s, within=%s, enforced=%s)", gate["status"], gate["arm"],
             gate["mean"], gate["within"], record["enforced"])
    if record["enforced"] and gate["status"] == "fail":
        raise ReproductionGateFailed(
            f"재현 게이트 실패: {gate['arm']} nDCG@10 seed 평균 {gate['mean']}가 {gate['within']} 밖입니다. 이 실행은 무효입니다"
            f"(사전 등록 A2.9). 남은 단계를 돌리지 않습니다. 기록: {run.store.dir / GATE_JSON}")
    return record


# --- E2 ----------------------------------------------------------------------------------------

def _k_label(k) -> str:
    return "all" if k is None else str(k)


def _truncated_share(ctx, req, k) -> float:
    """절단이 실제로 걸린 요청의 비율(히스토리가 k건보다 많은 요청)."""
    if k is None or req.n == 0:
        return 0.0
    return float((ctx.user_log.count(req.user, 0, req.profile_cutoff) > k).mean())


def stage_e2p2(run: Run):
    r = run.prereg["e2"]
    arms = [r["model"], "poolneg_mask0"]
    for k in list(run.prereg["conditions"]["truncate_ks"]) + [None]:
        def unit(k=k):
            task = p2_task(run.bench, "validation", run.p2_idx(), window_h=48, exclude_seen=True)
            ctx = run.bench.ctx["validation"]
            tctx, treq = truncate_logs(ctx, task.req, k)
            raw = run.features(tctx, treq, f"e2p2-k{_k_label(k)}")
            arrays = {"clusters": _anon_clusters(task.group_user)}
            labels, ptr, npos = task.labels, task.req.cand_ptr, task.n_pos_total
            for traffic, pop_value in (("orig", None), ("pop0", 0.0)):
                view = pop_mask_raw(raw, value=pop_value) if pop_value is not None else raw
                for seed in run.seeds:
                    base = baseline_scores(view, len(view), seed)
                    scores = {"popularity_6h": base["popularity_6h"], "recency": base["recency"],
                              "cosine_history": base["cosine_history"],
                              "heuristic_cold": prior_scores(view, run.heuristic_weights(), run.tau, False)}
                    for name, sc in scores.items():
                        arrays[f"{traffic}|{name}|{seed}"] = _stack(ranking_metrics(
                            sc, labels, ptr, ks=(10,), n_pos_total=npos, seed=seed, with_auc=False))
                for arm in arms:
                    for seed in run.seeds:
                        m = run.store.load_model(arm, seed)
                        arrays[f"{traffic}|{arm}|{seed}"] = _stack(ranking_metrics(
                            m.predict(view, task), labels, ptr, ks=(10,), n_pos_total=npos, seed=seed, with_auc=False))
            return arrays, {"k": _k_label(k), "truncated_share": _truncated_share(ctx, task.req, k), **_task_meta(task)}
        _unit(run, f"e2p2_k{_k_label(k)}", unit)


def stage_e2p1(run: Run):
    r = run.prereg["e2"]["p1_descriptive"]
    for k in list(run.prereg["conditions"]["truncate_ks"]) + [None]:
        def unit(k=k):
            task = p1_task(run.bench, "validation", run.idx["test"])
            ctx = run.bench.ctx["validation"]
            tctx, treq = truncate_logs(ctx, task.req, k)
            feats = run.features(tctx, treq, f"e2p1-k{_k_label(k)}")
            arrays = {"clusters": _anon_clusters(task.group_user)}
            labels, ptr = task.labels, task.req.cand_ptr
            for seed in run.seeds:
                base = baseline_scores(feats, len(feats), seed)
                for name in (r["baseline"], "cosine_history", "random"):
                    arrays[f"{name}|{seed}"] = _stack(ranking_metrics(base[name], labels, ptr, ks=(10,), seed=seed),
                                                      P1_CURVE_METRICS)
                m = run.store.load_model(r["model"], seed)
                arrays[f"{r['model']}|{seed}"] = _stack(ranking_metrics(m.predict(feats, task), labels, ptr, ks=(10,),
                                                                        seed=seed), P1_CURVE_METRICS)
            return arrays, {"k": _k_label(k), "truncated_share": _truncated_share(ctx, task.req, k), **_task_meta(task)}
        _unit(run, f"e2p1_k{_k_label(k)}", unit)


# --- E4 ----------------------------------------------------------------------------------------

def stage_e4(run: Run):
    r = run.prereg["e4"]

    def unit():
        task = p3_task(run.bench, "validation", run.p2_idx(), release_hour=r["release_hour"], batches=r["batches"])
        ctx = p3_context(run.bench.ctx["validation"], r["release_hour"])
        feats = run.features(ctx, task.req, "e4-p3")
        labels, ptr = task.labels, task.req.cand_ptr
        arrays = {"clusters": _anon_clusters(task.group_user),
                  "bucket": release_age_bucket(task, r["bucket_edges_h"]).astype(np.int8)}
        for seed in run.seeds:
            base = baseline_scores(feats, len(feats), seed)
            for name in r["methods"]:
                if name in base:
                    sc = base[name]
                else:
                    sc = run.store.load_model(name, seed).predict(feats, task)
                # 버킷 표는 풀 안 정답만으로 이상적 DCG를 잡는다(n_pos_total 없음). 풀이 놓친 정답은 meta의 상한으로 본다.
                arrays[f"{name}|{seed}"] = _stack(ranking_metrics(sc, labels, ptr, ks=(10,), seed=seed, with_auc=False))
        return arrays, {"release_hour": r["release_hour"], "batches": r["batches"],
                        "bucket_edges_h": r["bucket_edges_h"], **_task_meta(task)}

    _unit(run, "e4_p3", unit)


# --- E8 ----------------------------------------------------------------------------------------

def stage_e8(run: Run):
    ctx = run.bench.ctx["validation"]

    def evaluate(cfg, descriptive_models: tuple = ()):
        name = cfg.name
        capped = cfg.cap is not None
        # 서빙은 cap을 적용한 뒤에 이미 읽은 것을 뺀다. 하네스 v1은 읽은 것을 뺀 풀에서 출처 순위를 매겼다.
        task = p2_task(run.bench, "validation", run.p2_idx(), window_h=cfg.window_h, exclude_seen=not capped)
        feats = run.features(ctx, task.req, f"e8-{name}")
        mask, info = union_mask(cfg, feats, task.req.cand_ptr, seed=0)
        if capped:
            keep = ~seen_mask(run.bench, "validation", task.req)
            info["pool_seen_fraction_removed"] = float((~keep).mean()) if len(keep) else 0.0
            task, feats, mask = filter_candidates(task, keep), feats[keep].reset_index(drop=True), mask[keep]
        labels, ptr, npos = np.asarray(task.labels, bool), task.req.cand_ptr, task.n_pos_total
        g = group_ids(ptr)
        # 랭커가 실제로 받는 후보 수: (cap 적용 뒤) 이미 읽은 것을 뺀 합집합 크기
        info["mean_union_size_after_seen"] = float(np.bincount(g[mask], minlength=task.req.n).mean()) if task.req.n else 0.0
        with np.errstate(invalid="ignore", divide="ignore"):
            recall = np.where(npos > 0, np.bincount(g[labels & mask], minlength=task.req.n) / npos, np.nan)
        arrays = {"clusters": _anon_clusters(task.group_user), "union_recall": recall.astype(np.float32)}
        for arm in (cfg.model,) + tuple(descriptive_models):
            for seed in run.seeds:
                sc = run.store.load_model(arm, seed).predict(feats, task)
                for label, s in (("full_pool", sc), ("two_stage", np.where(mask, sc, -np.inf))):
                    arrays[f"{label}|{arm}|{seed}"] = _stack(ranking_metrics(
                        s, labels, ptr, ks=(10,), n_pos_total=npos, seed=seed, with_auc=False))
        return arrays, {"config": dataclasses.asdict(cfg), "union": info, "judged_model": cfg.model,
                        "descriptive_models": list(descriptive_models), **_task_meta(task)}

    _unit(run, "e8_serving", lambda: evaluate(SERVING, descriptive_models=("poolneg",)))
    _unit(run, "e8_harness", lambda: evaluate(HARNESS))


# --- assemble ----------------------------------------------------------------------------------

def _bank(arrays: dict, prefix: str, methods: list[str], seeds: list[int], n_boot: int,
          metrics=P2_METRICS, subset: Optional[np.ndarray] = None) -> MetricBank:
    clusters = arrays["clusters"] if subset is None else arrays["clusters"][subset]
    bank = MetricBank(clusters, n_boot, metrics=metrics)
    for name in methods:
        for seed in seeds:
            key = f"{prefix}{name}|{seed}"
            if key in arrays:
                a = arrays[key] if subset is None else arrays[key][:, subset]
                bank.add(name, {m: a[i].astype(np.float64) for i, m in enumerate(metrics)})
    return bank


def _methods_in(arrays: dict, prefix: str) -> list[str]:
    seen: dict = {}
    for key in arrays:
        if key.startswith(prefix) and key.count("|") == prefix.count("|") + 1:
            seen[key[len(prefix):].rsplit("|", 1)[0]] = True
    return list(seen)


def _summaries(bank: MetricBank) -> dict:
    return {n: bank.summary(n) for n in bank.runs}


def _pairs_for_cell(methods: list[str]) -> list[tuple[str, str]]:
    pairs = [(m, "poolneg") for m in methods if m.startswith("poolneg_")]
    for m in ("poolneg", "poolneg_mask0"):
        pairs += [(m, b) for b in ("heuristic_cold", "popularity_6h", "recency")]
    for h in ("heuristic_fit_a", "heuristic_fit_b"):
        pairs += [(h, b) for b in ("heuristic_prior4", "popularity_6h", "heuristic_cold")]
    return [(a, b) for a, b in pairs if a in methods and b in methods]


def articles_linked_to_registration(config: dict, prereg: dict) -> bool:
    """기사 파일을 등록한 원본에 이을 수 있는가.

    원격 실행은 본문을 뺀 파생 파일을 쓰므로 그 파일의 sha256은 등록값과 맞춰 볼 수 없다. 그래서 (1) 쓴 파일이 원본
    그대로이거나 (2) 올린 쪽이 manifest에 적어 넘긴 원본 sha256이 등록값일 때만 이어진 것으로 본다. (2)는 올린 쪽에서
    잰 값이고 런타임 안에서는 다시 확인할 수 없다(원본을 올리지 않는다).
    """
    registered = prereg["data"]["articles_original_sha256"]
    return registered in (config.get("data_files", {}).get("articles.parquet"),
                          config.get("articles_original_sha256_manifest"))


def evidence_grade(config: dict, n_boot: int, prereg: dict) -> dict:
    """실행 인자·입력 파일이 사전 등록과 같은지. 다르면 demo 등급이고 판정에 쓰지 않는다."""
    run, reasons = prereg["run"], []
    for key in ("dataset", "seeds", "p2_sample", "sub_cap", "fake_dim", "max_fit", "max_test"):
        if config.get(key) != run[key]:
            reasons.append(f"{key}={config.get(key)!r} (등록값 {run[key]!r})")
    if n_boot != run["n_boot"]:
        reasons.append(f"n_boot={n_boot} (등록값 {run['n_boot']})")
    files = config.get("data_files", {})
    for f, sha in prereg["data"]["files_sha256"].items():
        if files.get(f) != sha:
            reasons.append(f"{f} sha256이 등록값과 다름")
    if not articles_linked_to_registration(config, prereg):
        reasons.append("기사 파일을 등록한 원본에 잇지 못함(원본 그대로가 아니고, manifest가 적은 원본 sha256도 등록값이 아님)")
    if config.get("embeddings_sha256") != prereg["data"]["embeddings_sha256"]:
        reasons.append("임베딩 sha256이 등록값과 다름")
    sha = str(config.get("code_sha") or "unknown")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):   # unknown, <sha>-dirty, 커밋 id 없는 tarball 모두 여기 걸린다
        reasons.append(f"코드 SHA 확인 불가({sha})")
    if config.get("prereg_sha256") != prereg_sha256():
        reasons.append("사전 등록 yaml의 sha256이 실행 당시와 다름")
    return {"grade": DEMO_GRADE if reasons else EVIDENCE_GRADE, "reasons": reasons}


def assemble(store: Store, prereg: dict, seeds: list[int], n_boot: int, meta: dict) -> dict:
    """체크포인트 배열에서 CI·쌍체 차이·판정을 계산해 리포트 dict를 만든다. 없는 단위는 미측정으로 남는다."""
    units = store.progress["units"]
    cond = prereg["conditions"]
    pools = ["full"] + [str(n) for n in cond["pool_sizes"]]
    d: dict = {"meta": meta, "protocol": {
        "preregistration": prereg["id"], "statistics": prereg["statistics"], "seed_offsets": prereg["seed_offsets"],
        "conditions": cond, "arms": prereg["arms"], "heuristics": prereg["heuristics"],
        "lightgbm": prereg["lightgbm"]}}

    d["models"] = {}
    for arm in prereg["arms"]:
        rows = [json.loads((store.dir / f"models/{arm}_s{s}.json").read_text()) for s in seeds
                if f"model:{arm}:s{s}" in units]
        if rows:
            d["models"][arm] = rows

    # E1 격자 (E3·E6·E7의 비교도 이 칸들에 들어 있다)
    cells: dict = {}
    for unit, traffics in (("e1_base", ["orig", "pop0"]), *[(f"e1_{t}", [t]) for t in cond["subsample_fractions"]]):
        if unit not in units:
            continue
        arrays, um = store.load_arrays(unit)
        for traffic in traffics:
            for pool in pools:
                prefix = f"{traffic}|{pool}|"
                methods = _methods_in(arrays, prefix)
                if not methods or um["n_requests"] == 0:
                    continue
                bank = _bank(arrays, prefix, methods, seeds, n_boot)
                cells[f"{traffic}|{pool}"] = {
                    "n_requests": um["n_requests"], "n_users": um["n_users"], "methods": _summaries(bank),
                    "diffs": {f"{a}-vs-{b}": bank.diff(a, b) for a, b in _pairs_for_cell(methods)},
                    "unit_meta": {k: v for k, v in um.items() if k not in ("traffic",)}}
    d["e1"] = {"cells": cells}

    # E2 P2 곡선
    p2: dict = {}
    grid = [_k_label(k) for k in list(cond["truncate_ks"]) + [None]]
    k0 = store.load_arrays("e2p2_k0")[0] if "e2p2_k0" in units else None
    r2 = prereg["e2"]
    for k in grid:
        unit = f"e2p2_k{k}"
        if unit not in units:
            continue
        arrays, um = store.load_arrays(unit)
        for traffic in ("orig", "pop0"):
            prefix = f"{traffic}|"
            methods = _methods_in(arrays, prefix)
            bank = _bank(arrays, prefix, methods, seeds, n_boot)
            pairs = [(r2["model"], r2["baseline"]), (r2["model"], "recency"), ("poolneg_mask0", "recency"),
                     ("poolneg_mask0", r2["model"]), (r2["model"], "heuristic_cold")]
            cell = {"k": k, "truncated_share": um["truncated_share"], "n_requests": um["n_requests"],
                    "methods": _summaries(bank),
                    "diffs": {f"{a}-vs-{b}": bank.diff(a, b) for a, b in pairs if a in methods and b in methods}}
            if k0 is not None and k != "0":
                zero = _bank(k0, prefix, [r2["model"]], seeds, n_boot)
                both = MetricBank(arrays["clusters"], n_boot, metrics=P2_METRICS)
                both.runs = {"k": bank.runs[r2["model"]], "k0": zero.runs[r2["model"]]}
                cell["gain_vs_k0"] = both.diff("k", "k0")
            p2[f"{traffic}|k{k}"] = cell
    d["e2"] = {"p2": p2}
    if "e2p2_kall" in units and "e1_base" in units:
        a, _ = store.load_arrays("e2p2_kall")
        b, _ = store.load_arrays("e1_base")
        d["e2"]["consistency_with_e1"] = {
            "what": "절단 없음(k=all)의 poolneg 요청별 nDCG@10과 E1 원본·전체 풀 칸의 같은 값의 최대 절대 차이",
            "max_abs_diff": float(max(np.nanmax(np.abs(a[f"orig|poolneg|{s}"][0] - b[f"orig|full|poolneg|{s}"][0]))
                                      for s in seeds))}
    p1: dict = {}
    rp1 = r2["p1_descriptive"]
    for k in grid:
        unit = f"e2p1_k{k}"
        if unit not in units:
            continue
        arrays, um = store.load_arrays(unit)
        methods = _methods_in(arrays, "")
        bank = _bank(arrays, "", methods, seeds, n_boot, metrics=P1_CURVE_METRICS)
        p1[f"k{k}"] = {"k": k, "truncated_share": um["truncated_share"], "n_requests": um["n_requests"],
                       "methods": _summaries(bank),
                       "diffs": {f"{rp1['model']}-vs-{rp1['baseline']}": bank.diff(rp1["model"], rp1["baseline"])}}
    if p1:
        d["e2"]["p1"] = p1

    # E4 버킷 표
    if "e4_p3" in units:
        arrays, um = store.load_arrays("e4_p3")
        edges = um["bucket_edges_h"]
        labels = [f"{edges[i]}–{edges[i + 1]}h" for i in range(len(edges) - 1)] + [f"{edges[-1]}h+"]
        methods = _methods_in(arrays, "")
        buckets = {}
        for i, label in enumerate(labels):
            sel = np.flatnonzero(arrays["bucket"] == i)
            row = {"n_requests": int(len(sel)), "n_users": int(len(np.unique(arrays["clusters"][sel])))}
            if len(sel):
                bank = _bank(arrays, "", methods, seeds, n_boot, subset=sel)
                row["methods"] = {n: {"ndcg@10": bank.summary(n)["ndcg@10"]} for n in bank.runs}
            buckets[label] = row
        d["e4"] = {"buckets": buckets, "requests_without_positive_in_pool": int((arrays["bucket"] < 0).sum()),
                   "unit_meta": um, "verdict": "none (서술용)"}

    # E7 가중치
    weights: dict = {}
    for name in prereg["e7"]["sets"]:
        rows = {str(s): store.load_fit(name, s) for s in seeds if f"hfit:{name}:s{s}" in units}
        if rows:
            weights[name] = rows
    d["e7"] = {"weights": weights, "terms": prereg["heuristics"]["fit_terms"],
               "prior_weights": prereg["heuristics"]["prior_weights"]}

    # E8 후보 구성
    e8: dict = {}
    for name in ("serving", "harness"):
        unit = f"e8_{name}"
        if unit not in units:
            continue
        arrays, um = store.load_arrays(unit)
        recall = arrays["union_recall"].astype(np.float64)
        out = {"union_recall": float(np.nanmean(recall)), "config": um["config"], "union": um["union"],
               "n_requests": um["n_requests"], "pool_size_mean": um["pool_size_mean"],
               "recall_upper_bound": um["recall_upper_bound"], "judged_model": um["judged_model"]}
        for arm in [um["judged_model"]] + um["descriptive_models"]:
            bank = MetricBank(arrays["clusters"], n_boot, metrics=P2_METRICS)
            for label in ("full_pool", "two_stage"):
                for s in seeds:
                    a = arrays[f"{label}|{arm}|{s}"]
                    bank.add(label, {m: a[i].astype(np.float64) for i, m in enumerate(P2_METRICS)})
            block = {"full_pool": bank.summary("full_pool"), "two_stage": bank.summary("two_stage"),
                     "paired_vs_full_pool": bank.diff("two_stage", "full_pool")}
            if arm == um["judged_model"]:
                out["two_stage"] = block
            else:
                out.setdefault("descriptive", {})[arm] = block
        e8[name] = out
    d["e8"] = e8

    stage_units = {
        "fit": [f"model:{a}:s{s}" for a in prereg["arms"] for s in seeds] + [f"hfit:{n}:s{s}" for n in prereg["e7"]["sets"] for s in seeds],
        "e1": ["e1_base"] + [f"e1_{t}" for t in cond["subsample_fractions"]],
        "e2p2": [f"e2p2_k{k}" for k in grid], "e8": ["e8_serving", "e8_harness"], "e4": ["e4_p3"],
        "e2p1": [f"e2p1_k{k}" for k in grid]}
    d["stages"] = {st: {"units_done": sum(u in units for u in us), "units_total": len(us),
                        "seconds": round(sum(units[u]["seconds"] for u in us if u in units), 1)}
                   for st, us in stage_units.items()}
    v = cold_verdicts(d, prereg)
    d["verdicts"] = v
    d["unmeasured"] = {"rules": v["unmeasured"],
                       "stages": [st for st, s in d["stages"].items() if s["units_done"] < s["units_total"]]}
    return d


def stage_assemble(store: Store, prereg: dict, args, config: dict, bench_info: Optional[dict]):
    from .make_cold_report import render

    compute = None
    if (store.dir / "compute.json").exists():
        compute = json.loads((store.dir / "compute.json").read_text())
    meta = {
        "label": prereg["evidence_label"], "evidence": evidence_grade(config, args.n_boot, prereg),
        "preregistration": {"id": prereg["id"], "sha256": config["prereg_sha256"], "adr": prereg["adr"],
                            "commit": os.getenv("M4_PREREG_COMMIT") or prereg_commit()},
        "code_sha": config["code_sha"], "dataset": config["dataset"], "seeds": config["seeds"],
        "n_boot": args.n_boot, "p2_sample": config["p2_sample"], "sub_cap": config["sub_cap"],
        "fake_dim": config["fake_dim"], "max_fit": config["max_fit"], "max_test": config["max_test"],
        "environments": store.progress.get("environments", []), "assembled_on": environment(),
        "data_files": config["data_files"], "embeddings_sha256": config["embeddings_sha256"],
        # 기사 파일: 쓴 파일의 sha(잰 값), 등록한 원본 sha(yaml의 상수), 이 실행이 받은 원본 sha(manifest, 올린 쪽에서 잰 값)
        "articles_file_sha256": config["data_files"].get("articles.parquet"),
        "articles_original_sha256_registered": prereg["data"]["articles_original_sha256"],
        "articles_original_sha256_manifest": config.get("articles_original_sha256_manifest"),
        "articles_linked_to_registration": articles_linked_to_registration(config, prereg),
        "compute": compute, "config_hash": store.progress["config_hash"],
    }
    if bench_info:
        meta["data"] = bench_info
    d = assemble(store, prereg, config["seeds"], args.n_boot, meta)
    out_json = Path(args.out_json) if args.out_json else store.dir / REPORT_JSON
    out_md = Path(args.out_md) if args.out_md else store.dir / REPORT_MD
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=_json_default))
    out_md.write_text(render(d))
    log.info("wrote %s and %s (grade: %s, unmeasured: %s)", out_json, out_md, meta["evidence"]["grade"], d["unmeasured"])


# --- 진입점 ------------------------------------------------------------------------------------

STAGE_FUNCS = {"fit": stage_fit, "e1": stage_e1, "e2p2": stage_e2p2, "e8": stage_e8, "e4": stage_e4, "e2p1": stage_e2p1}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="EB-NeRD v1.2 콜드 regime 사슬 (ADR 0013 A2 사전 등록)")
    ap.add_argument("--dataset", default="ebnerd_small")
    ap.add_argument("--root", default=None)
    ap.add_argument("--emb-dir", default=None)
    ap.add_argument("--fake-dim", type=int, default=None, help="개발용 무작위 임베딩(결과 해석 금지)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--stage", required=True, help="fit|e1|e2p2|e8|e4|e2p1|assemble|all")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--p2-sample", type=int, default=20000)
    ap.add_argument("--sub-cap", type=int, default=20000)
    ap.add_argument("--max-fit", type=int, default=None, help="개발용 표본 상한(쓰면 demo 등급)")
    ap.add_argument("--max-test", type=int, default=None, help="개발용 표본 상한(쓰면 demo 등급)")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--resume", action="store_true", help="같은 설정의 완료된 단위를 건너뛰고 이어서 실행")
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--out-md", default=None)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    prereg = load_prereg()
    order = list(prereg["run"]["stages"])
    stages = order if args.stage == "all" else [args.stage]
    unknown = [s for s in stages if s not in order]
    if unknown:
        raise SystemExit(f"알 수 없는 단계: {unknown} (가능: {order + ['all']})")

    lgbm = prereg["lightgbm"]
    if (lgbm["num_boost_round"], lgbm["early_stopping"]) != (NUM_BOOST_ROUND, EARLY_STOPPING):
        raise SystemExit("사전 등록의 라운드 수·early stopping이 models.py 상수와 다릅니다")
    if stages == ["assemble"]:
        # 체크포인트만으로 리포트를 다시 만든다(데이터를 읽지 않는다). 설정은 실행 당시 기록을 쓴다.
        try:
            store = Store.open_existing(Path(args.out_dir))
        except ConfigMismatch as e:
            log.error("%s", e)
            return EXIT_CONFIG_MISMATCH
        stage_assemble(store, prereg, args, store.progress["config"], store.progress.get("bench_info"))
        return 0

    root = Path(args.root) if args.root else ebnerd_root()
    dataset_dir = root / args.dataset
    emb_dir = Path(args.emb_dir) if args.emb_dir else root / "derived" / args.dataset
    t0 = time.time()
    bench = load_bench(dataset_dir, emb_dir=None if args.fake_dim else emb_dir, fake_dim=args.fake_dim)
    W = protocol_windows(bench)
    log.info("bench loaded %.1fs windows=%s", time.time() - t0, {k: (_iso(a), _iso(b)) for k, (a, b) in W.items()})
    config = {
        "prereg_id": prereg["id"], "prereg_sha256": prereg_sha256(), "dataset": args.dataset,
        "seeds": list(args.seeds), "p2_sample": args.p2_sample, "sub_cap": args.sub_cap, "fake_dim": args.fake_dim,
        "max_fit": args.max_fit, "max_test": args.max_test, "data_files": bench.data_info["files"],
        "embeddings_sha256": bench.catalog_info.get("embeddings_sha256"),
        "code_sha": os.getenv("M4_CODE_SHA") or _git_sha(),
        "articles_original_sha256_manifest": os.getenv(ARTICLES_ORIGINAL_ENV) or None,
    }
    tr, va = bench.imps["train"], bench.imps["validation"]
    rng = np.random.default_rng(SPLIT_SEED)
    idx = {"fit": impressions_in(tr, W["fit"]), "es": impressions_in(tr, W["es"]), "test": impressions_in(va, W["test"])}
    for k, cap in (("fit", args.max_fit), ("es", args.max_fit), ("test", args.max_test)):
        if cap and len(idx[k]) > cap:
            idx[k] = np.sort(rng.choice(idx[k], size=cap, replace=False))
    info = {k: v for k, v in bench.data_info.items() if k != "files"}
    info["windows"] = {k: [_iso(a), _iso(b)] for k, (a, b) in W.items()}
    info["impressions"] = {k: int(len(v)) for k, v in idx.items()}
    info["catalog"] = bench.catalog_info
    try:
        store = Store(Path(args.out_dir), config, resume=args.resume, bench_info=info, env=environment(args.threads))
    except ConfigMismatch as e:
        log.error("%s", e)
        return EXIT_CONFIG_MISMATCH

    # 등급은 몇 시간 뒤 조립할 때가 아니라 시작할 때 알 수 있다: 판정용으로 돌린 실행이 demo로 끝나는 것을 로그에서 먼저 본다
    grade = evidence_grade(store.progress["config"], args.n_boot, prereg)
    log.info("evidence grade: %s%s", grade["grade"], " — " + "; ".join(grade["reasons"]) if grade["reasons"] else "")

    run = Run(args=args, prereg=prereg, bench=bench, windows=W, idx=idx, store=store, seeds=list(args.seeds))
    for stage in stages:
        t0 = time.time()
        if stage == "assemble":
            stage_assemble(store, prereg, args, store.progress["config"], store.progress.get("bench_info"))
        else:
            try:
                STAGE_FUNCS[stage](run)
            except ReproductionGateFailed as e:
                log.error("%s", e)
                return EXIT_GATE_FAILED
        log.info("stage %s finished %.0fs", stage, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
