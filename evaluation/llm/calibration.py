"""judge 보정·신뢰도 계산 (ADR 0009/0010). DB·LLM 호출 없는 순수 함수만 둔다.

- cohen_kappa / spearman: judge 대 사람, 사람 대 사람(재라벨) 일치도
- two_fold_select: 임계값을 한 폴드에서 고르고 다른 폴드에서 κ를 재는 out-of-fold 보정.
  같은 데이터로 임계값을 고르고 κ까지 재면 임계값 탐색만으로 κ가 부풀려진다.
  폴드는 클러스터 단위로 나눈다 - 같은 클러스터의 세 후보 출력은 서로 독립이 아니다.
- self_preference_did: 같은 계열 judge의 자기선호편향을 이중차분으로 추정
- cluster_confidence_analysis: ClusterEvaluator confidence를 게이트로 쓸 가치가 있는지
"""

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, Hashable, List, Optional, Sequence

import numpy as np


# ---------------------------------------------------------------------------
# 일치도
# ---------------------------------------------------------------------------

def cohen_kappa(a: Sequence, b: Sequence, weights: Optional[str] = None) -> float:
    """Cohen's κ. weights=None/"linear"/"quadratic"(가중치는 순서형 숫자 라벨 전용).

    두 평가자가 모두 한 범주만 쓰면 우연 일치가 1이라 κ가 정의되지 않는다 - 1.0이 아니라
    NaN을 돌려준다(전부 PASS인 judge를 "완벽히 일치"로 보고하지 않기 위해).
    """
    if len(a) != len(b):
        raise ValueError(f"길이가 다릅니다: {len(a)} != {len(b)}")
    if not a:
        raise ValueError("빈 입력")
    cats = sorted(set(a) | set(b))
    idx = {c: i for i, c in enumerate(cats)}
    k = len(cats)
    obs = np.zeros((k, k))
    for x, y in zip(a, b):
        obs[idx[x], idx[y]] += 1
    obs /= obs.sum()
    exp = np.outer(obs.sum(axis=1), obs.sum(axis=0))

    if weights is None:
        w = 1.0 - np.eye(k)
    else:
        pos = np.array(cats, dtype=float)
        dist = np.abs(pos[:, None] - pos[None, :])
        if weights == "linear":
            w = dist
        elif weights == "quadratic":
            w = dist ** 2
        else:
            raise ValueError(f"알 수 없는 weights: {weights!r}")

    denom = float((w * exp).sum())
    if denom == 0:
        return float("nan")
    return 1.0 - float((w * obs).sum()) / denom


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    from scipy.stats import spearmanr

    if len(set(x)) < 2 or len(set(y)) < 2:
        return float("nan")
    return float(spearmanr(x, y).statistic)


def intra_rater_kappa(first: Dict[Hashable, object], second: Dict[Hashable, object]) -> dict:
    """같은 라벨러의 1차/2차 라벨 일치도. 두 번 라벨된 항목만 쓴다."""
    keys = sorted(set(first) & set(second), key=str)
    if not keys:
        return {"n": 0, "kappa": float("nan"), "agreement": float("nan")}
    a = [first[k] for k in keys]
    b = [second[k] for k in keys]
    return {
        "n": len(keys),
        "kappa": cohen_kappa(a, b),
        "agreement": sum(x == y for x, y in zip(a, b)) / len(keys),
    }


def balanced_accuracy(truth: Sequence[bool], pred: Sequence[bool]) -> float:
    recalls = []
    for cls in (True, False):
        idx = [i for i, t in enumerate(truth) if bool(t) is cls]
        if idx:
            recalls.append(sum(bool(pred[i]) is cls for i in idx) / len(idx))
    return float(np.mean(recalls)) if recalls else float("nan")


def roc_auc(scores: Sequence[float], truth: Sequence[bool]) -> float:
    from sklearn.metrics import roc_auc_score

    if len({bool(t) for t in truth}) < 2:
        return float("nan")
    return float(roc_auc_score([bool(t) for t in truth], scores))


# ---------------------------------------------------------------------------
# 클러스터 단위 폴드 / 부트스트랩
# ---------------------------------------------------------------------------

def group_folds(groups: Sequence[Hashable], k: int = 2, seed: int = 0) -> List[int]:
    uniq = sorted(set(groups), key=str)
    random.Random(seed).shuffle(uniq)
    fold_of = {g: i % k for i, g in enumerate(uniq)}
    return [fold_of[g] for g in groups]


def bootstrap_counts(n_groups: int, n_boot: int, seed: int) -> np.ndarray:
    """클러스터 복원 추출 n_boot회를 (n_boot × n_groups) 등장 횟수 행렬로 돌려준다.

    통계량이 클러스터별 합의 비율(평균·평균 차이)이면 행렬곱 한 번으로 모든 재표본을 계산할 수
    있다 - 10,000회 부트스트랩을 파이썬 루프로 돌리면 분석 한 번에 분 단위가 걸린다.
    """
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, n_groups, size=(n_boot, n_groups))
    counts = np.zeros((n_boot, n_groups))
    np.add.at(counts, (np.arange(n_boot)[:, None], picks), 1.0)
    return counts


def percentile_ci(values: Sequence[float], level: float = 0.95) -> List[float]:
    vals = np.asarray([v for v in values if not math.isnan(v)])
    if vals.size == 0:
        return [float("nan"), float("nan")]
    alpha = (1 - level) / 2
    return [float(np.quantile(vals, alpha)), float(np.quantile(vals, 1 - alpha))]


# ---------------------------------------------------------------------------
# judge 임계값 보정
# ---------------------------------------------------------------------------

@dataclass
class ThresholdRule:
    params: Dict[str, float]
    predict: Callable[[dict], bool] = field(repr=False)


def v2_rule_grid(max_claims_options: Sequence[int] = (0, 1, 2, 3)) -> List[ThresholdRule]:
    """judge v2 판정 규칙 후보: min(기준 점수) >= t1 그리고 근거 없는 주장 수 <= t2.

    엄격한 규칙부터 나열한다 - 학습 폴드 κ가 같으면 먼저 나온(더 엄격한) 규칙을 고른다.
    """
    rules = []
    for t1 in (5, 4, 3, 2, 1):
        for t2 in max_claims_options:
            def predict(rec, t1=t1, t2=t2):
                crit = rec.get("criteria")
                if not crit:
                    return False  # judge 호출 실패는 운영과 같이 FAIL
                return min(crit.values()) >= t1 and len(rec.get("unsupported_claims") or []) <= t2
            rules.append(ThresholdRule({"min_criterion_score": t1, "max_unsupported_claims": t2}, predict))
    return rules


def v1_rule_grid() -> List[ThresholdRule]:
    """judge v1(0~10 단일 점수) 기준선: score >= t."""
    rules = []
    for t in range(10, -1, -1):
        def predict(rec, t=t):
            s = rec.get("score")
            return s is not None and s >= t
        rules.append(ThresholdRule({"min_score": t}, predict))
    return rules


def _best_rule(records, human, rules):
    best, best_k = None, -math.inf
    for rule in rules:
        k = cohen_kappa([rule.predict(r) for r in records], list(human))
        k = -math.inf if math.isnan(k) else k
        if k > best_k:
            best, best_k = rule, k
    return best, best_k


def two_fold_select(
    records: Sequence[dict],
    human: Sequence[bool],
    groups: Sequence[Hashable],
    rules: Sequence[ThresholdRule],
    seed: int,
    k: int = 2,
) -> dict:
    """k-fold(클러스터 단위)로 판정 규칙을 고르고 out-of-fold κ를 잰다.

    반환: oof_kappa(보고·선정 기준), 폴드별로 고른 규칙과 학습/평가 κ, 그리고 운영에
    넣을 규칙(full_fit: 전체 데이터로 다시 고른 것 - 이 값의 κ는 낙관적이라 보고하지 않는다).
    """
    human = [bool(h) for h in human]
    folds = group_folds(groups, k=k, seed=seed)
    oof: List[Optional[bool]] = [None] * len(records)
    per_fold = []
    for f in range(k):
        train = [i for i, x in enumerate(folds) if x != f]
        test = [i for i, x in enumerate(folds) if x == f]
        rule, train_k = _best_rule([records[i] for i in train], [human[i] for i in train], rules)
        for i in test:
            oof[i] = rule.predict(records[i])
        test_k = cohen_kappa([oof[i] for i in test], [human[i] for i in test]) if test else float("nan")
        per_fold.append({"fold": f, "params": rule.params, "train_kappa": train_k,
                         "test_kappa": test_k, "n_test": len(test)})
    full_rule, full_k = _best_rule(records, human, rules)
    return {
        "oof_kappa": cohen_kappa(oof, human),
        "oof_pred": oof,
        "folds": per_fold,
        "full_fit": {"params": full_rule.params, "in_sample_kappa": full_k},
        "n": len(records),
    }


# ---------------------------------------------------------------------------
# 자기선호편향 (이중차분)
# ---------------------------------------------------------------------------

def _zscore(values: np.ndarray) -> np.ndarray:
    sd = values.std()
    return (values - values.mean()) / sd if sd > 0 else values * 0.0


def self_preference_did(
    rows: Sequence[dict],
    reference_judge: str,
    n_boot: int = 10000,
    seed: int = 20260925,
    level: float = 0.95,
) -> Dict[str, dict]:
    """judge별 자기선호편향 DiD 추정치와 클러스터 부트스트랩 CI.

    rows: {cluster, generator_family, judge, judge_family, judge_score, human_score}.
    judge 점수는 judge마다, 사람 점수는 전체에서 표준화한 뒤 오차 e = judge_z − human_z.
      gap_j   = mean(e_j | 생성기 계열 = j의 계열) − mean(e_j | 다른 계열)
      gap_ref = 같은 분할에서 기준 judge(어느 생성기와도 다른 계열)의 차이
      DiD_j   = gap_j − gap_ref
    기준 judge를 빼는 이유: 사람이 특정 생성기 문체를 유독 엄하게/후하게 본 효과가 gap_j에
    섞이는데, 그 효과는 기준 judge의 gap에도 똑같이 들어 있다.
    """
    judges = sorted({r["judge"] for r in rows})
    if reference_judge not in judges:
        raise ValueError(f"기준 judge {reference_judge!r}의 행이 없습니다")
    gen_fams = {r["generator_family"] for r in rows}

    err_by_judge: Dict[str, dict] = {}
    for j in judges:
        jr = [r for r in rows if r["judge"] == j]
        jz = _zscore(np.array([float(r["judge_score"]) for r in jr]))
        hz = _zscore(np.array([float(r["human_score"]) for r in jr]))
        err_by_judge[j] = {
            "err": jz - hz,
            "gen": np.array([r["generator_family"] for r in jr]),
            "groups": [r["cluster"] for r in jr],
            "family": jr[0]["judge_family"],
        }

    def gap(e, gen, fam):
        same, other = e[gen == fam], e[gen != fam]
        if same.size == 0 or other.size == 0:
            return float("nan")
        return float(same.mean() - other.mean())

    def per_cluster(d, fam, clusters):
        """클러스터별 (같은 계열 오차 합, 개수, 다른 계열 오차 합, 개수)."""
        pos = {c: i for i, c in enumerate(clusters)}
        agg = np.zeros((len(clusters), 4))
        for e, g, c in zip(d["err"], d["gen"], d["groups"]):
            col = 0 if g == fam else 2
            agg[pos[c], col] += e
            agg[pos[c], col + 1] += 1
        return agg

    def boot_gap(w, agg):
        with np.errstate(invalid="ignore", divide="ignore"):
            return (w @ agg[:, 0]) / (w @ agg[:, 1]) - (w @ agg[:, 2]) / (w @ agg[:, 3])

    ref = err_by_judge[reference_judge]
    out: Dict[str, dict] = {}
    for j in judges:
        if j == reference_judge:
            continue
        d = err_by_judge[j]
        fam = d["family"]
        if fam not in gen_fams:
            continue  # 자기 계열 생성물이 없으면 자기선호를 정의할 수 없다
        point_j, point_ref = gap(d["err"], d["gen"], fam), gap(ref["err"], ref["gen"], fam)

        # 두 judge를 같은 클러스터 재표본(같은 가중치 행)으로 계산해야 차이의 분산이 맞게 잡힌다
        clusters = sorted(set(d["groups"]) | set(ref["groups"]), key=str)
        w = bootstrap_counts(len(clusters), n_boot, seed)
        boots = boot_gap(w, per_cluster(d, fam, clusters)) - boot_gap(w, per_cluster(ref, fam, clusters))
        out[j] = {
            "family": fam,
            "did": point_j - point_ref,
            "ci": percentile_ci(boots.tolist(), level),
            "same_family_gap": point_j,
            "reference_gap": point_ref,
            "n_rows": int(len(d["err"])),
        }
    return out


# ---------------------------------------------------------------------------
# ClusterEvaluator confidence
# ---------------------------------------------------------------------------

def cluster_confidence_score(decision: str, confidence: float) -> float:
    """'이 클러스터가 단일 사건이다'에 대한 점수. FAIL의 confidence는 FAIL에 대한 확신이다."""
    c = float(confidence or 0.0)
    return c if str(decision).upper() == "PASS" else 1.0 - c


def _best_threshold(scores, truth):
    best_t, best_b = None, -math.inf
    for t in sorted(set(scores)):
        b = balanced_accuracy(truth, [s >= t for s in scores])
        if b > best_b:
            best_t, best_b = t, b
    return best_t, best_b


def cluster_confidence_analysis(
    decisions: Sequence[str],
    confidences: Sequence[float],
    single_event: Sequence[bool],
    groups: Sequence[Hashable],
    seed: int,
    min_auc: float,
    min_gain: float,
    k: int = 2,
    parsed: Optional[Sequence[bool]] = None,
) -> dict:
    """confidence 임계값 게이트가 기존 PASS/FAIL 결정보다 나은지 (ADR 0009 사전 등록 기준).

    기준선은 ClusterEvaluator의 PASS/FAIL 그대로의 balanced accuracy, 비교 대상은 점수
    s >= t (t는 학습 폴드에서 balanced accuracy 최대)의 out-of-fold balanced accuracy.

    parsed가 False인 행(호출 실패 -> FAIL/0.0, 텍스트 휴리스틱 -> FAIL/0.1)은 뺀다(ADR 0009 A3).
    점수 1 − c로 "가장 확신한 단일 사건"이 되어 AUC를 왜곡하는데, 판정이 아니고 운영에서는
    어느 쪽 규칙이든 FAIL로 스킵되므로 두 규칙의 비교와도 무관하다. 개수는 따로 보고한다.
    """
    n_excluded = 0
    if parsed is not None:
        keep = [i for i, ok in enumerate(parsed) if ok]
        n_excluded = len(parsed) - len(keep)
        decisions = [decisions[i] for i in keep]
        confidences = [confidences[i] for i in keep]
        single_event = [single_event[i] for i in keep]
        groups = [groups[i] for i in keep]
    truth = [bool(x) for x in single_event]
    scores = [cluster_confidence_score(d, c) for d, c in zip(decisions, confidences)]
    baseline = balanced_accuracy(truth, [str(d).upper() == "PASS" for d in decisions])
    auc = roc_auc(scores, truth)

    folds = group_folds(groups, k=k, seed=seed)
    oof = [False] * len(scores)
    per_fold = []
    for f in range(k):
        train = [i for i, x in enumerate(folds) if x != f]
        test = [i for i, x in enumerate(folds) if x == f]
        t, b = _best_threshold([scores[i] for i in train], [truth[i] for i in train])
        for i in test:
            oof[i] = t is not None and scores[i] >= t
        per_fold.append({"fold": f, "threshold": t, "train_balanced_accuracy": b, "n_test": len(test)})
    oof_b = balanced_accuracy(truth, oof)
    full_t, _ = _best_threshold(scores, truth)
    gain = oof_b - baseline
    return {
        "n": len(scores),
        "n_excluded_unparsed": n_excluded,
        "auc": auc,
        "baseline_balanced_accuracy": baseline,
        "oof_balanced_accuracy": oof_b,
        "gain": gain,
        "folds": per_fold,
        "threshold_full_fit": full_t,
        "gate_recommended": bool(not math.isnan(auc) and auc >= min_auc and gain >= min_gain),
    }
