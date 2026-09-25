"""평가 지표 유틸리티. team의 `src/core/evaluator.py::Evaluator`는 수정 없이 그대로
쓰고(MRR/Precision@k/Recall@k/nDCG@k/Coverage@k), 여기서는 그 위에 필요한 것만
얹는다: user별 지표 산출(부트스트랩의 재표본 단위), AUC, 부트스트랩 신뢰구간.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set

import numpy as np


def per_user_metrics(
    evaluator,
    recommendations: Dict[int, List[int]],
    ground_truth: Dict[int, Set[int]],
    item_categories: Dict[int, List[int]],
) -> Dict[int, Dict[str, float]]:
    """user_id -> Evaluator.evaluate_user(...) 결과. ground_truth가 없거나 빈 유저는
    제외한다(Evaluator.evaluate_all_users와 동일한 제외 규칙)."""
    out: Dict[int, Dict[str, float]] = {}
    for uid, rec in recommendations.items():
        relevant = ground_truth.get(uid)
        if not relevant:
            continue
        out[uid] = evaluator.evaluate_user(rec, relevant, item_categories)
    return out


def aggregate(per_user: Dict[int, Dict[str, float]]) -> Dict[str, float]:
    if not per_user:
        return {}
    keys = next(iter(per_user.values())).keys()
    agg = {k: float(np.mean([m[k] for m in per_user.values()])) for k in keys}
    agg["num_users"] = len(per_user)
    return agg


def compute_auc(y_true: Sequence[int], y_score: Sequence[float]) -> Optional[float]:
    """valid set에 대한 AUC. 양/음 클래스가 둘 다 있어야 정의되므로, 한쪽만 있으면
    None을 반환한다(팀 config의 metric='auc'와 같은 정의: LightGBM 자체 binary AUC와
    동일하게 sklearn.roc_auc_score 사용)."""
    from sklearn.metrics import roc_auc_score

    y_true = np.asarray(y_true)
    if len(np.unique(y_true)) < 2:
        return None
    return float(roc_auc_score(y_true, y_score))


def bootstrap_ci(
    per_user: Dict[int, Dict[str, float]],
    metric_key: str,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> Dict[str, float]:
    """유저를 복원추출로 재표본해 지표 평균의 부트스트랩 신뢰구간을 계산한다."""
    rng = np.random.default_rng(seed)
    vals = np.array([m[metric_key] for m in per_user.values()], dtype=float)
    n = len(vals)
    if n == 0:
        return {"mean": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan"), "n_users": 0, "n_boot": n_boot}
    point = float(vals.mean())
    idx = rng.integers(0, n, size=(n_boot, n))
    boot_means = vals[idx].mean(axis=1)
    lo, hi = np.percentile(boot_means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"mean": point, "ci_lo": float(lo), "ci_hi": float(hi), "n_users": n, "n_boot": n_boot}


def bootstrap_paired_diff(
    per_user_a: Dict[int, Dict[str, float]],
    per_user_b: Dict[int, Dict[str, float]],
    metric_key: str,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> Dict[str, float]:
    """조건 A와 B(예: leaky vs fixed)에 공통으로 존재하는 유저에 대해, 같은 재표본
    인덱스로 A/B 쌍을 함께 리샘플링해 '효과(B-A 평균 차이)'의 부트스트랩 신뢰구간을
    계산한다(paired bootstrap - 유저별 변동을 상쇄해 검정력이 더 높다)."""
    common = sorted(set(per_user_a) & set(per_user_b))
    rng = np.random.default_rng(seed)
    n = len(common)
    if n == 0:
        return {"effect": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan"), "n_users": 0, "n_boot": n_boot}
    a_vals = np.array([per_user_a[u][metric_key] for u in common])
    b_vals = np.array([per_user_b[u][metric_key] for u in common])
    point = float((b_vals - a_vals).mean())
    idx = rng.integers(0, n, size=(n_boot, n))
    diff = (b_vals[idx] - a_vals[idx]).mean(axis=1)
    lo, hi = np.percentile(diff, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {
        "effect": point,
        "ci_lo": float(lo),
        "ci_hi": float(hi),
        "n_users": n,
        "n_boot": n_boot,
        "mean_a": float(a_vals.mean()),
        "mean_b": float(b_vals.mean()),
    }
