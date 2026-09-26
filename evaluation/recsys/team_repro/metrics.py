"""평가 지표 유틸리티. team의 `src/core/evaluator.py::Evaluator`는 수정 없이 그대로
쓰고(MRR/Precision@k/Recall@k/nDCG@k/Coverage@k), 여기서는 그 위에 필요한 것만
얹는다: user별 지표 산출(부트스트랩의 재표본 단위), AUC, 부트스트랩 신뢰구간.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Set

import numpy as np

# v2 REQUIRED CHANGE #8: v1의 bootstrap_ci/bootstrap_paired_diff는 여러 시드의
# per-user 지표를 유저별로 평균낸 뒤(pooled_per_user) 유저만 재표본했다 - 그러면
# 학습 자체의 무작위성(시드 간 변동)이 신뢰구간에서 완전히 빠진다. 아래
# nested_bootstrap_*는 시드와 유저를 함께 재표본한다(시드는 좁고(3~5개) 유저는
# 31명 안팎이라는 것도 함께 report에 n으로 남긴다).


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


def _common_matrix(
    per_user_by_seed: Sequence[Dict[int, Dict[str, float]]], metric_key: str
) -> "tuple[np.ndarray, list, int, int]":
    seed_sets = [set(pu) for pu in per_user_by_seed if pu]
    common = sorted(set.intersection(*seed_sets)) if seed_sets else []
    n_seeds = len(per_user_by_seed)
    n_users = len(common)
    mat = np.zeros((n_seeds, n_users), dtype=float)
    for i, pu in enumerate(per_user_by_seed):
        for j, u in enumerate(common):
            mat[i, j] = pu[u][metric_key]
    return mat, common, n_seeds, n_users


def nested_bootstrap_ci(
    per_user_by_seed: Sequence[Dict[int, Dict[str, float]]],
    metric_key: str,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> Dict[str, float]:
    """시드 x 유저를 함께 재표본하는 중첩 부트스트랩(nested bootstrap). 각 반복마다
    (a) 시드를 복원추출로 재표본하고 (b) 유저도 복원추출로 재표본한 뒤, 재표본된
    (시드, 유저) 조합 전체의 평균을 취한다 - 학습 무작위성(시드 변동)과 유저
    표본 변동을 둘 다 신뢰구간에 반영한다(v1은 유저 변동만 반영했다)."""
    mat, common, n_seeds, n_users = _common_matrix(per_user_by_seed, metric_key)
    if n_seeds == 0 or n_users == 0:
        return {
            "mean": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan"),
            "n_users": n_users, "n_seeds": n_seeds, "n_boot": n_boot,
        }
    rng = np.random.default_rng(seed)
    point = float(mat.mean())
    boot_means = np.empty(n_boot)
    for b in range(n_boot):
        seed_idx = rng.integers(0, n_seeds, size=n_seeds)
        user_idx = rng.integers(0, n_users, size=n_users)
        boot_means[b] = mat[np.ix_(seed_idx, user_idx)].mean()
    lo, hi = np.percentile(boot_means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {
        "mean": point, "ci_lo": float(lo), "ci_hi": float(hi),
        "n_users": n_users, "n_seeds": n_seeds, "n_boot": n_boot,
    }


def nested_bootstrap_paired_diff(
    per_user_by_seed_a: Sequence[Dict[int, Dict[str, float]]],
    per_user_by_seed_b: Sequence[Dict[int, Dict[str, float]]],
    metric_key: str,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
    paired_seeds: bool = False,
) -> Dict[str, float]:
    """nested_bootstrap_ci의 paired-diff 버전. A/B 각각 (시드 리스트, per-user dict
    리스트)를 받아, 공통 유저에 대해 시드 x 유저를 함께 재표본하며 B-A 평균 차이의
    신뢰구간을 계산한다.

    paired_seeds=False(기본): 시드를 각 조건 안에서 독립적으로 재표본한다 - 두 조건이
    서로 다른 모델(다른 학습)일 때 쓴다.

    paired_seeds=True: A/B의 i번째 시드가 **같은 학습된 모델**을 공유할 때(예: 같은
    ranker를 point-in-time/as-written 두 방식으로만 추론한 누출 효과) 두 조건에 같은
    시드 인덱스를 쓴다. 독립 재표본은 공유된 모델 변동을 두 번 더해 구간을 과하게
    넓힌다(보수적 편향) - v2 리뷰 MINOR 지적 대응. 이 모드는 두 조건의 시드 수가
    같아야 한다.
    """
    mat_a, common_a, n_seeds_a, n_users_a = _common_matrix(per_user_by_seed_a, metric_key)
    mat_b, common_b, n_seeds_b, n_users_b = _common_matrix(per_user_by_seed_b, metric_key)
    common = sorted(set(common_a) & set(common_b))
    if not common or n_seeds_a == 0 or n_seeds_b == 0:
        return {
            "effect": float("nan"), "ci_lo": float("nan"), "ci_hi": float("nan"),
            "n_users": len(common), "n_boot": n_boot, "paired_seeds": paired_seeds,
        }
    if paired_seeds and n_seeds_a != n_seeds_b:
        raise ValueError(
            f"paired_seeds=True에는 두 조건의 시드 수가 같아야 합니다 ({n_seeds_a} vs {n_seeds_b})."
        )
    idx_a = [common_a.index(u) for u in common]
    idx_b = [common_b.index(u) for u in common]
    mat_a = mat_a[:, idx_a]
    mat_b = mat_b[:, idx_b]
    n_users = len(common)
    rng = np.random.default_rng(seed)
    point = float(mat_b.mean() - mat_a.mean())
    diffs = np.empty(n_boot)
    for b in range(n_boot):
        sa = rng.integers(0, n_seeds_a, size=n_seeds_a)
        sb = sa if paired_seeds else rng.integers(0, n_seeds_b, size=n_seeds_b)
        u_idx = rng.integers(0, n_users, size=n_users)
        diffs[b] = mat_b[np.ix_(sb, u_idx)].mean() - mat_a[np.ix_(sa, u_idx)].mean()
    lo, hi = np.percentile(diffs, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {
        "effect": point, "ci_lo": float(lo), "ci_hi": float(hi),
        "n_users": n_users, "n_seeds_a": n_seeds_a, "n_seeds_b": n_seeds_b, "n_boot": n_boot,
        "mean_a": float(mat_a.mean()), "mean_b": float(mat_b.mean()),
        "paired_seeds": paired_seeds,
    }


def ci_verdict(effect: Dict[str, float]) -> str:
    """부트스트랩 효과 dict를 CI 부호로만 분류한다 - 리포트 문장이 숫자와 무관하게
    하드코딩되어 '1.0을 0.897에 근접'처럼 쓰이던 문제(v2 리뷰 MINOR)를 막기 위해,
    결론 단어는 이 함수가 돌려주는 값에서만 고른다.

    반환: "positive"(CI 전체가 0 초과), "negative"(CI 전체가 0 미만),
    "inconclusive"(CI가 0을 포함), "nan"(계산 불가)."""
    lo, hi = effect.get("ci_lo"), effect.get("ci_hi")
    if lo is None or hi is None or lo != lo or hi != hi:
        return "nan"
    if lo > 0:
        return "positive"
    if hi < 0:
        return "negative"
    return "inconclusive"
