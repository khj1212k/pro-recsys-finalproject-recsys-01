"""EB-NeRD 실험용 모델: 팀 방식 LightGBM, ranker v2(LambdaRank), 휴리스틱 베이스라인.

하이퍼파라미터는 ai_workspace/recommend_engine/config/config.yaml의 팀 설정을 모든
ablation 단계에 똑같이 쓴다(차이는 목적함수/그룹/네거티브/피처만). 각 모델은 자기
학습 데이터와 같은 방식으로 만든 train 마지막 날 데이터로 early-stop 한다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd

from .prepare import RankTask

TEAM_FEATURES = ["hours_since_pub", "is_fresh_24h", "is_fresh_7d", "hist_cos", "cat_match_count",
                 "is_cat_match", "news_category", "user_age", "user_gender", "user_ncat"]
POP_FEATURES = ["pop_clicks_6h", "pop_clicks_24h", "pop_clicks_48h", "pop_inviews_24h", "pop_ctr_24h"]
SHORT_FEATURES = ["short_cos", "short_len", "sess_cos", "sess_len", "hours_since_last_event"]
V2_EXTRA_FEATURES = ["cat_share", "hist_len"]
ALL_GROUPS = ("recency", "history", "team_category", "category", "popularity", "short_term")

TEAM_PARAMS = {
    "boosting_type": "gbdt", "num_leaves": 31, "learning_rate": 0.05, "feature_fraction": 0.9,
    "bagging_fraction": 0.8, "bagging_freq": 5, "verbose": -1,
}
NUM_BOOST_ROUND = 1000
EARLY_STOPPING = 50


@dataclass
class ModelSpec:
    name: str
    data: str          # "random_neg" | "inview"
    objective: str     # "binary" | "lambdarank"
    group: str         # "none" | "user" | "impression" | "request"
    features: list[str]
    note: str = ""


ABLATION = [
    ModelSpec("team_binary", "random_neg", "binary", "none", TEAM_FEATURES,
              "팀 원본: binary + 클릭당 무작위 네거티브 5개 + 팀 피처"),
    ModelSpec("lambdarank_user_groups", "random_neg", "lambdarank", "user", TEAM_FEATURES,
              "+LambdaRank, 쿼리=유저 전체(레거시 group_key=user_id)"),
    ModelSpec("lambdarank_impression_groups", "random_neg", "lambdarank", "impression", TEAM_FEATURES,
              "+쿼리=impression_id(그 노출의 클릭들+각자의 무작위 네거티브). 한 노출의 클릭은 "
              "시각이 같으므로 현재 main의 group_key=user_timestamp와 같은 묶음"),
    ModelSpec("inview_negatives", "inview", "lambdarank", "request", TEAM_FEATURES,
              "+실제 노출 비클릭 네거티브, 쿼리=impression_id"),
    ModelSpec("plus_trailing_popularity", "inview", "lambdarank", "request", TEAM_FEATURES + POP_FEATURES,
              "+trailing 인기도(6/24/48h 클릭, 24h 노출·CTR)"),
    ModelSpec("plus_short_term_session", "inview", "lambdarank", "request",
              TEAM_FEATURES + POP_FEATURES + SHORT_FEATURES, "+24h 단기·세션 벡터 코사인"),
    ModelSpec("ranker_v2", "inview", "lambdarank", "request",
              TEAM_FEATURES + POP_FEATURES + SHORT_FEATURES + V2_EXTRA_FEATURES,
              "+카테고리 분포 share·히스토리 길이 (= ranker v2 전체)"),
]

# ablation 사슬 밖: 같은 ranker v2 피처로 네거티브 출처만 서빙 분포(P2 풀)에 맞춘 변형.
V2_FEATURES = TEAM_FEATURES + POP_FEATURES + SHORT_FEATURES + V2_EXTRA_FEATURES
NEGATIVE_VARIANTS = [
    ModelSpec("ranker_v2_poolneg", "pool_neg", "lambdarank", "request", V2_FEATURES,
              "ranker v2 피처 + 48h 발행 풀 무작위 네거티브 20개(노출 목록 미사용)"),
    ModelSpec("ranker_v2_mixed", "mixed_neg", "lambdarank", "request", V2_FEATURES,
              "ranker v2 피처 + 노출 비클릭 + 48h 풀 무작위 네거티브 20개"),
]


def feature_matrix(feats: pd.DataFrame, task: RankTask, columns: list[str]) -> np.ndarray:
    pair_req = task.req.pair_req
    extra = {
        "user_age": task.extra["age"][pair_req],
        "user_gender": task.extra["gender"][pair_req],
    }
    cols = [np.asarray(extra[c] if c in extra else feats[c], dtype=np.float32) for c in columns]
    return np.column_stack(cols) if cols else np.zeros((len(pair_req), 0), np.float32)


def _order_and_groups(task: RankTask, group: str,
                      rng: Optional[np.random.Generator] = None) -> tuple[np.ndarray, Optional[np.ndarray]]:
    """LightGBM에 넣을 행 순서와 쿼리 크기. 쿼리 안에서는 행을 무작위로 섞는다.

    LightGBM의 NDCG 평가는 점수 동점을 입력 순서대로 두기 때문에, 과제 구성상 정답이
    쿼리 맨 앞에 오면(무작위/풀 네거티브) 트리가 적어 동점이 많은 초반 반복의 NDCG가
    부풀고 early stopping이 1회차를 고르는 인공물이 생긴다.
    """
    rng = rng or np.random.default_rng(0)
    n_pairs = len(task.labels)
    if group == "none":
        return np.arange(n_pairs), None
    noise = rng.random(n_pairs)
    if group == "request":
        order = np.lexsort((noise, task.req.pair_req))
        sizes = task.req.n_candidates
        return order, sizes[sizes > 0]
    if group == "impression":
        imps = np.asarray(task.imp_index)[task.req.pair_req]
        order = np.lexsort((noise, imps))
        _, sizes = np.unique(imps[order], return_counts=True)
        return order, sizes
    if group == "user":
        users = task.group_user[task.req.pair_req]
        order = np.lexsort((noise, users))
        _, sizes = np.unique(users[order], return_counts=True)
        return order, sizes
    raise ValueError(group)


def _params(spec: ModelSpec, seed: int, num_threads: int) -> dict:
    p = dict(TEAM_PARAMS, seed=seed, num_threads=num_threads, objective=spec.objective)
    if spec.objective == "binary":
        p["metric"] = "auc"
    else:
        p.update(metric="ndcg", eval_at=[10], label_gain=[0, 1])
    return p


@dataclass
class TrainedModel:
    spec: ModelSpec
    seed: int
    booster: lgb.Booster
    best_iteration: int
    best_score: float
    fit_rows: int
    es_rows: int
    seconds: float
    importance_gain: dict = field(default_factory=dict)

    def predict(self, feats: pd.DataFrame, task: RankTask) -> np.ndarray:
        x = feature_matrix(feats, task, self.spec.features)
        return self.booster.predict(x, num_iteration=self.best_iteration)

    def summary(self) -> dict:
        return {"model": self.spec.name, "seed": self.seed, "best_iteration": self.best_iteration,
                "best_es_score": self.best_score, "fit_rows": self.fit_rows, "es_rows": self.es_rows,
                "train_seconds": round(self.seconds, 1), "importance_gain": self.importance_gain}


def train(spec: ModelSpec, fit: tuple[RankTask, pd.DataFrame], es: tuple[RankTask, pd.DataFrame],
          seed: int, num_threads: int = 4) -> TrainedModel:
    t0 = time.time()
    sets = []
    rng = np.random.default_rng(seed)
    for task, feats in (fit, es):
        order, groups = _order_and_groups(task, spec.group, rng)
        x = feature_matrix(feats, task, spec.features)[order]
        y = task.labels[order].astype(np.float32)
        sets.append((x, y, groups))
    (xf, yf, gf), (xe, ye, ge) = sets
    dfit = lgb.Dataset(xf, yf, group=gf, feature_name=spec.features, free_raw_data=True)
    des = lgb.Dataset(xe, ye, group=ge, reference=dfit, feature_name=spec.features)
    evals: dict = {}
    booster = lgb.train(
        _params(spec, seed, num_threads), dfit, num_boost_round=NUM_BOOST_ROUND, valid_sets=[des],
        valid_names=["es"],
        callbacks=[lgb.early_stopping(EARLY_STOPPING, first_metric_only=True, verbose=False),
                   lgb.record_evaluation(evals)],
    )
    metric = "auc" if spec.objective == "binary" else "ndcg@10"
    best_it = int(booster.best_iteration) or NUM_BOOST_ROUND
    curve = evals["es"][metric]
    gain = booster.feature_importance("gain", iteration=best_it)
    total = float(gain.sum()) or 1.0
    return TrainedModel(
        spec=spec, seed=seed, booster=booster, best_iteration=best_it, best_score=float(curve[best_it - 1]),
        fit_rows=len(yf), es_rows=len(ye), seconds=time.time() - t0,
        importance_gain={f: round(float(g) / total, 4) for f, g in zip(spec.features, gain)},
    )


def baseline_scores(feats: pd.DataFrame, n_rows: int, seed: int) -> dict[str, np.ndarray]:
    """휴리스틱 베이스라인 점수(클수록 앞). 동점은 지표 계산 시 seed 고정 무작위로 깬다."""
    rng = np.random.default_rng(seed)
    out = {"random": rng.random(n_rows)}
    for w in (6, 24, 48):
        out[f"popularity_{w}h"] = feats[f"pop_clicks_{w}h"].to_numpy(np.float64)
    out["recency"] = -feats["hours_since_pub"].to_numpy(np.float64)
    out["cosine_history"] = feats["hist_cos"].to_numpy(np.float64)
    out["category_share"] = feats["cat_share"].to_numpy(np.float64)
    return out
