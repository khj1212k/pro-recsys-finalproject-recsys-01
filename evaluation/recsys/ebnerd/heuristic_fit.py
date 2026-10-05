"""활성 스코어러(4항 휴리스틱)의 사전 가중치 점수와 데이터 적합(E7).

서빙 `HeuristicScorer`(backend/app/recsys/scoring.py)는 0.45·cos_long + 0.35·cos_short + 0.15·exp(-age/48h) +
0.05·(인기 항)을 쓴다. 그 가중치는 합성 점검으로 고른 사전값이라 사람 클릭 데이터로 맞춰 본 적이 없다. 여기서는
(1) 사전값 점수를 EB-NeRD 피처로 그대로 계산하고, (2) 같은 4항의 가중치를 쌍별 조건부 로지스틱으로 적합한다.

쌍별 조건부 로지스틱: 요청 안의 (정답 p, 네거티브 n) 쌍마다 차이 벡터 d = x_p - x_n을 만들고 log(1 + exp(-w·d))를
최소화한다. 차이를 쓰므로 절편과 요청 수준 효과가 사라진다. 요청마다 쌍 가중치의 합이 1이 되게 해 후보가 많은 요청이
적합을 지배하지 않게 한다. 4차원이라 numpy Newton으로 충분하고 결과가 결정론적이다.
정의·상수는 ADR 0013 A2 사전 등록과 preregistration/cold-v1.2.yaml(heuristics, e7)에 있다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from recsys_core import expand_ranges, group_ids

TERMS = ("hist_cos", "short_cos", "recency", "log1p_pop_clicks_6h")


def heuristic_terms(feats: pd.DataFrame, tau_h: float = 48.0) -> np.ndarray:
    """[cos_long, cos_short, exp(-age/tau), log1p(pop_clicks_6h)] 행렬(행 = 후보)."""
    return np.column_stack([
        feats["hist_cos"].to_numpy(np.float64),
        feats["short_cos"].to_numpy(np.float64),
        np.exp(-feats["hours_since_pub"].to_numpy(np.float64) / tau_h),
        np.log1p(feats["pop_clicks_6h"].to_numpy(np.float64)),
    ])


def prior_scores(feats: pd.DataFrame, weights: Mapping[str, float], tau_h: float = 48.0,
                 with_popularity: bool = True) -> np.ndarray:
    """서빙 사전 가중치 점수. with_popularity=False면 인기 항을 뺀 heuristic_cold, True면 heuristic_prior4.

    서빙 코드와 같이 한쪽 신호가 없으면 그 가중치를 다른 쪽으로 넘긴다: 단기 이벤트가 없으면 단기 가중치를 장기로,
    (단기는 있는데) 장기 이벤트가 없으면 장기 가중치를 단기로. 인기 항은 서빙 식의 형태 min(1, log1p(x)/5)를 그대로
    쓰고 x에 pop_clicks_6h를 넣는다.
    """
    w_long = np.full(len(feats), float(weights["long_term"]))
    w_short = np.full(len(feats), float(weights["short_term"]))
    personal = float(weights["long_term"]) + float(weights["short_term"])
    no_short = feats["short_len"].to_numpy() <= 0
    no_long = (feats["hist_len"].to_numpy() <= 0) & ~no_short
    w_long[no_short], w_short[no_short] = personal, 0.0
    w_long[no_long], w_short[no_long] = 0.0, personal
    score = (w_long * feats["hist_cos"].to_numpy(np.float64) + w_short * feats["short_cos"].to_numpy(np.float64)
             + float(weights["recency"]) * np.exp(-feats["hours_since_pub"].to_numpy(np.float64) / tau_h))
    if with_popularity:
        pop = np.minimum(1.0, np.log1p(feats["pop_clicks_6h"].to_numpy(np.float64)) / 5.0)
        score = score + float(weights["popularity"]) * pop
    return score


@dataclass
class FitResult:
    weights: np.ndarray
    n_groups: int
    n_pairs: int
    iterations: int
    converged: bool
    loss: float

    def summary(self) -> dict:
        w = self.weights
        total = float(np.abs(w).sum())
        return {"terms": list(TERMS), "weights": [float(x) for x in w],
                "weights_l1_normalized": [float(x / total) if total > 0 else 0.0 for x in w],
                "n_groups": self.n_groups, "n_pairs": self.n_pairs, "iterations": self.iterations,
                "converged": self.converged, "loss": self.loss}


def _pairs(labels: np.ndarray, ptr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """(정답 행, 네거티브 행, 쌍 가중치, 쌍이 있는 요청 수). 요청 안 쌍 가중치의 합은 1."""
    labels = np.asarray(labels, dtype=bool)
    g = group_ids(ptr)
    n_groups = len(ptr) - 1
    neg_rows = np.flatnonzero(~labels)          # 요청 순으로 정렬돼 있다
    neg_count = np.bincount(g[neg_rows], minlength=n_groups)
    neg_ptr = np.concatenate([[0], np.cumsum(neg_count)])
    pos_rows = np.flatnonzero(labels)
    pg = g[pos_rows]
    which, where = expand_ranges(neg_ptr[pg], neg_ptr[pg + 1])
    pos_idx, neg_idx = pos_rows[which], neg_rows[where]
    pair_group = g[pos_idx]
    per_group = np.bincount(pair_group, minlength=n_groups)
    weight = 1.0 / per_group[pair_group] if len(pair_group) else np.zeros(0)
    return pos_idx, neg_idx, weight, int((per_group > 0).sum())


def fit_pairwise_logistic(x: np.ndarray, labels: np.ndarray, ptr: np.ndarray, l2: float = 1e-6,
                          max_iter: int = 100, tol: float = 1e-10) -> FitResult:
    """쌍별 조건부 로지스틱의 가중치(절편 없음). 정답이나 네거티브가 없는 요청은 쓰지 않는다."""
    x = np.asarray(x, dtype=np.float64)
    pos_idx, neg_idx, a, n_groups = _pairs(labels, np.asarray(ptr, dtype=np.int64))
    dim = x.shape[1]
    w = np.zeros(dim)
    if n_groups == 0:
        return FitResult(w, 0, 0, 0, False, float("nan"))
    d = x[pos_idx] - x[neg_idx]
    a = a / n_groups

    def loss(v: np.ndarray) -> float:
        return float(a @ np.logaddexp(0.0, -(d @ v)) + 0.5 * l2 * (v @ v))

    cur = loss(w)
    converged, it = False, 0
    for it in range(1, max_iter + 1):
        z = d @ w
        s = 1.0 / (1.0 + np.exp(np.clip(z, -500, 500)))          # sigma(-z)
        grad = -(d.T @ (a * s)) + l2 * w
        hess = (d.T * (a * s * (1.0 - s))) @ d + l2 * np.eye(dim)
        step = np.linalg.solve(hess, -grad)
        t = 1.0
        while t > 1e-8:
            cand = w + t * step
            new = loss(cand)
            if new <= cur + 1e-4 * t * (grad @ step):
                break
            t *= 0.5
        w_next = w + t * step
        done = np.max(np.abs(w_next - w)) < tol
        w, cur = w_next, loss(w_next)
        if done:
            converged = True
            break
    return FitResult(w, n_groups, int(len(pos_idx)), it, converged, cur)


def fitted_scores(feats: pd.DataFrame, weights: np.ndarray, tau_h: float = 48.0) -> np.ndarray:
    return heuristic_terms(feats, tau_h) @ np.asarray(weights, dtype=np.float64)
