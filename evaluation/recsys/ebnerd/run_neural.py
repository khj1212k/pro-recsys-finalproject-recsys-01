"""EB-NeRD E15 실행기: 신경망 사용자 모델(NRMS-lite, SASRec-lite, 스태킹) vs 같은 예산으로 튠한 LightGBM.

    python -m evaluation.recsys.ebnerd.run_neural --dataset ebnerd_small --root <EBNERD_ROOT> --out-dir <out> \
        --task p1 --stage gate --resume
    python -m evaluation.recsys.ebnerd.run_neural --stage assemble --out-dir <out>        # 데이터 없이 체크포인트만으로

규칙은 ADR 0013 "A3 사전 등록"에 결과 전에 고정했고, 표본·시드·arm·탐색 공간·임계값은 preregistration/neural-e15.yaml에서만
읽는다. 과제(p1|p2)와 단계를 하나씩 받아 돌고, 단위(trial, seed별 모델, 콜드 조건 등)가 끝날 때마다 체크포인트를 쓴다.
같은 설정으로 다시 실행하면 끝난 단위를 건너뛴다. 신경망에서 나온 단위는 torch 버전과 장치가 같을 때만 건너뛴다.

두 학습기는 같은 RankTask(같은 후보·네거티브·라벨)와 같은 스칼라 행렬을 받는다. v1·v1.2 실행 경로의 파일은 고치지 않는다.
무거운 실행은 원격 런타임에서만 한다. 로그에는 집계 수치와 진행 상태만 쓴다(기사 텍스트를 읽지도 쓰지도 않는다).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import resource
import sys
import time
import warnings
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from recsys_core import compute_features

from ..metrics import cluster_bootstrap, paired_bootstrap_diff, ranking_metrics
from .loaders import ebnerd_root
from .models import ABLATION, ALL_GROUPS, NEGATIVE_VARIANTS, V2_FEATURES, ModelSpec, feature_matrix, train
from .neural import stack as stacking
from .neural.cold import cold_conditions, pop_mask_raw, truncate_logs
from .neural.datasets import NeuralInputs, build_inputs
from .neural.report import (DEMO_GRADE, EVIDENCE_GRADE, diff_key, load_prereg, neural_verdict, prereg_commit,
                            prereg_sha256, render)
from .neural.sequences import (ScalarSpec, Sequences, augment_features, aux_negative_table, cold_augment_plan,
                               keys_sha256, last_n_clicks, scalar_block, seq_scalars, standardization_stats,
                               truncate_sequences)
from .neural.tune import (BudgetMismatch, check_budget, gbdt_configs, gbdt_params, neural_configs, select_best,
                          select_family, train_gbdt)
from .prepare import (Bench, RankTask, filter_candidates, impressions_in, load_bench, p1_task, p2_task,
                      pool_negative_task, protocol_windows, seen_mask)
from .run_cold import (ConfigMismatch, Store, _anon_clusters, articles_linked_to_registration, environment)
from .run_ebnerd import SPLIT_SEED, _git_sha, _iso, _json_default

log = logging.getLogger("ebnerd.neural")

REPORT_JSON, REPORT_MD = "ebnerd_v1_3_neural.json", "ebnerd_v1_3_neural.md"
EXIT_CONFIG_MISMATCH, EXIT_GATE_FAILED, EXIT_DETERMINISM_FAILED, EXIT_USAGE = 3, 4, 5, 64
A, A_STAR, A_PLUS, D_ARM = "A", "A_star", "A_plus", "D"
P1_KS, P2_KS = (5, 10), (10,)
GRADE_KEYS = ("dataset", "seeds", "p2_sample", "p2_cold_sample", "p2_select_sample", "neural_trials", "gbdt_trials",
              "tune_max_epochs", "final_max_epochs", "patience", "fake_dim", "max_fit", "max_test")
ENV_CODE_SHA, ENV_ARTICLES, ENV_PREREG_COMMIT = "E15_CODE_SHA", "E15_ARTICLES_ORIGINAL_SHA256", "E15_PREREG_COMMIT"
# 설계 사양이 서술용으로 든 것 중 이 코드가 내지 않는 항목(사전 등록 A3.7). 리포트에 미측정으로 남는다.
NOT_IMPLEMENTED = ("p1_p2_cross", "p2_ild_coverage_novelty", "user_x_seed_hierarchical_bootstrap",
                   "positive_filter_pool_unseen_sensitivity", "title_subtitle_embedding_sensitivity", "onnx_microbench")


class ReproductionGateFailed(RuntimeError):
    pass


class DeterminismGateFailed(RuntimeError):
    pass


# --- 체크포인트 --------------------------------------------------------------------------------

class NeuralStore(Store):
    def read_json(self, rel: str) -> dict:
        return json.loads((self.dir / rel).read_text())

    def json_unit(self, unit: str, obj: dict, seconds: float = 0.0):
        self.write_json(f"units/{unit}.json", obj)
        self._mark(unit, [f"units/{unit}.json"], seconds)

    def unit_json(self, unit: str) -> Optional[dict]:
        return self.read_json(f"units/{unit}.json") if self.done(unit) else None

    def drop_units(self, pred: Callable[[str], bool]) -> list[str]:
        gone = [u for u in self.progress["units"] if pred(u)]
        for u in gone:
            del self.progress["units"][u]
        if gone:
            self._flush()
        return gone


def neural_unit(task: str, name: str) -> str:
    return f"{task}__n__{name}"


def is_neural_derived(task: str, unit: str) -> bool:
    return unit.startswith(f"{task}__n__") or unit.startswith(f"model:{task}_n_")


# --- 평가 집합 ---------------------------------------------------------------------------------

@dataclasses.dataclass
class EvalSet:
    kind: str                        # "p1" | "p2"
    task: RankTask
    feats: pd.DataFrame              # V2 피처 + 시퀀스 스칼라 열
    ctx: object
    seq: Sequences
    metrics: tuple
    keep: Optional[np.ndarray] = None      # P1: seen이 아닌 후보(쌍 bool)
    fptr: Optional[np.ndarray] = None

    @property
    def clusters(self) -> np.ndarray:
        return _anon_clusters(self.task.group_user)

    def _stack(self, m: dict) -> np.ndarray:
        return np.stack([m[k] for k in self.metrics]).astype(np.float32)

    def evaluate(self, scores: np.ndarray, seed: int, judged_only: bool = False) -> dict[str, np.ndarray]:
        """요청별 지표 [지표, 요청]. judged = 판정 표본(P1은 seen 제외판), seen_incl = P1 seen 포함판(서술용)."""
        t = self.task
        if self.kind == "p2":
            m = ranking_metrics(scores, t.labels, t.req.cand_ptr, ks=P2_KS, n_pos_total=t.n_pos_total, seed=seed,
                                with_auc=False)
            return {"judged": self._stack(m)}
        out = {"judged": self._stack(ranking_metrics(scores[self.keep], t.labels[self.keep], self.fptr, ks=P1_KS, seed=seed))}
        if not judged_only:
            out["seen_incl"] = self._stack(ranking_metrics(scores, t.labels, t.req.cand_ptr, ks=P1_KS, seed=seed))
        return out

    def selection_metric(self, scores: np.ndarray, seed: int) -> float:
        return float(np.nanmean(self.evaluate(scores, seed, judged_only=True)["judged"][0]))


@dataclasses.dataclass
class RawSet:
    """신경망 입력의 재료: 과제, raw 스칼라 행렬(V2 열 순서), 마지막 N 클릭(N = 가장 긴 설정)."""
    task: RankTask
    x: np.ndarray
    seq: Sequences


# --- 실행 컨텍스트 -----------------------------------------------------------------------------

@dataclasses.dataclass
class Run:
    args: argparse.Namespace
    prereg: dict
    bench: Bench
    windows: dict
    idx: dict
    store: NeuralStore
    task: str
    _cache: dict = dataclasses.field(default_factory=dict)

    # 등록 값
    @property
    def seeds(self) -> list[int]:
        return list(self.args.seeds)

    @property
    def off(self) -> dict:
        return self.prereg["seed_offsets"]

    @property
    def split_seed(self) -> int:
        return int(self.prereg["statistics"]["split_seed"])

    @property
    def fixed(self) -> dict:
        return self.prereg["neural"]["fixed"]

    @property
    def scalar_spec(self) -> ScalarSpec:
        return ScalarSpec.from_prereg(self.prereg["neural"]["scalar_block"])

    @property
    def seq_cols(self) -> list[str]:
        return list(self.prereg["gbdt"]["seq_scalars"]["columns"])

    @property
    def seq_n(self) -> int:
        return max(int(self.prereg["gbdt"]["seq_scalars"]["n_hist"]), *self.prereg["neural"]["space"]["n_hist"]["choice"],
                   int(self.prereg["neural"]["determinism_gate"]["spec"]["n_hist"]))

    @property
    def stack_col(self) -> str:
        return self.prereg["gbdt"]["stack_feature"]

    @property
    def emb(self) -> np.ndarray:
        return self.bench.catalog.emb

    @property
    def n_categories(self) -> int:
        return int(self.bench.catalog.n_categories)

    @property
    def edges(self) -> np.ndarray:
        return stacking.block_edges(self.windows["fit"], self.prereg["windows"]["block_hours"])

    def spec(self, arm: str) -> ModelSpec:
        base = next(s for s in ABLATION if s.name == "ranker_v2") if self.task == "p1" else NEGATIVE_VARIANTS[0]
        feats = list(V2_FEATURES)
        if arm == A_PLUS:
            feats += self.seq_cols
        if arm == D_ARM:
            feats += [self.stack_col]
        return dataclasses.replace(base, name=f"{self.task}_{arm}", features=feats) if arm != A else base

    # 표본
    def p2_idx(self) -> np.ndarray:
        rng = np.random.default_rng(self.split_seed + self.off["p2_sample"])
        test = self.idx["test"]
        return np.sort(rng.choice(test, size=min(self.args.p2_sample, len(test)), replace=False))

    def sel_idx(self) -> np.ndarray:
        rng = np.random.default_rng(self.split_seed + self.off["p2_select_sample"])
        es = self.idx["es"]
        return np.sort(rng.choice(es, size=min(self.args.p2_select_sample, len(es)), replace=False))

    def cold_idx(self) -> np.ndarray:
        base = self.p2_idx()
        rest = np.setdiff1d(self.idx["test"], base)
        n = min(max(self.args.p2_cold_sample - len(base), 0), len(rest))
        extra = np.random.default_rng(self.split_seed + self.off["p2_cold_extra"]).choice(rest, size=n, replace=False)
        return np.sort(np.concatenate([base, extra]))

    # 피처
    def features(self, ctx, req, label: str) -> pd.DataFrame:
        t0 = time.time()
        f = compute_features(ctx, req, groups=ALL_GROUPS)
        log.info("features %s: %d requests, %d pairs, %.1fs", label, req.n, len(f), time.time() - t0)
        return f

    def with_seq(self, feats: pd.DataFrame, seq: Sequences, req) -> pd.DataFrame:
        cfg = self.prereg["gbdt"]["seq_scalars"]
        n = int(cfg["n_hist"])
        s = Sequences(seq.items[:, -n:], seq.mask[:, -n:], seq.pos[:, -n:])
        return pd.concat([feats, seq_scalars(s, req, self.emb, cfg["columns"], float(cfg["empty_value"]))], axis=1)

    def sequences(self, ctx, req) -> Sequences:
        return last_n_clicks(ctx.user_log, req.user, req.profile_cutoff, self.seq_n)

    def _one(self, key, build):
        """seed마다 달라지는 큰 덩어리는 하나만 들고 있는다(다음 seed로 넘어가면 앞의 것을 버린다)."""
        kind = key[0]
        if key not in self._cache:
            for k in [k for k in self._cache if k[0] == kind]:
                del self._cache[k]
            self._cache[key] = build()
        return self._cache[key]

    def raw_train(self, seed: int) -> dict:
        """v1과 같은 학습 데이터: fit·es 과제와 raw 피처(증강 없음)."""
        def build():
            ctx = self.bench.ctx["train"]
            if self.task == "p1":
                fit, es = p1_task(self.bench, "train", self.idx["fit"]), p1_task(self.bench, "train", self.idx["es"])
            else:
                neg = self.prereg["tasks"]["p2"]["fit_negatives"]
                rng = np.random.default_rng(self.off["pool_negatives"] + seed)       # fit -> es 순서(v1과 같은 난수 소비)
                fit = pool_negative_task(self.bench, "train", self.idx["fit"], rng, n_neg=neg["pool_random"],
                                         window_h=neg["window_h"])
                es = pool_negative_task(self.bench, "train", self.idx["es"], rng, n_neg=neg["pool_random"],
                                        window_h=neg["window_h"])
            return {"fit": (fit, self.features(ctx, fit.req, f"{self.task}-fit-s{seed}")),
                    "es": (es, self.features(ctx, es.req, f"{self.task}-es-s{seed}")), "ctx": ctx}
        return self._one(("raw", 0 if self.task == "p1" else seed), build)

    def aug_train(self, seed: int) -> dict:
        """A를 뺀 모든 arm의 학습 데이터: 콜드 증강한 fit, 조기 종료 행(P1은 seen 제외판), 시퀀스, 시퀀스 스칼라 열."""
        def build():
            raw = self.raw_train(seed)
            ctx = raw["ctx"]
            (fit, fit_feats), (es, es_feats) = raw["fit"], raw["es"]
            cfg = self.prereg["cold_augment"]
            plan = cold_augment_plan(fit.req.n, cfg["fraction"], cfg["ks"], self.off["cold_augment"] + seed)
            t0 = time.time()
            fit_aug = augment_features(ctx, fit.req, fit_feats, plan)
            fit_seq = truncate_sequences(self.sequences(ctx, fit.req), plan)
            es_seq = self.sequences(ctx, es.req)
            fit_aug, es_plus = self.with_seq(fit_aug, fit_seq, fit.req), self.with_seq(es_feats, es_seq, es.req)
            log.info("augment %s seed=%d: %d/%d fit requests truncated, %.1fs", self.task, seed, int((plan >= 0).sum()),
                     fit.req.n, time.time() - t0)
            out = {"ctx": ctx, "plan": plan, "fit": (fit, fit_aug), "fit_seq": fit_seq, "es_all": (es, es_plus),
                   "es_seq": es_seq, "block": stacking.block_of(fit.req.time, self.edges)}
            if self.task == "p1":
                keep = ~seen_mask(self.bench, "train", es.req)
                out["es"] = (filter_candidates(es, keep), es_plus[keep].reset_index(drop=True))
                out["es_keep"] = keep
            else:
                out["es"] = (es, es_plus)
            return out
        return self._one(("aug", seed), build)

    def eval_set(self, name: str) -> EvalSet:
        """sel(선택 표본) | test(판정 표본) | cold(콜드 게이트 표본). 시퀀스 스칼라 열까지 붙여 둔다."""
        def build():
            metrics = tuple(self.prereg["tasks"][self.task]["metrics"])
            if self.task == "p1":
                split = "train" if name == "sel" else "validation"
                ctx = self.bench.ctx[split]
                if name == "sel":
                    task, feats = self.raw_train(self.seeds[0])["es"]
                else:
                    task = p1_task(self.bench, "validation", self.idx["test"])
                    feats = self.features(ctx, task.req, f"p1-{name}")
                seq = self.sequences(ctx, task.req)
                keep = ~seen_mask(self.bench, split, task.req)
                fptr = np.concatenate([[0], np.cumsum(np.bincount(task.req.pair_req[keep], minlength=task.req.n))])
                return EvalSet("p1", task, self.with_seq(feats, seq, task.req), ctx, seq, metrics, keep, fptr)
            split, idx = {"sel": ("train", self.sel_idx), "test": ("validation", self.p2_idx),
                          "cold": ("validation", self.cold_idx)}[name]
            ctx = self.bench.ctx[split]
            task = p2_task(self.bench, split, idx(), window_h=self.prereg["tasks"]["p2"]["fit_negatives"]["window_h"],
                           exclude_seen=True)
            seq = self.sequences(ctx, task.req)
            return EvalSet("p2", task, self.with_seq(self.features(ctx, task.req, f"p2-{name}"), seq, task.req), ctx, seq, metrics)
        key = ("eval", "test" if (self.task == "p1" and name == "cold") else name)
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    # 신경망 입력
    def aux_table(self, seed: int) -> np.ndarray:
        ctx = self.bench.ctx["train"]
        return self._one(("aux", seed), lambda: aux_negative_table(
            ctx.user_log, ctx.catalog.pub_time, int(self.fixed["aux_negatives"]), float(self.fixed["aux_window_h"]),
            self.off["aux_negatives"] + seed))

    def raw_set(self, task: RankTask, feats: pd.DataFrame, seq: Sequences) -> RawSet:
        return RawSet(task, feature_matrix(feats, task, V2_FEATURES), seq)

    def inputs(self, raw: RawSet, stats: dict, n_hist: int, aux: Optional[np.ndarray] = None) -> NeuralInputs:
        block = scalar_block(raw.x, V2_FEATURES, self.scalar_spec, stats, self.n_categories)
        seq = Sequences(raw.seq.items[:, -n_hist:], raw.seq.mask[:, -n_hist:], raw.seq.pos[:, -n_hist:])
        return build_inputs(raw.task, block, seq, aux)

    def neural_train(self, seed: int) -> dict:
        """신경망 학습 재료: 증강한 fit, es(전 후보), fit 통계, es 지표 함수(두 학습기가 같은 행에서 멈춘다)."""
        def build():
            d = self.aug_train(seed)
            fit = self.raw_set(d["fit"][0], d["fit"][1], d["fit_seq"])
            es_task, es_feats = d["es_all"]
            es = self.raw_set(es_task, es_feats, d["es_seq"])
            if self.task == "p1":
                keep = d["es_keep"]
                fptr = np.concatenate([[0], np.cumsum(np.bincount(es_task.req.pair_req[keep], minlength=es_task.req.n))])

                def es_metric(s, _seed=seed):
                    return float(np.nanmean(ranking_metrics(s[keep], es_task.labels[keep], fptr, ks=(10,), seed=_seed,
                                                            with_auc=False)["ndcg@10"]))
            else:
                def es_metric(s, _seed=seed):
                    return float(np.nanmean(ranking_metrics(s, es_task.labels, es_task.req.cand_ptr, ks=(10,), seed=_seed,
                                                            with_auc=False)["ndcg@10"]))
            return {"fit": fit, "es": es, "es_metric": es_metric, "block": d["block"],
                    "stats": standardization_stats(fit.x, V2_FEATURES, self.scalar_spec)}
        return self._one(("ntrain", seed), build)


# --- 공통 도구 ---------------------------------------------------------------------------------

def _torch():
    """torch가 필요한 단계에서만 임포트한다(GBDT 단계와 assemble은 torch 없이 돈다)."""
    from .neural import train as tr
    from .neural.models import NeuralSpec
    return tr, NeuralSpec


def _unit(run: Run, unit: str, fn: Callable[[], tuple[dict, dict]]) -> bool:
    if run.store.done(unit):
        log.info("skip %s (완료된 단위)", unit)
        return False
    t0 = time.time()
    arrays, meta = fn()
    run.store.save_arrays(unit, arrays, meta, time.time() - t0)
    log.info("unit %s done %.0fs", unit, time.time() - t0)
    return True


def _put(arrays: dict, ev: EvalSet, arm: str, seed: int, scores: np.ndarray, judged_only: bool = False):
    for kind, arr in ev.evaluate(scores, seed, judged_only=judged_only).items():
        arrays[f"{kind}|{arm}|{seed}"] = arr


def _seed_mean(arrays: dict, kind: str, arm: str, seeds: list[int]) -> Optional[np.ndarray]:
    runs = [arrays[f"{kind}|{arm}|{s}"] for s in seeds if f"{kind}|{arm}|{s}" in arrays]
    if not runs:
        return None
    with warnings.catch_warnings():       # 지표가 정의되지 않는 요청(정답이 seen으로 빠진 노출)은 모든 seed에서 NaN이다
        warnings.simplefilter("ignore", category=RuntimeWarning)
        return np.nanmean(np.stack(runs).astype(np.float64), axis=0)


def evidence_grade(config: dict, n_boot: int, prereg: dict, fit_blocks: Optional[int]) -> dict:
    """실행 인자·입력 파일이 사전 등록과 같은지. 다르면 demo 등급이고 판정에 쓰지 않는다."""
    reg, reasons = prereg["run"], []
    for key in GRADE_KEYS:
        if config.get(key) != reg[key]:
            reasons.append(f"{key}={config.get(key)!r} (등록값 {reg[key]!r})")
    if n_boot != reg["n_boot"]:
        reasons.append(f"n_boot={n_boot} (등록값 {reg['n_boot']})")
    files = config.get("data_files", {})
    for f, sha in prereg["data"]["files_sha256"].items():
        if files.get(f) != sha:
            reasons.append(f"{f} sha256이 등록값과 다름")
    if not articles_linked_to_registration(config, prereg):
        reasons.append("기사 파일을 등록한 원본에 잇지 못함")
    if config.get("embeddings_sha256") != prereg["data"]["embeddings_sha256"]:
        reasons.append("임베딩 sha256이 등록값과 다름")
    if not re.fullmatch(r"[0-9a-f]{40}", str(config.get("code_sha") or "unknown")):
        reasons.append(f"코드 SHA 확인 불가({config.get('code_sha')})")
    if config.get("prereg_sha256") != prereg_sha256():
        reasons.append("사전 등록 yaml의 sha256이 실행 당시와 다름")
    if fit_blocks != prereg["windows"]["fit_blocks"]:
        reasons.append(f"fit 블록 수 {fit_blocks} (등록값 {prereg['windows']['fit_blocks']})")
    return {"grade": DEMO_GRADE if reasons else EVIDENCE_GRADE, "reasons": reasons}


def _grade(run: Run) -> str:
    info = run.store.progress.get("bench_info") or {}
    return evidence_grade(run.store.progress["config"], run.args.n_boot, run.prereg, info.get("fit_blocks"))["grade"]


def _lgbm_extra(prereg: dict) -> dict:
    """A에 주는 실행 옵션: 등록한 고정 옵션 중 팀 파라미터에 없는 것(deterministic, force_col_wise)."""
    from .models import TEAM_PARAMS
    return {k: v for k, v in prereg["gbdt"]["fixed"].items() if k not in TEAM_PARAMS}


# --- gate: A 재학습과 재현 게이트 --------------------------------------------------------------

def gate_values(arrays: dict, task: str, seeds: list[int], prereg: dict) -> dict:
    """재현 게이트의 값과 판정. 값 = 요청별 nDCG@10을 seed 평균한 뒤 요청 평균(v1 리포트의 mean과 같은 식)."""
    g = prereg["reproduction_gate"][task]
    checks = {"judged": g["seen_filtered_within"], "seen_incl": g["seen_included_within"]} if task == "p1" \
        else {"judged": g["within"]}
    out, ok = {}, True
    for kind, (lo, hi) in checks.items():
        m = _seed_mean(arrays, kind, g["arm"], seeds)
        value = None if m is None else float(np.nanmean(m[0]))
        inside = value is not None and lo <= value <= hi
        out[kind] = {"value": value, "within": [lo, hi], "inside": inside}
        ok &= inside
    measured = all(v["value"] is not None for v in out.values()) and \
        all(f"judged|{g['arm']}|{s}" in arrays for s in seeds)
    return {"arm": g["arm"], "checks": out, "status": "unmeasured" if not measured else ("pass" if ok else "fail")}


def stage_gate(run: Run):
    T, tag = run.task, run.args.recheck_gate
    unit = f"{T}__gate" + (f"_recheck_{tag}" if tag else "")

    def compute():
        test = run.eval_set("test")
        arrays, models, hashes = {"clusters": test.clusters}, [], {}
        for seed in run.seeds:
            raw = run.raw_train(seed)
            m = train(run.spec(A), raw["fit"], raw["es"], seed=seed, num_threads=run.args.threads,
                      extra_params=_lgbm_extra(run.prereg))
            if not tag:
                run.store.save_model(f"{T}_{A}", seed, m, {}, m.seconds)
            _put(arrays, test, A, seed, m.predict(test.feats, test.task))
            models.append(m.summary())
            for part in ("fit", "es"):
                r = raw[part][0].req
                hashes[f"{part}|{seed}"] = keys_sha256(r.user, r.time, r.cand_item)
            log.info("gate %s seed=%d best_it=%d es=%.4f %.0fs", T, seed, m.best_iteration, m.best_score, m.seconds)
        t = test.task
        meta = {"models": models, "negative_hash": hashes, "n_requests": int(t.req.n), "n_pairs": int(len(t.labels)),
                "n_users": int(len(np.unique(t.group_user))), "sample_sha256": keys_sha256(t.imp_index)}
        if T == "p1":
            seen = ~test.keep
            meta["p1_seen"] = {"candidate_seen_fraction": float(seen.mean()),
                               "clicked_items_seen_fraction": float((t.labels & seen).sum() / max(t.labels.sum(), 1))}
        return arrays, meta

    _unit(run, unit, compute)
    arrays, _ = run.store.load_arrays(unit)
    gate = gate_values(arrays, T, run.seeds, run.prereg)
    grade = _grade(run)
    record = {**gate, "task": T, "evidence_grade": grade, "enforced": grade == EVIDENCE_GRADE, "recheck": tag}
    if tag and run.store.done(f"{T}__gate"):
        base, _ = run.store.load_arrays(f"{T}__gate")
        a, b = _seed_mean(arrays, "judged", A, run.seeds), _seed_mean(base, "judged", A, run.seeds)
        record["max_abs_diff_vs_first_session"] = float(np.nanmax(np.abs(a - b))) if a is not None and b is not None else None
    run.store.write_json(f"{unit}_result.json", record)
    log.info("reproduction gate %s%s: %s %s (enforced=%s)", T, f" recheck {tag}" if tag else "", gate["status"],
             {k: v["value"] for k, v in gate["checks"].items()}, record["enforced"])
    if record["enforced"] and gate["status"] == "fail":
        raise ReproductionGateFailed(f"재현 게이트 실패({T}): {gate['checks']}. 이 실행은 무효입니다(사전 등록 A3.2).")


# --- 선택 표본의 기록 --------------------------------------------------------------------------

def _sel_record(sel: EvalSet) -> dict:
    """trial 행에 남기는 선택 표본의 기록: 그 trial이 실제로 채점한 요청(노출 인덱스)의 해시와 수."""
    return {"selection_sample_sha256": keys_sha256(sel.task.imp_index), "selection_requests": int(sel.task.req.n)}


def selection_sample(rows: list[dict]) -> dict:
    """trial 행들이 채점한 선택 표본. 행마다 다르거나 기록이 없으면 선택하지 않는다(다른 표본의 지표는 비교할 수 없다)."""
    seen = {(r.get("selection_sample_sha256"), r.get("selection_requests")) for r in rows}
    if len(seen) != 1 or None in next(iter(seen)):
        raise RuntimeError(f"trial들이 같은 선택 표본에서 채점되지 않았습니다(기록 {len(seen)}종, 행 {len(rows)}개)")
    sha, n = next(iter(seen))
    return {"sha256": sha, "n_requests": int(n)}


# --- GBDT: A*·A+ 튠과 최종 ---------------------------------------------------------------------

def stage_gbdt_tune(run: Run):
    T = run.task
    cfgs = gbdt_configs(run.prereg, run.args.gbdt_trials)
    for arm in (A_STAR, A_PLUS):
        for i, cfg in enumerate(cfgs):
            unit = f"{T}__tune_{arm}_t{i:02d}"
            if run.store.done(unit):
                continue
            t0 = time.time()
            d, sel = run.aug_train(run.seeds[0]), run.eval_set("sel")
            m = train_gbdt(run.spec(arm), d["fit"], d["es"], seed=run.seeds[0],
                           params=gbdt_params(run.prereg, cfg, run.seeds[0], run.args.threads))
            metric = sel.selection_metric(m.predict(sel.feats, sel.task), run.seeds[0])
            run.store.json_unit(unit, {"trial": i, "arm": arm, "config": cfg, "selection_metric": metric,
                                       "best_iteration": m.best_iteration, "best_es_score": m.best_score,
                                       **_sel_record(sel), "seconds": round(time.time() - t0, 1)}, time.time() - t0)
            log.info("tune %s %s trial %d/%d sel=%.4f best_it=%d", T, arm, i + 1, len(cfgs), metric, m.best_iteration)


def gbdt_trials(store: NeuralStore, task: str, arm: str, n: int) -> list[dict]:
    rows = [store.unit_json(f"{task}__tune_{arm}_t{i:02d}") for i in range(n)]
    if any(r is None for r in rows):
        raise RuntimeError(f"{task} {arm}의 튠이 끝나지 않았습니다({sum(r is not None for r in rows)}/{n} trial)")
    return rows


def best_gbdt(run: Run, arm: str) -> dict:
    rows = gbdt_trials(run.store, run.task, arm, run.args.gbdt_trials)
    selection_sample(rows)
    return rows[select_best(rows)]


def _gbdt_model(run: Run, name: str, seed: int, build: Callable):
    """저장된 모델이 있으면 읽고 없으면 학습해 저장한다(seed 하나가 끝날 때마다 체크포인트)."""
    if run.store.done(f"model:{name}:s{seed}"):
        return run.store.load_model(name, seed)
    m, extra = build()
    run.store.save_model(name, seed, m, extra, m.seconds)
    log.info("trained %s seed=%d best_it=%d es=%.4f %.0fs", name, seed, m.best_iteration, m.best_score, m.seconds)
    return run.store.load_model(name, seed)


def stage_gbdt_final(run: Run):
    T = run.task

    def compute():
        test = run.eval_set("test")
        arrays, best = {"clusters": test.clusters}, {arm: best_gbdt(run, arm) for arm in (A_STAR, A_PLUS)}
        for seed in run.seeds:
            for arm in (A_STAR, A_PLUS):
                def build(arm=arm, seed=seed):
                    d = run.aug_train(seed)
                    return train_gbdt(run.spec(arm), d["fit"], d["es"], seed=seed, params=gbdt_params(
                        run.prereg, best[arm]["config"], seed, run.args.threads)), {"config": best[arm]["config"]}
                _put(arrays, test, arm, seed, _gbdt_model(run, f"{T}_{arm}", seed, build).predict(test.feats, test.task))
        return arrays, {"best": best}

    _unit(run, f"{T}__test_gbdt", compute)


# --- 신경망 단계 -------------------------------------------------------------------------------

def ensure_neural_env(run: Run) -> dict:
    """신경망 단위는 torch 버전과 장치가 같을 때만 이어서 쓴다. 다르면 그 과제의 신경망 단위를 전부 버린다."""
    tr, _ = _torch()
    device = tr.resolve_device(run.args.device)
    if sys.platform != "darwin":      # macOS에서는 neural/train.py가 1스레드로 고정한다(OpenMP 런타임 충돌)
        tr.torch.set_num_threads(int(run.args.threads))
    fp = tr.device_fingerprint(device)
    rel = f"{run.task}__neural_env.json"
    prev = run.store.read_json(rel) if (run.store.dir / rel).exists() else None
    if prev and prev != fp:
        gone = run.store.drop_units(lambda u: is_neural_derived(run.task, u))
        log.warning("신경망 환경이 이전과 다릅니다(%s -> %s). %s의 신경망 단위 %d개를 버리고 처음부터 돕니다.", prev, fp,
                    run.task, len(gone))
    run.store.write_json(rel, fp)
    return {"device": device, "fingerprint": fp}


def _spec(run: Run, family: str, config: dict, use_scalars: bool = True):
    _, NeuralSpec = _torch()
    return NeuralSpec.from_config(family, config, use_scalars=use_scalars)


def stage_determinism(run: Run):
    T = run.task
    unit = neural_unit(T, "determinism")
    env = ensure_neural_env(run)
    if not run.store.done(unit):
        tr, _ = _torch()
        t0 = time.time()
        g = run.prereg["neural"]["determinism_gate"]
        seed = run.seeds[0]
        nt = run.neural_train(seed)
        n = nt["fit"].task.req.n
        rows = np.sort(np.random.default_rng(run.split_seed + run.off["determinism_subsample"]).choice(
            n, size=max(1, int(round(g["fit_fraction"] * n))), replace=False))
        results = {}
        for family in run.prereg["neural"]["families"]:
            spec = _spec(run, family, g["spec"])
            fit_in = run.inputs(nt["fit"], nt["stats"], spec.n_hist, run.aux_table(seed)).subset(rows)
            es_in = run.inputs(nt["es"], nt["stats"], spec.n_hist)
            results[family] = tr.determinism_gate(spec, fit_in, es_in, run.emb, device=env["device"], fixed=run.fixed,
                                                  n_categories=run.n_categories, epochs=int(g["epochs"]),
                                                  repeats=int(g["repeats"]), seed=seed)
            log.info("determinism gate %s %s: %s", T, family, results[family]["status"])
        status = "pass" if all(r["status"] == "pass" for r in results.values()) else "fail"
        run.store.json_unit(unit, {"status": status, "families": results, "environment": env["fingerprint"],
                                   "fit_requests": int(len(rows)), "evidence_grade": _grade(run)}, time.time() - t0)
    rec = run.store.unit_json(unit)
    if rec["status"] == "fail" and _grade(run) == EVIDENCE_GRADE:
        raise DeterminismGateFailed(f"결정론 게이트 실패({T}). 이 실행은 무효입니다(사전 등록 A3.9).")


def _fit_neural(run: Run, env: dict, spec, seed: int, max_epochs: int, rows: Optional[np.ndarray] = None,
                fixed_epochs: Optional[int] = None):
    """한 모델을 학습한다. rows가 있으면 그 요청만으로(블록 모델) 학습하고 통계도 그 행에서 계산한다."""
    tr, _ = _torch()
    nt = run.neural_train(seed)
    stats = nt["stats"]
    if rows is not None:
        pairs = np.repeat(np.isin(np.arange(nt["fit"].task.req.n), rows), nt["fit"].task.req.n_candidates)
        stats = standardization_stats(nt["fit"].x[pairs], V2_FEATURES, run.scalar_spec)
    aux = run.aux_table(seed) if spec.family == "sasrec" and spec.aux_lambda > 0 else None
    fit_in = run.inputs(nt["fit"], stats, spec.n_hist, aux)
    if rows is not None:
        fit_in = fit_in.subset(rows)
    kw = dict(seed=seed, device=env["device"], fixed=run.fixed, n_categories=run.n_categories, max_epochs=max_epochs)
    if fixed_epochs is None:
        t = tr.fit(spec, fit_in, run.emb, patience=int(run.args.patience), es_in=run.inputs(nt["es"], stats, spec.n_hist),
                   es_metric=nt["es_metric"], **kw)
    else:
        t = tr.fit(spec, fit_in, run.emb, fixed_epochs=fixed_epochs, **kw)
    return t, stats


def _training_record(model) -> dict:
    """(학습된 모델, 표준화 통계) -> 모델이 학습 입력에서 적어 둔 기록. 시간 전진 확인(stack.assert_trained_before)이 읽는다."""
    trained, stats = model
    return {"n_requests": trained.fit_groups or None, "max_time": trained.fit_time_max, "stats_pairs": stats.get("n_rows")}


def _score_neural(run: Run, env: dict, trained, stats: dict, task: RankTask, feats: pd.DataFrame, seq: Sequences) -> np.ndarray:
    tr, _ = _torch()
    return tr.predict(trained, run.inputs(run.raw_set(task, feats, seq), stats, trained.spec.n_hist), run.emb,
                      device=env["device"], fixed=run.fixed)


def stage_neural_tune(run: Run):
    T = run.task
    env = ensure_neural_env(run)
    seed = run.seeds[0]
    for family in run.prereg["neural"]["families"]:
        cfgs = neural_configs(run.prereg, family, run.args.neural_trials)
        for i, cfg in enumerate(cfgs):
            unit = neural_unit(T, f"tune_{family}_t{i:02d}")
            if run.store.done(unit):
                continue
            t0 = time.time()
            sel = run.eval_set("sel")
            t, stats = _fit_neural(run, env, _spec(run, family, cfg), seed, int(run.args.tune_max_epochs))
            metric = sel.selection_metric(_score_neural(run, env, t, stats, sel.task, sel.feats, sel.seq), seed)
            run.store.json_unit(unit, {"trial": i, "family": family, "config": cfg, "selection_metric": metric,
                                       **t.summary(), **_sel_record(sel), "seconds": round(time.time() - t0, 1)},
                                time.time() - t0)
            log.info("tune %s %s trial %d/%d sel=%.4f best_epoch=%d %.0fs", T, family, i + 1, len(cfgs), metric,
                     t.best_epoch, time.time() - t0)


def neural_trials(store: NeuralStore, task: str, family: str, n: int) -> list[dict]:
    rows = [store.unit_json(neural_unit(task, f"tune_{family}_t{i:02d}")) for i in range(n)]
    if any(r is None for r in rows):
        raise RuntimeError(f"{task} {family}의 튠이 끝나지 않았습니다({sum(r is not None for r in rows)}/{n} trial)")
    return rows


def stage_select(run: Run):
    T = run.task
    unit = neural_unit(T, "selection")
    if run.store.done(unit):
        return
    families = list(run.prereg["neural"]["families"])
    table, scored = {}, []
    for f in families:
        rows = neural_trials(run.store, T, f, run.args.neural_trials)
        scored += rows
        b = rows[select_best(rows)]
        table[f] = {"best_trial": b["trial"], "selection_metric": b["selection_metric"], "config": b["config"],
                    "best_epoch_in_tuning": b["best_epoch"]}
    # A*·A+의 trial도 같은 표본에서 채점됐어야 한다(선택 표본은 trial·family·A*·A+ 공통, A3.6)
    scored += [r for arm in (A_STAR, A_PLUS) for r in (run.store.unit_json(f"{T}__tune_{arm}_t{i:02d}")
                                                       for i in range(run.args.gbdt_trials)) if r]
    sample = selection_sample(scored)
    chosen = select_family({f: table[f]["selection_metric"] for f in families}, families)
    run.store.json_unit(unit, {"family": chosen, "table": table, "rule": run.prereg["selection"]["family_rule"],
                               "sample": run.prereg["tasks"][T]["selection_sample"], "sample_sha256": sample["sha256"],
                               "sample_requests": sample["n_requests"], "uses_test": False})
    log.info("selection %s: %s %s", T, chosen, {f: round(v["selection_metric"], 4) for f, v in table.items()})


def selection(run: Run) -> dict:
    s = run.store.unit_json(neural_unit(run.task, "selection"))
    if s is None:
        raise RuntimeError(f"{run.task}의 family 선택이 끝나지 않았습니다(select 단계)")
    return s


def _neural_model(run: Run, env: dict, name: str, spec, seed: int, **fit_kw):
    """저장된 신경망 모델(가중치 + 표준화 통계)을 읽거나 학습해 저장한다."""
    tr, _ = _torch()
    unit, rel = neural_unit(run.task, f"model_{name}_s{seed}"), f"models/{run.task}_n_{name}_s{seed}.pt"
    if not run.store.done(unit):
        t0 = time.time()
        t, stats = _fit_neural(run, env, spec, seed, int(run.args.final_max_epochs), **fit_kw)
        tr.save_trained(run.store.dir / rel, t, {"stats": stats})
        run.store.write_json(rel.replace(".pt", ".json"), t.summary())
        run.store._mark(unit, [rel, rel.replace(".pt", ".json")], time.time() - t0)
        log.info("trained %s %s seed=%d best_epoch=%d params=%d %.0fs", run.task, name, seed, t.best_epoch, t.n_params,
                 time.time() - t0)
    t, extra = tr.load_trained(run.store.dir / rel)
    return t, extra["stats"]


def _neural_arm(run: Run, env: dict, unit_name: str, arm: str, family: str, config: dict, use_scalars: bool = True):
    """한 신경망 arm의 최종 모델(seed마다)과 판정 표본 지표."""
    def compute():
        test = run.eval_set("test")
        arrays, models = {"clusters": test.clusters}, []
        for seed in run.seeds:
            t, stats = _neural_model(run, env, arm, _spec(run, family, config, use_scalars), seed)
            _put(arrays, test, arm, seed, _score_neural(run, env, t, stats, test.task, test.feats, test.seq))
            models.append(t.summary())
        return arrays, {"arm": arm, "family": family, "config": config, "use_scalars": use_scalars, "models": models,
                        "missing_indicator_columns": stats["missing"]}
    _unit(run, neural_unit(run.task, unit_name), compute)


def stage_neural_final(run: Run):
    env = ensure_neural_env(run)
    s = selection(run)
    _neural_arm(run, env, f"test_{s['family']}", s["family"], s["family"], s["table"][s["family"]]["config"])


def stage_stack(run: Run):
    T = run.task
    env = ensure_neural_env(run)
    s = selection(run)
    family, cfg = s["family"], s["table"][s["family"]]["config"]
    best_cfg = best_gbdt(run, A_STAR)["config"]
    spec = _spec(run, family, cfg)

    def compute():
        test = run.eval_set("test")
        arrays, audits, gains = {"clusters": test.clusters}, {}, {}
        for seed in run.seeds:
            final, stats = _neural_model(run, env, family, spec, seed)

            def build(seed=seed, final=final, stats=stats):
                d, nt = run.aug_train(seed), run.neural_train(seed)
                fit_task, fit_feats = d["fit"]

                def fit_block(rows):
                    return _fit_neural(run, env, spec, seed, final.best_epoch, rows=rows, fixed_epochs=final.best_epoch)

                def score_block(model, rows):
                    tr, _ = _torch()
                    return tr.predict(model[0], run.inputs(nt["fit"], model[1], spec.n_hist).subset(rows), run.emb,
                                      device=env["device"], fixed=run.fixed)

                chain = stacking.forward_chain_scores(fit_task.req.time, fit_task.req.cand_ptr, run.edges, fit_block,
                                                      score_block, _training_record)
                es_task, es_feats = d["es_all"]
                # es·test·콜드 행을 채점하는 최종 모델도 같은 기록으로 본다: fit 전체로 학습했고 es보다 이르다
                final_rec = stacking.assert_trained_before(
                    _training_record((final, stats)), fit_task.req.n, int(es_task.req.time.min()), "최종 sel 모델",
                    expected_pairs=int(len(fit_task.labels)))
                audits[seed] = {**stacking.assert_forward_only(chain, fit_task.req.time, fit_task.req.cand_ptr),
                                "block_models": len(chain.models), "block_model_records": chain.audit,
                                "final_model_record": final_rec, "epochs_per_block_model": final.best_epoch}
                es_scores = _score_neural(run, env, final, stats, es_task, es_feats, d["es_seq"])
                es_ranked = stacking.with_neural_rank(es_feats, es_scores, es_task.req.cand_ptr, run.stack_col)
                es_set = (es_task, es_ranked)
                if T == "p1":      # 순위는 전 후보에서 매기고, 조기 종료 행만 seen 제외판으로 거른다
                    es_set = (d["es"][0], es_ranked[d["es_keep"]].reset_index(drop=True))
                fit_set = (fit_task, stacking.with_neural_rank(fit_feats, chain.scores, fit_task.req.cand_ptr, run.stack_col))
                m = train_gbdt(run.spec(D_ARM), fit_set, es_set, seed=seed,
                               params=gbdt_params(run.prereg, best_cfg, seed, run.args.threads),
                               fit_rows=stacking.stacker_rows(chain, fit_task.req.cand_ptr))
                return m, {"config": best_cfg, "neural_family": family, "forward_chain": audits[seed]}

            stacker = _gbdt_model(run, f"{T}_n_{D_ARM}", seed, build)
            scores = _score_neural(run, env, final, stats, test.task, test.feats, test.seq)
            ranked = stacking.with_neural_rank(test.feats, scores, test.task.req.cand_ptr, run.stack_col)
            _put(arrays, test, D_ARM, seed, stacker.predict(ranked, test.task))
            gains[seed] = stacker.info.get("importance_gain", {}).get(run.stack_col)
            audits.setdefault(seed, stacker.info.get("forward_chain"))
        return arrays, {"neural_family": family, "a_star_config": best_cfg, "forward_chain": audits,
                        "neural_score_rank_gain_share": gains}

    _unit(run, neural_unit(T, f"test_{D_ARM}"), compute)


def stage_cold(run: Run):
    T = run.task
    env = ensure_neural_env(run)
    s = selection(run)
    family = s["family"]
    spec = _spec(run, family, s["table"][family]["config"])
    names = set(run.prereg["cold"]["conditions"])
    for cond in [c for c in cold_conditions() if c.name in names]:
        def compute(cond=cond):
            base = run.eval_set("cold")
            req = base.task.req
            if cond.pop_zero:
                feats, seq = pop_mask_raw(base.feats), base.seq
            else:
                ctx_k, req_k = truncate_logs(base.ctx, req, cond.truncate_k)
                seq = last_n_clicks(ctx_k.user_log, req_k.user, req_k.profile_cutoff, run.seq_n)
                feats = run.with_seq(run.features(ctx_k, req_k, f"{T}-cold-{cond.name}"), seq, req)
            arrays = {"clusters": base.clusters}
            for seed in run.seeds:
                for arm in (A_STAR, A_PLUS):
                    _put(arrays, base, arm, seed, run.store.load_model(f"{T}_{arm}", seed).predict(feats, base.task), True)
                final, stats = _neural_model(run, env, family, spec, seed)
                scores = _score_neural(run, env, final, stats, base.task, feats, seq)
                _put(arrays, base, family, seed, scores, True)
                ranked = stacking.with_neural_rank(feats, scores, req.cand_ptr, run.stack_col)
                _put(arrays, base, D_ARM, seed, run.store.load_model(f"{T}_n_{D_ARM}", seed).predict(ranked, base.task), True)
            # 두 학습기가 받은 스칼라 행렬은 이 한 벌이다(한 프로세스에서 한 번 계산해 모든 arm에 넣었다).
            return arrays, {"condition": cond.name, "n_requests": int(req.n), "n_pairs": int(len(base.task.labels)),
                            "scalar_matrix_sha256": keys_sha256(feature_matrix(feats, base.task, V2_FEATURES)),
                            "sequences_nonempty_share": float(seq.mask.any(axis=1).mean())}
        _unit(run, neural_unit(T, f"cold_{cond.name}"), compute)


def stage_narrative(run: Run):
    """서술용 arm. 단위 하나가 끝날 때마다 체크포인트를 쓰므로 예산으로 중간에 끊겨도 끝난 것은 남는다."""
    T = run.task
    env = ensure_neural_env(run)
    s = selection(run)
    best_cfg = best_gbdt(run, A_STAR)["config"]

    def gbdt_variant(arm: str, augment: bool, blocks: Optional[list] = None):
        def compute():
            test = run.eval_set("test")
            arrays = {"clusters": test.clusters}
            for seed in run.seeds:
                def build(seed=seed):
                    d = run.aug_train(seed)
                    fit = d["fit"] if augment else run.raw_train(seed)["fit"]
                    rows = stacking.blocks_mask(d["block"], fit[0].req.cand_ptr, blocks) if blocks else None
                    return train_gbdt(run.spec(A_STAR), fit, d["es"], seed=seed, fit_rows=rows, params=gbdt_params(
                        run.prereg, best_cfg, seed, run.args.threads)), {"config": best_cfg, "augment": augment, "blocks": blocks}
                _put(arrays, test, arm, seed, _gbdt_model(run, f"{T}_{arm}", seed, build).predict(test.feats, test.task))
            return arrays, {"arm": arm, "augment": augment, "fit_blocks": blocks}
        _unit(run, f"{T}__narr_{arm}", compute)

    gbdt_variant("A_star_noaug", augment=False)
    gbdt_variant("A_star_b234", augment=True, blocks=list(run.prereg["arms"]["A_star_b234"]["fit_blocks"]))
    sel_cfg = s["table"][s["family"]]["config"]
    _neural_arm(run, env, "narr_sel_no_scalars", "sel_no_scalars", s["family"], sel_cfg, use_scalars=False)
    for other in [f for f in run.prereg["neural"]["families"] if f != s["family"]]:
        _neural_arm(run, env, f"test_{other}", other, other, s["table"][other]["config"])


# --- assemble ----------------------------------------------------------------------------------

def _load(store: NeuralStore, unit: str) -> tuple[dict, dict]:
    return store.load_arrays(unit) if store.done(unit) else ({}, {})


def _summary(arrays: dict, kind: str, arm: str, seeds: list[int], metrics: list, n_boot: int, boot_seed: int) -> Optional[dict]:
    present = [s for s in seeds if f"{kind}|{arm}|{s}" in arrays]
    mean = _seed_mean(arrays, kind, arm, seeds)
    if mean is None:
        return None
    out = {"n_seeds": len(present), "seeds": present}
    for i, m in enumerate(metrics):
        ci = cluster_bootstrap(mean[i], arrays["clusters"], n_boot=n_boot, seed=boot_seed)
        out[m] = {"mean": ci["mean"], "ci95": [ci["lo"], ci["hi"]], "n": ci["n"], "n_users": ci["n_clusters"],
                  "seed_means": [float(np.nanmean(arrays[f"{kind}|{arm}|{s}"][i])) for s in present]}
    return out


def diff_entry(arrays: dict, kind: str, a: str, b: str, seeds: list[int], n_boot: int, stats: dict,
               full: bool = False) -> Optional[dict]:
    """같은 요청의 쌍체 차이(a − b, nDCG@10). full이면 seed별 Δ와 Holm 수준별 하한까지."""
    ma, mb = _seed_mean(arrays, kind, a, seeds), _seed_mean(arrays, kind, b, seeds)
    if ma is None or mb is None:
        return None
    clusters, seed = arrays["clusters"], stats["bootstrap_seed_paired"]

    def boot(x, y, alpha):
        return paired_bootstrap_diff(x, y, clusters, n_boot=n_boot, seed=seed, alpha=alpha)

    ci = boot(ma[0], mb[0], 1 - stats["ci_level"])
    gate = boot(ma[0], mb[0], 1 - stats["gate_ci_level"])
    out = {"diff": ci["mean"], "ci95": [ci["lo"], ci["hi"]], "ci90": [gate["lo"], gate["hi"]], "n": ci["n"]}
    if full:
        h = stats["holm"]
        out["holm_lo"] = {str(m): boot(ma[0], mb[0], h["alpha"] / m)["lo"] for m in range(1, h["family_size"] + 1)}
        out["per_seed"] = []
        for s in seeds:
            ka, kb = f"{kind}|{a}|{s}", f"{kind}|{b}|{s}"
            if ka in arrays and kb in arrays:
                c = boot(arrays[ka][0].astype(np.float64), arrays[kb][0].astype(np.float64), 1 - stats["ci_level"])
                out["per_seed"].append({"seed": s, "diff": c["mean"], "ci95": [c["lo"], c["hi"]]})
    return out


def assemble_task(store: NeuralStore, prereg: dict, task: str, seeds: list[int], n_boot: int) -> dict:
    stats = prereg["statistics"]
    metrics = list(prereg["tasks"][task]["metrics"])
    sel = store.unit_json(neural_unit(task, "selection"))
    families = list(prereg["neural"]["families"])
    units = [f"{task}__gate", f"{task}__test_gbdt", neural_unit(task, f"test_{D_ARM}"), f"{task}__narr_A_star_noaug",
             f"{task}__narr_A_star_b234", neural_unit(task, "narr_sel_no_scalars")] + \
            [neural_unit(task, f"test_{f}") for f in families]
    arrays, metas = {}, {}
    for u in units:
        arr, meta = _load(store, u)
        if arr:
            arrays.update(arr)
            metas[u] = meta
    out = {"arms": {}, "diffs": {}, "cold": {}, "units": {u: (u in metas) for u in units}, "unit_meta": metas}
    if "clusters" not in arrays:
        return out
    arms = sorted({k.split("|")[1] for k in arrays if k.startswith("judged|")})
    boot0 = stats["bootstrap_seed_mean"]
    out["arms"] = {a: _summary(arrays, "judged", a, seeds, metrics, n_boot, boot0) for a in arms}
    if task == "p1":
        out["arms_seen_included"] = {a: _summary(arrays, "seen_incl", a, seeds, metrics, n_boot, boot0) for a in arms
                                     if any(k.startswith(f"seen_incl|{a}|") for k in arrays)}
    chosen = sel["family"] if sel else None
    judged_refs = {(chosen, A_STAR), (D_ARM, A_STAR), (chosen, A_PLUS), (D_ARM, A_PLUS), (A_STAR, A), (A_PLUS, A_STAR)}
    judged_refs |= {(f, A_STAR) for f in families} | {(f, A_PLUS) for f in families}
    for a in arms:
        for b in (A_STAR, A, A_PLUS):
            if a != b and b in arms:
                e = diff_entry(arrays, "judged", a, b, seeds, n_boot, stats, full=(a, b) in judged_refs)
                if e:
                    out["diffs"][diff_key(a, b)] = e
    for cond in prereg["cold"]["conditions"]:
        arr, meta = _load(store, neural_unit(task, f"cold_{cond}"))
        if not arr:
            continue
        c_arms = sorted({k.split("|")[1] for k in arr if k.startswith("judged|")})
        block = {"meta": meta, "arms": {a: _summary(arr, "judged", a, seeds, metrics[:1], n_boot, boot0) for a in c_arms},
                 "diffs": {}}
        for a in c_arms:
            if a != prereg["cold"]["reference"]:
                e = diff_entry(arr, "judged", a, prereg["cold"]["reference"], seeds, n_boot, stats)
                if e:
                    block["diffs"][diff_key(a, prereg["cold"]["reference"])] = e
        out["cold"][cond] = block
    return out


def assemble(store: NeuralStore, prereg: dict, n_boot: int, meta: dict) -> dict:
    seeds = list(store.progress["config"]["seeds"])
    cfg = store.progress["config"]
    d = {"meta": meta, "protocol": {}, "validity": {}, "selection": {}, "trials": {}, "tasks": {}}
    for task in prereg["run"]["tasks"]:
        d["tasks"][task] = assemble_task(store, prereg, task, seeds, n_boot)
        gate_arr, gate_meta = _load(store, f"{task}__gate")
        v = {"reproduction": gate_values(gate_arr, task, seeds, prereg) if gate_arr else {"status": "unmeasured"},
             "determinism": {"status": "unmeasured"}}
        det = store.unit_json(neural_unit(task, "determinism"))
        if det:
            v["determinism"] = {"status": det["status"], "environment": det["environment"],
                                "families": {f: r["status"] for f, r in det["families"].items()}}
        v["rechecks"] = [json.loads(p.read_text()) for p in sorted(store.dir.glob(f"{task}__gate_recheck_*_result.json"))]
        if any(r["status"] == "fail" for r in v["rechecks"]):
            v["reproduction"] = {**v["reproduction"], "status": "fail", "reason": "다른 세션의 재확인에서 실패"}
        d["validity"][task] = v
        sel = store.unit_json(neural_unit(task, "selection"))
        if sel:
            d["selection"][task] = sel
        d["trials"][task] = {}
        for arm in (A_STAR, A_PLUS):
            d["trials"][task][arm] = [r for r in (store.unit_json(f"{task}__tune_{arm}_t{i:02d}")
                                                  for i in range(cfg["gbdt_trials"])) if r]
        for f in prereg["neural"]["families"]:
            d["trials"][task][f] = [r for r in (store.unit_json(neural_unit(task, f"tune_{f}_t{i:02d}"))
                                                for i in range(cfg["neural_trials"])) if r]
        env_file = store.dir / f"{task}__neural_env.json"
        # 선택 표본: trial 행들이 채점한 표본의 해시. 한 가지가 아니면(있을 수 없는 일이지만) 비워 두고 종류 수를 남긴다
        samples = sorted({(r.get("selection_sample_sha256"), r.get("selection_requests"))
                          for rows in d["trials"][task].values() for r in rows}, key=str)
        one = samples[0] if len(samples) == 1 else (None, None)
        d["protocol"][task] = {"gate": {k: v for k, v in gate_meta.items() if k != "models"},
                               "selection_sample_sha256": one[0], "selection_sample_requests": one[1],
                               "selection_sample_distinct": len(samples),
                               "neural_environment": json.loads(env_file.read_text()) if env_file.exists() else None,
                               "budget": check_budget(prereg, cfg["neural_trials"], cfg["gbdt_trials"])}
    d["verdict"] = neural_verdict(d, prereg)
    descriptive = [f"{t}:{u}" for t, b in d["tasks"].items() for u, done in b["units"].items() if not done]
    d["unmeasured"] = {"judged": d["verdict"]["unmeasured"], "units_not_run": descriptive,
                       "not_implemented_descriptive": list(NOT_IMPLEMENTED)}
    return d


def stage_assemble(store: NeuralStore, prereg: dict, args):
    config = store.progress["config"]
    info = store.progress.get("bench_info") or {}
    compute = json.loads((store.dir / "compute.json").read_text()) if (store.dir / "compute.json").exists() else None
    recorded, claimed = prereg_commit(), os.getenv(ENV_PREREG_COMMIT) or None
    meta = {
        "label": prereg["evidence_label"], "evidence": evidence_grade(config, args.n_boot, prereg, info.get("fit_blocks")),
        "preregistration": {"id": prereg["id"], "sha256": config["prereg_sha256"], "adr": prereg["adr"],
                            "commit": recorded or claimed, "commit_source": "file" if recorded else ("env" if claimed else None)},
        "code_sha": config["code_sha"], "n_boot": args.n_boot, **{k: config[k] for k in GRADE_KEYS},
        "environments": store.progress.get("environments", []), "assembled_on": environment(),
        "data_files": config["data_files"], "embeddings_sha256": config["embeddings_sha256"],
        "articles_file_sha256": config["data_files"].get("articles.parquet"),
        "articles_original_sha256_registered": prereg["data"]["articles_original_sha256"],
        "articles_original_sha256_manifest": config.get("articles_original_sha256_manifest"),
        "compute": compute, "config_hash": store.progress["config_hash"], "data": info,
        "stage_log": store.progress.get("stage_log", []),
    }
    d = assemble(store, prereg, args.n_boot, meta)
    out_json = Path(args.out_json) if args.out_json else store.dir / REPORT_JSON
    out_md = Path(args.out_md) if args.out_md else store.dir / REPORT_MD
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(d, indent=2, ensure_ascii=False, default=_json_default))
    out_md.write_text(render(d))
    log.info("wrote %s and %s (grade: %s, claim: %s, unmeasured judged: %s)", out_json, out_md, meta["evidence"]["grade"],
             d["verdict"]["claim"], d["verdict"]["unmeasured"])


# --- 진입점 ------------------------------------------------------------------------------------

STAGE_FUNCS = {"gate": stage_gate, "gbdt_tune": stage_gbdt_tune, "gbdt_final": stage_gbdt_final,
               "determinism": stage_determinism, "neural_tune": stage_neural_tune, "select": stage_select,
               "neural_final": stage_neural_final, "stack": stage_stack, "cold": stage_cold, "narrative": stage_narrative}


def build_parser() -> argparse.ArgumentParser:
    reg = load_prereg()["run"]
    ap = argparse.ArgumentParser(description="EB-NeRD E15 신경망 사용자 모델 비교 (ADR 0013 A3 사전 등록)")
    ap.add_argument("--dataset", default=reg["dataset"])
    ap.add_argument("--root", default=None)
    ap.add_argument("--emb-dir", default=None)
    ap.add_argument("--fake-dim", type=int, default=None, help="개발용 무작위 임베딩(결과 해석 금지)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--task", choices=reg["tasks"], default=None)
    ap.add_argument("--stage", required=True, help="|".join(reg["stages"]) + "|all")
    ap.add_argument("--seeds", type=int, nargs="+", default=list(reg["seeds"]))
    ap.add_argument("--n-boot", type=int, default=reg["n_boot"])
    ap.add_argument("--p2-sample", type=int, default=reg["p2_sample"])
    ap.add_argument("--p2-cold-sample", type=int, default=reg["p2_cold_sample"])
    ap.add_argument("--p2-select-sample", type=int, default=reg["p2_select_sample"])
    ap.add_argument("--neural-trials", type=int, default=reg["neural_trials"])
    ap.add_argument("--gbdt-trials", type=int, default=reg["gbdt_trials"])
    ap.add_argument("--tune-max-epochs", type=int, default=reg["tune_max_epochs"])
    ap.add_argument("--final-max-epochs", type=int, default=reg["final_max_epochs"])
    ap.add_argument("--patience", type=int, default=reg["patience"])
    ap.add_argument("--max-fit", type=int, default=None, help="개발용 표본 상한(쓰면 demo 등급)")
    ap.add_argument("--max-test", type=int, default=None, help="개발용 표본 상한(쓰면 demo 등급)")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--device", default="auto", help="auto|cpu|cuda")
    ap.add_argument("--resume", action="store_true", help="같은 설정의 완료된 단위를 건너뛰고 이어서 실행")
    ap.add_argument("--recheck-gate", default=None, metavar="TAG",
                    help="gate 단계를 이 환경에서 다시 돌려 따로 기록한다(다른 세션의 재확인)")
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
        log.error("알 수 없는 단계: %s (가능: %s)", unknown, order + ["all"])
        return EXIT_USAGE
    if stages == ["assemble"]:
        try:
            store = NeuralStore.open_existing(Path(args.out_dir))
        except ConfigMismatch as e:
            log.error("%s", e)
            return EXIT_CONFIG_MISMATCH
        stage_assemble(store, prereg, args)
        return 0
    if args.task is None:
        log.error("--task가 필요합니다(assemble만 과제 없이 돕니다)")
        return EXIT_USAGE
    try:
        check_budget(prereg, args.neural_trials, args.gbdt_trials)
    except BudgetMismatch as e:
        log.error("%s — 시작하지 않습니다.", e)
        return EXIT_USAGE

    root = Path(args.root) if args.root else ebnerd_root()
    dataset_dir = root / args.dataset
    emb_dir = Path(args.emb_dir) if args.emb_dir else root / "derived" / args.dataset
    t0 = time.time()
    bench = load_bench(dataset_dir, emb_dir=None if args.fake_dim else emb_dir, fake_dim=args.fake_dim)
    W = protocol_windows(bench)
    log.info("bench loaded %.1fs windows=%s", time.time() - t0, {k: (_iso(a), _iso(b)) for k, (a, b) in W.items()})
    config = {
        "prereg_id": prereg["id"], "prereg_sha256": prereg_sha256(), **{k: getattr(args, k) for k in GRADE_KEYS},
        "seeds": list(args.seeds), "data_files": bench.data_info["files"],
        "embeddings_sha256": bench.catalog_info.get("embeddings_sha256"),
        "code_sha": os.getenv(ENV_CODE_SHA) or _git_sha(),
        "articles_original_sha256_manifest": os.getenv(ENV_ARTICLES) or None,
    }
    tr, va = bench.imps["train"], bench.imps["validation"]
    rng = np.random.default_rng(SPLIT_SEED)
    idx = {"fit": impressions_in(tr, W["fit"]), "es": impressions_in(tr, W["es"]), "test": impressions_in(va, W["test"])}
    for k, cap in (("fit", args.max_fit), ("es", args.max_fit), ("test", args.max_test)):
        if cap and len(idx[k]) > cap:
            idx[k] = np.sort(rng.choice(idx[k], size=cap, replace=False))
    info = {k: v for k, v in bench.data_info.items() if k != "files"}
    info.update(windows={k: [_iso(a), _iso(b)] for k, (a, b) in W.items()}, catalog=bench.catalog_info,
                impressions={k: int(len(v)) for k, v in idx.items()},
                fit_blocks=int(len(stacking.block_edges(W["fit"], prereg["windows"]["block_hours"])) - 1))
    try:
        store = NeuralStore(Path(args.out_dir), config, resume=args.resume, bench_info=info, env=environment(args.threads))
    except ConfigMismatch as e:
        log.error("%s", e)
        return EXIT_CONFIG_MISMATCH
    grade = evidence_grade(store.progress["config"], args.n_boot, prereg, info["fit_blocks"])
    log.info("evidence grade: %s%s", grade["grade"], " — " + "; ".join(grade["reasons"]) if grade["reasons"] else "")

    run = Run(args=args, prereg=prereg, bench=bench, windows=W, idx=idx, store=store, task=args.task)
    for stage in stages:
        t0 = time.time()
        if stage == "assemble":
            stage_assemble(store, prereg, args)
        else:
            try:
                STAGE_FUNCS[stage](run)
            except ReproductionGateFailed as e:
                log.error("%s", e)
                return EXIT_GATE_FAILED
            except DeterminismGateFailed as e:
                log.error("%s", e)
                return EXIT_DETERMINISM_FAILED
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_gb = rss / (1 << 30) if sys.platform == "darwin" else rss / (1 << 20)
        store.progress.setdefault("stage_log", []).append({"task": args.task, "stage": stage, "recheck": args.recheck_gate,
                                                            "seconds": round(time.time() - t0, 1), "peak_rss_gb": round(peak_gb, 2)})
        store._flush()
        log.info("stage %s %s finished %.0fs (peak RSS %.2f GB)", args.task, stage, time.time() - t0, peak_gb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
