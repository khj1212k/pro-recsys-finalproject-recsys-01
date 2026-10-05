"""E15의 튜닝: 같은 예산의 무작위 탐색과 튠한 LightGBM 학습 (ADR 0013 A3.6). torch를 쓰지 않는다.

탐색 공간·trial 수·시드는 preregistration/neural-e15.yaml에서만 읽는다. 공정성 규칙은 하나다 — 과제마다 GBDT arm 하나가
받는 trial 수는 신경망 family들이 받는 trial 수의 합과 같아야 하고, 다르면 실행을 시작하지 않는다.
"""
from __future__ import annotations

import math
import time
from typing import Callable, Mapping, Optional, Sequence

import lightgbm as lgb
import numpy as np
import pandas as pd

from ..models import EARLY_STOPPING, NUM_BOOST_ROUND, TEAM_PARAMS, ModelSpec, TrainedModel, _order_and_groups, feature_matrix
from ..prepare import RankTask


class BudgetMismatch(ValueError):
    pass


def check_budget(prereg: Mapping, neural_trials: Optional[int] = None, gbdt_trials: Optional[int] = None) -> dict:
    """GBDT trial 수 = 신경망 family 수 × family당 trial 수인지 본다. 인자를 주면 그 값으로(실행 인자 검사)."""
    n = int(prereg["run"]["neural_trials"] if neural_trials is None else neural_trials)
    g = int(prereg["run"]["gbdt_trials"] if gbdt_trials is None else gbdt_trials)
    families = list(prereg["neural"]["families"])
    if n < 1 or g != n * len(families):
        raise BudgetMismatch(f"튜닝 예산이 같지 않습니다: GBDT {g} trial vs 신경망 {n} × {len(families)} family = "
                             f"{n * len(families)} trial")
    return {"gbdt_per_arm": g, "neural_per_family": n, "neural_total": n * len(families), "families": families}


def _draw(rule: Mapping, rng: np.random.Generator):
    if "choice" in rule:
        return rule["choice"][int(rng.integers(0, len(rule["choice"])))]
    if "log_uniform" in rule:
        lo, hi = rule["log_uniform"]
        return float(math.exp(rng.uniform(math.log(lo), math.log(hi))))
    raise ValueError(f"알 수 없는 탐색 규칙: {dict(rule)}")


def sample_configs(space: Mapping, n_trials: int, seed: int, trial0: Optional[Mapping] = None) -> list[dict]:
    """설정 n_trials개. trial0을 주면 그것이 0번이고 나머지를 뽑는다. 키는 space에 적힌 순서대로 난수를 쓴다."""
    rng = np.random.default_rng(seed)
    configs = [dict(trial0)] if trial0 is not None else []
    while len(configs) < n_trials:
        configs.append({k: _draw(rule, rng) for k, rule in space.items()})
    return configs[:n_trials]


def in_space(config: Mapping, space: Mapping) -> bool:
    for k, rule in space.items():
        v = config.get(k)
        if "choice" in rule and v not in rule["choice"]:
            return False
        if "log_uniform" in rule and not (rule["log_uniform"][0] <= v <= rule["log_uniform"][1]):
            return False
    return set(config) == set(space)


def gbdt_configs(prereg: Mapping, n_trials: Optional[int] = None) -> list[dict]:
    """A*·A+가 함께 쓰는 설정 목록(0번 = 팀 설정)."""
    g = prereg["gbdt"]
    seed = int(prereg["statistics"]["split_seed"]) + int(prereg["seed_offsets"]["tune_gbdt"])
    return sample_configs(g["space"], int(n_trials or prereg["run"]["gbdt_trials"]), seed, trial0=g["trial0"])


def neural_space(prereg: Mapping, family: str) -> dict:
    space = dict(prereg["neural"]["space"])
    if family == "sasrec":
        space.update(prereg["neural"]["sasrec_space"])
    return space


def neural_configs(prereg: Mapping, family: str, n_trials: Optional[int] = None) -> list[dict]:
    seed = int(prereg["statistics"]["split_seed"]) + int(prereg["seed_offsets"][f"tune_{family}"])
    return sample_configs(neural_space(prereg, family), int(n_trials or prereg["run"]["neural_trials"]), seed)


def select_best(trials: Sequence[Mapping], key: str = "selection_metric") -> int:
    """선택 지표가 가장 큰 trial의 번호. 동률이면 번호가 작은 쪽. NaN인 trial은 고르지 않는다."""
    best, best_v = None, -math.inf
    for t in sorted(trials, key=lambda t: t["trial"]):
        v = t.get(key)
        if v is not None and not math.isnan(v) and v > best_v:
            best, best_v = int(t["trial"]), float(v)
    if best is None:
        raise ValueError("선택할 수 있는 trial이 없습니다")
    return best


def select_family(best_metric: Mapping[str, float], order: Sequence[str]) -> str:
    """각 family의 튠 best 지표가 큰 쪽. 동률이면 order의 앞쪽."""
    missing = [f for f in order if f not in best_metric]
    if missing:
        raise ValueError(f"튠 결과가 없는 family: {missing}")
    return max(order, key=lambda f: (best_metric[f], -list(order).index(f)))


def random_search(configs: Sequence[Mapping], evaluate: Callable[[int, Mapping], Mapping],
                  done: Optional[Mapping[int, Mapping]] = None,
                  on_trial: Optional[Callable[[Mapping], None]] = None) -> list[dict]:
    """설정마다 evaluate(trial, config) -> {"selection_metric", ...}를 불러 trial 행을 모은다.

    done에 이미 끝난 trial 행이 있으면 다시 돌리지 않는다(재개). 신경망과 LightGBM이 같은 함수를 쓴다.
    """
    rows = []
    for i, cfg in enumerate(configs):
        if done and i in done:
            rows.append(dict(done[i]))
            continue
        t0 = time.time()
        row = {"trial": i, "config": dict(cfg), **evaluate(i, cfg), "seconds": round(time.time() - t0, 1)}
        rows.append(row)
        if on_trial:
            on_trial(row)
    return rows


# --- 튠한 LightGBM ----------------------------------------------------------------------------

def gbdt_params(prereg: Mapping, hyper: Mapping, seed: int, num_threads: int) -> dict:
    """팀 설정 위에 trial 설정과 등록한 고정 옵션을 얹은 LambdaRank 파라미터. 목적함수·지표는 A와 같다."""
    g = prereg["gbdt"]
    p = dict(TEAM_PARAMS)
    p.update(g["fixed"])
    p.update(hyper)
    p.update(seed=seed, num_threads=num_threads, objective="lambdarank", metric="ndcg", eval_at=[10], label_gain=[0, 1])
    return p


def train_gbdt(spec: ModelSpec, fit: tuple[RankTask, pd.DataFrame], es: tuple[RankTask, pd.DataFrame], seed: int,
               params: Mapping, fit_rows: Optional[np.ndarray] = None) -> TrainedModel:
    """`models.train`과 같은 데이터 구성(행 순서, 쿼리, early stopping)으로 주어진 파라미터의 LambdaRank를 학습한다.

    `models.train`은 팀 파라미터를 덮어쓰지 못하게 막으므로 튠한 설정은 이 함수로 돌린다. trial 0(팀 설정)에서 두 함수의
    예측이 같은지는 테스트가 묶는다. fit_rows(후보 쌍 bool)가 있으면 그 행만 학습에 쓴다(스태커의 b2..b4).
    """
    if spec.objective != "lambdarank" or spec.group != "request":
        raise ValueError("튠 경로는 요청 단위 LambdaRank만 지원합니다")
    t0 = time.time()
    rng = np.random.default_rng(seed)
    sets = []
    for i, (task, feats) in enumerate((fit, es)):
        order, groups = _order_and_groups(task, spec.group, rng)
        x = feature_matrix(feats, task, spec.features)
        y = task.labels.astype(np.float32)
        if i == 0 and fit_rows is not None:
            keep = np.asarray(fit_rows, dtype=bool)
            order = order[keep[order]]
            groups = np.bincount(task.req.pair_req[keep], minlength=task.req.n)
            groups = groups[groups > 0]
        sets.append((x[order], y[order], groups))
    (xf, yf, gf), (xe, ye, ge) = sets
    dfit = lgb.Dataset(xf, yf, group=gf, feature_name=list(spec.features), free_raw_data=True)
    des = lgb.Dataset(xe, ye, group=ge, reference=dfit, feature_name=list(spec.features))
    evals: dict = {}
    booster = lgb.train(dict(params), dfit, num_boost_round=NUM_BOOST_ROUND, valid_sets=[des], valid_names=["es"],
                        callbacks=[lgb.early_stopping(EARLY_STOPPING, first_metric_only=True, verbose=False),
                                   lgb.record_evaluation(evals)])
    best_it = int(booster.best_iteration) or NUM_BOOST_ROUND
    gain = booster.feature_importance("gain", iteration=best_it)
    total = float(gain.sum()) or 1.0
    return TrainedModel(spec=spec, seed=seed, booster=booster, best_iteration=best_it,
                        best_score=float(evals["es"]["ndcg@10"][best_it - 1]), fit_rows=len(yf), es_rows=len(ye),
                        seconds=time.time() - t0,
                        importance_gain={f: round(float(g) / total, 4) for f, g in zip(spec.features, gain)})
