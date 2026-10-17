"""한국어 기사에서 BGE-M3 임베딩이 주제 구조를 담는지 보는 점검 (ADR 0031 결정 5).

무엇을 하는가
- E0 반출본(ADR 0036)의 임베딩 팩(BGE-M3 1024차원, 입력 = 제목 + 본문 앞 8,000자, L2 정규화)과 반출 행의
  메타데이터 중 **언론사와 URL 경로만** 쓴다. 반출 행에는 제목·본문도 들어 있어 JSON 한 줄을 읽을 때 함께
  메모리에 올라오지만 곧바로 버리고, 어떤 반환값·파일·출력에도 넣지 않는다.
- 라벨은 언론사가 기사 URL에 붙인 섹션이다: 매일경제 `/news/<섹션>/<번호>`, 동아일보
  `/news/<섹션>/article/all/...`. 나머지 여섯 언론사는 URL에 섹션이 없어 라벨이 없다.
- **1차**(판정): 두 언론사의 섹션을 서비스의 7개 범주(`ai_workspace/db/schema.py`의 category 표)로 옮긴 라벨에서
  코사인 kNN(k = 10) leave-one-out 정확도와 macro-F1, 기사 단위 부트스트랩 95% 구간, 다수 클래스·무작위 기준선,
  범주별 표. kNN 규칙(자기 자신을 뺀 최근접 k개의 다수결, 동률은 가장 가까운 이웃의 범주)은 덴마크어 점검
  (`evaluation/recsys/ebnerd/embedding_sanity.py`, ADR 0013)과 같다. 기준은 ADR 0031 결정 5: 정확도 ≥ 0.6이고
  다수 클래스 비율보다 높다.
- **서술**(판정에 쓰지 않음): 언론사별 원래 섹션, 언론사를 건너는 예측(한 언론사의 기사를 다른 언론사의 기사만으로),
  언론사 라벨(8개 언론사 전체), 거의 같은 기사(코사인 ≥ 0.95)를 이웃에서 뺀 민감도, k = 5·20.
- 기준(k = 10 leave-one-out, ≥ 0.6, 다수 클래스보다 높음)은 ADR 0031 결정 5에 2026-10-06부터 적혀 있었다. 라벨 규칙·
  범주 대응·분석 목록은 이 점검을 만들며 정했고(URL 경로의 모양과 섹션별 건수만 보고), 이 파일을 커밋한 뒤 깨끗한
  작업 트리에서 한 번 돌렸다. 그 전에 실제 반출본으로 돌리지 않았다는 것은 작성자의 진술이다 - 사전 등록한 실행이
  아니며 증거 등급은 `descriptive`다.

무엇을 하지 않는가
- 추천 정확도가 아니다. 클릭을 쓰지 않는다. "임베딩 공간이 한국어 뉴스의 주제 구조를 담는가"만 본다.
- ADR 0031 결정 5는 "뉴스레터 500건"이라 적었다. 생성된 뉴스레터가 없어서 단위는 기사다.
- 다시 임베딩하지 않는다. 팩에는 제목+본문 벡터 하나뿐이라 제목만의 벡터와 비교하지 않는다.

    python -m evaluation.recsys.kr_embedding_check run --export data/exports/<T0> \\
        --out-json reports/kr/embedding_check_v1.json --out-md reports/kr/embedding_check_v1.md
    python -m evaluation.recsys.kr_embedding_check render --json reports/kr/embedding_check_v1.json \\
        --out-md reports/kr/embedding_check_v1.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import numpy as np

FORMAT = "kr-embedding-check/1"
PACK_FORMAT = "news-embedding-pack/1"
EXPORT_FORMAT = "frozen-news-export/1"
REPO_ROOT = Path(__file__).resolve().parents[2]

K = 10
K_SENSITIVITY = (5, 20)
THRESHOLD = 0.6
N_BOOT = 1000
BOOT_SEED = 0
RANDOM_DRAWS = 1000
RANDOM_SEED = 1
NEAR_DUP_COS = 0.95
MIN_SECTION_N = 10  # 언론사별 원래 섹션 분석에서 이보다 적은 섹션은 뺀다(leave-one-out에서 거의 맞힐 수 없다)

# 서비스의 카테고리 표(ai_workspace/db/schema.py insert_initial_category_data)와 같은 7개.
SERVICE_CATEGORIES = ("정치", "사회", "경제", "IT/과학", "생활/문화", "스포츠", "세계")

# 언론사 -> (호스트, 경로 정규식). 정규식의 첫 그룹이 섹션이다. 다른 모양의 URL은 라벨이 없다.
SECTION_RULES: Dict[str, Tuple[str, "re.Pattern[str]"]] = {
    "매일경제": ("www.mk.co.kr", re.compile(r"^/news/([a-z]+)/\d+/?$")),
    "동아일보": ("www.donga.com", re.compile(r"^/news/([A-Za-z]+)/article/all/")),
}

# 섹션 -> 서비스 범주. 없는 섹션은 1차에서 뺀다: 동아일보 Opinion(사설·칼럼 - 주제가 섞인다), People(인물·부고),
# Entertainment(연예 - 서비스 범주에 없다), list 등.
SERVICE_MAP: Dict[str, Dict[str, str]] = {
    "매일경제": {
        "politics": "정치", "society": "사회", "economy": "경제", "business": "경제", "stock": "경제",
        "realestate": "경제", "world": "세계", "it": "IT/과학", "culture": "생활/문화", "sports": "스포츠",
    },
    "동아일보": {
        "Politics": "정치", "Society": "사회", "Economy": "경제", "Inter": "세계", "It": "IT/과학",
        "Culture": "생활/문화", "Sports": "스포츠",
    },
}
LABELED_PRESS = tuple(SECTION_RULES)


class CheckError(RuntimeError):
    pass


# ------------------------------------------------------------------ 라벨


def section_label(press: str, url: str) -> Optional[str]:
    """언론사가 URL 경로에 붙인 섹션. 규칙이 없는 언론사나 다른 모양의 URL은 None."""
    rule = SECTION_RULES.get(press)
    if rule is None:
        return None
    host, pattern = rule
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.netloc != host:
        return None
    m = pattern.match(parts.path)
    return m.group(1) if m else None


def service_category(press: str, section: Optional[str]) -> Optional[str]:
    if section is None:
        return None
    return SERVICE_MAP.get(press, {}).get(section)


# ------------------------------------------------------------------ kNN


def _unit(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise CheckError("노름이 0인 벡터가 있다")
    return x / norms


def knn_predict(x_query: np.ndarray, x_ref: np.ndarray, ref_labels: np.ndarray, k: int, *,
                self_index: Optional[np.ndarray] = None, max_cos: Optional[float] = None,
                chunk: int = 1024) -> Tuple[np.ndarray, np.ndarray]:
    """query 행마다 ref에서 코사인 최근접 k개의 다수결 라벨(정수)과 남은 이웃 중 가장 가까운 것의 코사인.

    self_index[i]는 query i와 같은 기사인 ref 행 번호(없으면 -1)이고 그 행은 이웃에서 뺀다(leave-one-out).
    max_cos가 있으면 코사인이 그 값 이상인 이웃도 뺀다. 동률은 가장 가까운 이웃이 속한 라벨로 깬다
    (`ebnerd.embedding_sanity.knn_category_accuracy`와 같은 규칙)."""
    q, r = _unit(x_query), _unit(x_ref)
    lab = np.asarray(ref_labels, dtype=np.int64)
    if len(lab) != len(r):
        raise CheckError("ref 라벨 수가 ref 행 수와 다르다")
    if k < 1:
        raise CheckError("k는 1 이상")
    n_cls = int(lab.max()) + 1
    nq = len(q)
    pred = np.empty(nq, dtype=np.int64)
    top1 = np.empty(nq, dtype=np.float64)
    for s in range(0, nq, chunk):
        e = min(s + chunk, nq)
        rows = np.arange(e - s)
        sims = q[s:e] @ r.T
        if self_index is not None:
            si = np.asarray(self_index[s:e], dtype=np.int64)
            own = si >= 0
            sims[rows[own], si[own]] = -np.inf
        if max_cos is not None:
            sims[sims >= max_cos] = -np.inf
        if k > sims.shape[1]:
            raise CheckError("이웃 후보가 k개보다 적다")
        part = np.argpartition(-sims, k - 1, axis=1)[:, :k]
        psims = np.take_along_axis(sims, part, axis=1)
        if not np.all(np.isfinite(psims)):
            raise CheckError("뺀 뒤 남은 이웃이 k개보다 적은 행이 있다")
        order = np.argsort(-psims, axis=1, kind="stable")
        nbr = lab[np.take_along_axis(part, order, axis=1)]
        top1[s:e] = np.take_along_axis(psims, order[:, :1], axis=1)[:, 0]
        counts = np.zeros((e - s, n_cls), dtype=np.int64)
        nearest_bonus = np.zeros((e - s, n_cls), dtype=np.int64)
        for j in range(k):
            np.add.at(counts, (rows, nbr[:, j]), 1)
            nearest_bonus[rows, nbr[:, j]] = np.maximum(nearest_bonus[rows, nbr[:, j]], k - j)
        pred[s:e] = np.argmax(counts * (k + 1) + nearest_bonus, axis=1)
    return pred, top1


# ------------------------------------------------------------------ 지표


def _confusions(y_true: np.ndarray, y_pred: np.ndarray, n_cls: int) -> np.ndarray:
    """(B, n) 쌍에서 (B, C, C) 혼동 행렬(행 = 실제, 열 = 예측)."""
    t, p = np.atleast_2d(y_true), np.atleast_2d(y_pred)
    b = t.shape[0]
    flat = (np.arange(b)[:, None] * n_cls * n_cls + t * n_cls + p).ravel()
    return np.bincount(flat, minlength=b * n_cls * n_cls).reshape(b, n_cls, n_cls)


def _scores(cms: np.ndarray) -> Dict[str, np.ndarray]:
    """혼동 행렬 묶음에서 정확도, macro-F1, 범주별 정밀도·재현율·F1. 예측이 없는 범주의 정밀도는 0으로 둔다.
    macro-F1은 그 표본에 실제로 있는 범주(지지 > 0)의 F1 평균이다."""
    tp = np.diagonal(cms, axis1=1, axis2=2).astype(np.float64)
    support = cms.sum(axis=2).astype(np.float64)
    predicted = cms.sum(axis=1).astype(np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        precision = np.where(predicted > 0, tp / predicted, 0.0)
        recall = np.where(support > 0, tp / support, 0.0)
        f1 = np.where(precision + recall > 0, 2 * precision * recall / (precision + recall), 0.0)
    present = support > 0
    macro = (f1 * present).sum(axis=1) / present.sum(axis=1)
    accuracy = tp.sum(axis=1) / support.sum(axis=1)
    return {"accuracy": accuracy, "macro_f1": macro, "precision": precision, "recall": recall, "f1": f1,
            "support": support, "predicted": predicted}


def classification_metrics(y_true: np.ndarray, y_pred: np.ndarray, n_cls: int) -> Dict[str, Any]:
    cm = _confusions(np.asarray(y_true), np.asarray(y_pred), n_cls)
    s = _scores(cm)
    return {"accuracy": float(s["accuracy"][0]), "macro_f1": float(s["macro_f1"][0]),
            "precision": s["precision"][0], "recall": s["recall"][0], "f1": s["f1"][0],
            "support": s["support"][0].astype(int), "predicted": s["predicted"][0].astype(int),
            "confusion": cm[0]}


def _interval(values: np.ndarray) -> List[float]:
    lo, hi = np.percentile(values, [2.5, 97.5])
    return [float(lo), float(hi)]


def bootstrap_ci(y_true: np.ndarray, y_pred: np.ndarray, n_cls: int, n_boot: int = N_BOOT,
                 seed: int = BOOT_SEED, batch: int = 100) -> Dict[str, List[float]]:
    """기사 단위 백분위 부트스트랩 95% 구간(정확도, macro-F1). (실제, 예측) 쌍을 복원 추출한다 - 이웃 집합은
    고정이라 이웃 쪽의 표본 변동은 구간에 들어 있지 않다."""
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    rng = np.random.default_rng(seed)
    n = len(y_true)
    acc, macro = [], []
    for start in range(0, n_boot, batch):
        b = min(batch, n_boot - start)
        idx = rng.integers(0, n, size=(b, n))
        s = _scores(_confusions(y_true[idx], y_pred[idx], n_cls))
        acc.append(s["accuracy"])
        macro.append(s["macro_f1"])
    return {"accuracy": _interval(np.concatenate(acc)), "macro_f1": _interval(np.concatenate(macro))}


def _random_draws(y_true: np.ndarray, n_cls: int, probs: np.ndarray, draws: int, rng: np.random.Generator,
                  batch: int = 100) -> Dict[str, Any]:
    acc, macro = [], []
    for start in range(0, draws, batch):
        b = min(batch, draws - start)
        pred = rng.choice(n_cls, size=(b, len(y_true)), p=probs)
        s = _scores(_confusions(np.broadcast_to(y_true, pred.shape), pred, n_cls))
        acc.append(s["accuracy"])
        macro.append(s["macro_f1"])
    acc, macro = np.concatenate(acc), np.concatenate(macro)
    return {"accuracy_mean": float(acc.mean()), "accuracy_95": _interval(acc),
            "macro_f1_mean": float(macro.mean()), "macro_f1_95": _interval(macro)}


def baselines(y_true: np.ndarray, n_cls: int, draws: int = RANDOM_DRAWS, seed: int = RANDOM_SEED,
              names: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """다수 클래스(항상 가장 많은 범주), 균등 무작위(있는 범주 중 균등), 비율 무작위(범주 비율대로)."""
    y_true = np.asarray(y_true)
    counts = np.bincount(y_true, minlength=n_cls)
    present = counts > 0
    share = counts / counts.sum()
    major = int(np.argmax(counts))
    p_major = float(share[major])
    rng = np.random.default_rng(seed)
    uniform = np.where(present, 1.0, 0.0)
    return {
        "majority": {"label": names[major] if names is not None else major, "accuracy": p_major,
                     # 다수 범주: 정밀도 = 비율, 재현율 = 1. 다른 범주의 F1은 0.
                     "macro_f1": float(2 * p_major / (1 + p_major) / present.sum())},
        "random_uniform": _random_draws(y_true, n_cls, uniform / uniform.sum(), draws, rng),
        "random_prior": _random_draws(y_true, n_cls, share, draws, rng),
        "random_draws": draws, "random_seed": seed,
    }


def ids_sha256(ids: Sequence[int]) -> str:
    text = "\n".join(str(int(i)) for i in sorted(int(i) for i in ids))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def evaluate(x: np.ndarray, labels: Sequence[str], ids: Sequence[int], *, k: int = K,
             ref_x: Optional[np.ndarray] = None, ref_labels: Optional[Sequence[str]] = None,
             ref_ids: Optional[Sequence[int]] = None, max_cos: Optional[float] = None,
             n_boot: int = N_BOOT, with_baselines: bool = True,
             groups: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """query(x, labels)의 kNN 예측을 채점한다. ref가 없으면 query 안의 leave-one-out이다.
    groups(질의 행마다의 이름, 예: 언론사)를 주면 그 묶음별 건수와 정확도를 함께 낸다(서술용)."""
    labels = list(labels)
    loo = ref_x is None
    if loo:
        ref_x, ref_labels, ref_ids = x, labels, ids
    classes = sorted(set(labels) | set(ref_labels))
    index = {c: i for i, c in enumerate(classes)}
    y_true = np.array([index[c] for c in labels], dtype=np.int64)
    y_ref = np.array([index[c] for c in ref_labels], dtype=np.int64)
    self_index = np.arange(len(labels)) if loo else None
    pred, top1 = knn_predict(x, ref_x, y_ref, k, self_index=self_index, max_cos=max_cos)
    m = classification_metrics(y_true, pred, len(classes))
    out: Dict[str, Any] = {
        "n": len(labels), "n_ref": len(ref_labels), "leave_one_out": loo, "k": k, "max_cos": max_cos,
        "ids_sha256": ids_sha256(ids), "ref_ids_sha256": None if loo else ids_sha256(ref_ids),
        "classes": classes,
        "accuracy": m["accuracy"], "macro_f1": m["macro_f1"],
        "ci95": bootstrap_ci(y_true, pred, len(classes), n_boot=n_boot),
        "per_class": [
            {"label": c, "support": int(m["support"][i]), "predicted": int(m["predicted"][i]),
             "precision": float(m["precision"][i]), "recall": float(m["recall"][i]), "f1": float(m["f1"][i])}
            for i, c in enumerate(classes)
        ],
        "confusion": m["confusion"].astype(int).tolist(),
        "top1_cos": {
            "p05": float(np.percentile(top1, 5)), "p50": float(np.percentile(top1, 50)),
            "p95": float(np.percentile(top1, 95)), "p99": float(np.percentile(top1, 99)),
            "share_ge_near_dup": float((top1 >= NEAR_DUP_COS).mean()), "near_dup_cos": NEAR_DUP_COS,
        },
    }
    if groups is not None:
        g = np.asarray(list(groups), dtype=object)
        out["by_group"] = {str(name): {"n": int((g == name).sum()),
                                       "accuracy": float((pred[g == name] == y_true[g == name]).mean())}
                           for name in sorted(set(g.tolist()))}
    if with_baselines:
        out["baselines"] = baselines(y_true, len(classes), names=classes)
    return out


def verdict(primary: Mapping[str, Any]) -> Dict[str, Any]:
    """ADR 0031 결정 5의 기준(덴마크어 점검과 같다): 정확도 ≥ 0.6이고 다수 클래스 비율보다 높다."""
    acc, major = primary["accuracy"], primary["baselines"]["majority"]["accuracy"]
    checks = {"n_ge_500": primary["n"] >= 500, "accuracy_ge_threshold": acc >= THRESHOLD,
              "above_majority": acc > major}
    return {"rule": f"kNN(k={K}) LOO 정확도 >= {THRESHOLD} 이고 다수 클래스 비율보다 높다", "threshold": THRESHOLD,
            **checks, "met": all(checks.values()),
            "ci_lower_ge_threshold": primary["ci95"]["accuracy"][0] >= THRESHOLD,
            "unit": "기사(뉴스레터 아님)", "registered_unit": "뉴스레터 500건 이상",
            "unit_matches_registered_rule": False}


def verdict_label(v: Mapping[str, Any]) -> str:
    """판정을 한 낱말로 줄일 때 쓰는 말. 등록한 단위(뉴스레터)가 아니면 '대리 기준'이라고 붙인다.

    v1 JSON(코드 2d66f96)에는 `unit_matches_registered_rule`가 없다 - 그 실행도 기사 단위였으므로 없으면 False로 읽는다.
    """
    prefix = "" if v.get("unit_matches_registered_rule", False) else "대리 기준 "
    return prefix + ("충족" if v["met"] else "불충족")


def reference_class_counts(result: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
    """언론사를 건너는 예측마다 이웃 쪽 언론사의 서비스 범주별 기사 수(건수만). label_coverage와 범주 대응에서 센다."""
    cov, service_map = result["label_coverage"], result["meta"]["params"]["service_map"]
    out: Dict[str, Dict[str, int]] = {}
    for key, r in result["cross_press"].items():
        ref_press = key.split("<-", 1)[1]
        counts = {c: 0 for c in r["classes"]}
        for section, n in cov[ref_press]["sections"].items():
            cat = service_map.get(ref_press, {}).get(section)
            if cat is not None:
                counts[cat] = counts.get(cat, 0) + int(n)
        out[key] = counts
    return out


def sparse_reference_classes(result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """이웃 쪽 기사가 k/2보다 적은 범주. k개 이웃 중 그 범주의 표가 이웃 쪽 건수를 넘지 못해 다수결로 거의 이기지 못한다."""
    out: List[Dict[str, Any]] = []
    for key, counts in reference_class_counts(result).items():
        r = result["cross_press"][key]
        for i, c in enumerate(r["classes"]):
            if counts[c] * 2 < r["k"]:
                out.append({"pair": key, "label": c, "n_ref": counts[c], "support": r["per_class"][i]["support"],
                            "correct": int(r["confusion"][i][i])})
    return out


# ------------------------------------------------------------------ 입력


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_inputs(export_dir: Path) -> Dict[str, Any]:
    """임베딩 팩과 반출 행의 언론사·URL만 읽는다(읽기 전용). 파일 해시와 신원이 매니페스트와 다르면 멈춘다."""
    export_dir = Path(export_dir)
    pack_dir = export_dir / "embeddings"
    pman = json.loads((pack_dir / "embeddings_manifest.json").read_text(encoding="utf-8"))
    eman = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    if pman.get("format") != PACK_FORMAT or eman.get("format") != EXPORT_FORMAT:
        raise CheckError("팩 또는 반출본의 형식이 다르다")
    if pman["export_identity_sha256"] != eman["identity_sha256"]:
        raise CheckError("임베딩 팩이 이 반출본의 것이 아니다(신원 sha256 불일치)")
    pack_files = {}
    for name in ("emb.f16.npy", "ids.npy"):
        digest = _sha256_file(pack_dir / name)
        if digest != pman["files"][name]["sha256"]:
            raise CheckError(f"{name}의 sha256이 팩 매니페스트와 다르다")
        pack_files[name] = digest
    articles = export_dir / "articles.jsonl"
    articles_digest = _sha256_file(articles)
    if articles_digest != eman["files"]["articles.jsonl"]["sha256"]:
        raise CheckError("articles.jsonl의 sha256이 반출 매니페스트와 다르다")
    emb = np.load(pack_dir / "emb.f16.npy", allow_pickle=False).astype(np.float32)
    ids = np.load(pack_dir / "ids.npy", allow_pickle=False).astype(np.int64)
    if emb.shape != (pman["rows"], pman["dim"]) or len(ids) != pman["rows"]:
        raise CheckError("팩 배열의 모양이 매니페스트와 다르다")
    meta: Dict[int, Tuple[str, str]] = {}
    with articles.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            # 언론사와 URL만 남긴다. 제목·본문은 이 줄의 row와 함께 버려진다.
            meta[int(row["id"])] = (str(row["press"]), str(row["url"]))
            del row
    if len(meta) != len(ids) or set(meta) != set(ids.tolist()):
        raise CheckError("팩의 기사 id가 반출 행과 다르다")
    identity = eman["identity"]
    return {
        "emb": emb, "ids": ids,
        "press": [meta[int(i)][0] for i in ids], "urls": [meta[int(i)][1] for i in ids],
        "data": {
            "source": "Tier 0 news_raw 동결 반출본(ADR 0036), 한국어 수집 기사 - 집계만",
            "export_identity_sha256": eman["identity_sha256"],
            "window_utc": [identity["window_utc"]["start"], identity["window_utc"]["end"]],
            "crawled_at_range_utc": identity["crawled_at_range_utc"],
            "rows": identity["rows"], "by_press": identity["by_press"],
            "articles_sha256": articles_digest,
            "pack": {
                "format": pman["format"], "rows": pman["rows"], "dim": pman["dim"], "dtype": pman["dtype"],
                "model": {key: pman["model"][key] for key in
                          ("name", "revision", "max_length", "text_rule", "l2_normalize", "precision")},
                "files_sha256": pack_files, "created_at_utc": pman.get("created_at_utc"),
                "title_only_vectors": False,
            },
        },
    }


def _code() -> Dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False).stdout.strip()

    return {"git_sha": git("rev-parse", "HEAD") or None, "dirty": bool(git("status", "--porcelain"))}


# ------------------------------------------------------------------ 실행


def _label_coverage(press: Sequence[str], sections: Sequence[Optional[str]],
                    service: Sequence[Optional[str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for p in sorted(set(press)):
        rows = [i for i, x in enumerate(press) if x == p]
        secs: Dict[str, int] = {}
        for i in rows:
            if sections[i] is not None:
                secs[sections[i]] = secs.get(sections[i], 0) + 1
        out[p] = {"rows": len(rows), "with_section": sum(sections[i] is not None for i in rows),
                  "mapped_to_service": sum(service[i] is not None for i in rows),
                  "sections": dict(sorted(secs.items(), key=lambda kv: (-kv[1], kv[0]))),
                  "unmapped_sections": sorted(s for s in secs if service_category(p, s) is None)}
    return out


def analyze(emb: np.ndarray, ids: np.ndarray, press: Sequence[str], urls: Sequence[str],
            n_boot: int = N_BOOT) -> Dict[str, Any]:
    sections = [section_label(p, u) for p, u in zip(press, urls)]
    service = [service_category(p, s) for p, s in zip(press, sections)]
    press_arr = np.asarray(press, dtype=object)
    lab = np.flatnonzero([c is not None for c in service])
    lab_labels = [service[i] for i in lab]

    primary = evaluate(emb[lab], lab_labels, ids[lab], n_boot=n_boot, groups=[press[i] for i in lab])
    primary["verdict"] = verdict(primary)

    sensitivity = {
        "near_dup_excluded": evaluate(emb[lab], lab_labels, ids[lab], max_cos=NEAR_DUP_COS, n_boot=n_boot,
                                      with_baselines=False),
        **{f"k{kk}": evaluate(emb[lab], lab_labels, ids[lab], k=kk, n_boot=n_boot, with_baselines=False)
           for kk in K_SENSITIVITY},
    }

    within_press = {}
    for p in LABELED_PRESS:
        rows = [i for i in range(len(press)) if press[i] == p and sections[i] is not None]
        counts: Dict[str, int] = {}
        for i in rows:
            counts[sections[i]] = counts.get(sections[i], 0) + 1
        keep = {s for s, c in counts.items() if c >= MIN_SECTION_N}
        rows = np.array([i for i in rows if sections[i] in keep], dtype=np.int64)
        res = evaluate(emb[rows], [sections[i] for i in rows], ids[rows], n_boot=n_boot)
        res["dropped_sections"] = {s: c for s, c in sorted(counts.items()) if s not in keep}
        within_press[p] = res

    cross_press = {}
    for q_press, r_press in (("동아일보", "매일경제"), ("매일경제", "동아일보")):
        q = np.array([i for i in lab if press[i] == q_press], dtype=np.int64)
        r = np.array([i for i in lab if press[i] == r_press], dtype=np.int64)
        cross_press[f"{q_press}<-{r_press}"] = evaluate(
            emb[q], [service[i] for i in q], ids[q], ref_x=emb[r], ref_labels=[service[i] for i in r],
            ref_ids=ids[r], n_boot=n_boot)

    press_label = evaluate(emb, list(press_arr), ids, n_boot=n_boot)

    return {
        "label_coverage": _label_coverage(press, sections, service),
        "primary": primary,
        "sensitivity": sensitivity,
        "within_press_sections": within_press,
        "cross_press": cross_press,
        "press_label": press_label,
    }


def params() -> Dict[str, Any]:
    return {
        "k": K, "k_sensitivity": list(K_SENSITIVITY), "threshold": THRESHOLD, "n_boot": N_BOOT,
        "boot_seed": BOOT_SEED, "random_draws": RANDOM_DRAWS, "random_seed": RANDOM_SEED,
        "near_dup_cos": NEAR_DUP_COS, "min_section_n": MIN_SECTION_N,
        "service_categories": list(SERVICE_CATEGORIES),
        "section_rules": {p: {"host": h, "path_regex": rx.pattern} for p, (h, rx) in SECTION_RULES.items()},
        "service_map": SERVICE_MAP,
    }


def run(export_dir: Path, *, command: str, n_boot: int = N_BOOT) -> Dict[str, Any]:
    code = _code()
    inputs = load_inputs(export_dir)
    analyses = analyze(inputs["emb"], inputs["ids"], inputs["press"], inputs["urls"], n_boot=n_boot)
    return {
        "meta": {
            "format": FORMAT, "adr": "0031 결정 5", "command": command, "code": code,
            "run_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "data": inputs["data"], "params": params(), "n_boot": n_boot,
            "platform": {"python": platform.python_version(), "numpy": np.__version__},
            "evidence_grade": "descriptive",
        },
        **analyses,
    }


# ------------------------------------------------------------------ 리포트


def _f(x: Optional[float], d: int = 4) -> str:
    return "-" if x is None else f"{x:.{d}f}"


def _ci(ci: Sequence[float], d: int = 4) -> str:
    return f"[{ci[0]:.{d}f}, {ci[1]:.{d}f}]"


def _row(cells: Sequence[Any]) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    return [_row(header), _row(["---"] * len(header)), *[_row(r) for r in rows]]


def _metric_row(name: str, r: Mapping[str, Any]) -> List[Any]:
    return [name, r["n"], len(r["classes"]), _f(r["accuracy"]), _ci(r["ci95"]["accuracy"]),
            _f(r["macro_f1"]), _ci(r["ci95"]["macro_f1"])]


def _baseline_rows(b: Mapping[str, Any]) -> List[List[Any]]:
    return [
        [f"다수 클래스 (`{b['majority']['label']}`)", _f(b["majority"]["accuracy"]), "-",
         _f(b["majority"]["macro_f1"]), "-"],
        ["균등 무작위", _f(b["random_uniform"]["accuracy_mean"]), _ci(b["random_uniform"]["accuracy_95"]),
         _f(b["random_uniform"]["macro_f1_mean"]), _ci(b["random_uniform"]["macro_f1_95"])],
        ["비율 무작위", _f(b["random_prior"]["accuracy_mean"]), _ci(b["random_prior"]["accuracy_95"]),
         _f(b["random_prior"]["macro_f1_mean"]), _ci(b["random_prior"]["macro_f1_95"])],
    ]


def render_markdown(result: Mapping[str, Any]) -> str:
    meta, data, prm = result["meta"], result["meta"]["data"], result["meta"]["params"]
    pr, v = result["primary"], result["primary"]["verdict"]
    model = data["pack"]["model"]
    cov = result["label_coverage"]
    lines: List[str] = [
        "# 한국어 기사 임베딩 점검 v1 — BGE-M3 섹션 kNN leave-one-out (ADR 0031 결정 5)",
        "",
        "> 이 리포트는 `evaluation/recsys/kr_embedding_check.py`가 같은 이름의 JSON에서 만든다(손으로 고치지 않는다).",
        "> 증거 등급 `descriptive`. 추천 정확도·클릭 예측력의 수치가 **아니다**. 기사 본문·제목·URL은 싣지 않는다"
        " — 기사 id 집합의 해시와 건수·지표만 있다.",
        "",
        "## 머리말",
        "",
        f"- 실행 명령: `{meta['command']}`",
        f"- 코드: `{meta['code']['git_sha']}` (작업 트리 {'변경 있음' if meta['code']['dirty'] else '깨끗함'}),"
        f" 실행 {meta['run_at_utc']}, Python {meta['platform']['python']}, numpy {meta['platform']['numpy']}",
        f"- 데이터: {data['source']}. 수집 창 {data['window_utc'][0]} ~ {data['window_utc'][1]} (UTC),"
        f" {data['rows']:,}건, 반출 신원 `{data['export_identity_sha256'][:16]}…`",
        f"- 임베딩: `{model['name']}` revision `{model['revision'][:12]}`, 입력 `{model['text_rule']}`,"
        f" max_length {model['max_length']}, {model['precision']}, L2 정규화. 팩 `emb.f16.npy`"
        f" sha256 `{data['pack']['files_sha256']['emb.f16.npy'][:16]}…`."
        " 팩에는 제목+본문 벡터 하나뿐이라 제목만의 벡터와는 비교하지 않았다(다시 임베딩하지 않음)",
        f"- 표본: 1차 {pr['n']:,}건(라벨이 있는 두 언론사), 언론사 라벨 {result['press_label']['n']:,}건."
        f" 불확실성: 기사 단위 백분위 부트스트랩 {meta['n_boot']:,}회(seed {prm['boot_seed']}) 95% 구간,"
        f" 무작위 기준선은 {prm['random_draws']:,}회 추첨(seed {prm['random_seed']})의 평균과 95% 범위",
        "",
        "## 무엇을 확인하고 무엇을 확인하지 않는가",
        "",
        "- 확인하는 것: BGE-M3 임베딩 공간에서 가까운 한국어 기사끼리 언론사가 붙인 섹션(주제)이 같은가."
        " 같다면 코사인 유사도가 한국어 뉴스의 주제 구조를 담는다는 뜻이다.",
        "- 확인하지 않는 것: 추천 정확도, 사용자가 코사인이 높은 기사를 클릭하는지(클릭은 쓰지 않았다),"
        " 뉴스레터(생성물) 단위의 성질, 다른 기간·다른 언론사로의 일반화.",
        "- ADR 0031 결정 5는 \"뉴스레터 500건 이상\"이라 적었다. 생성된 뉴스레터가 없어 **기사 단위의 대리 점검**이다.",
        "- 라벨은 사람이 붙인 주제 라벨이 아니라 URL 경로의 섹션이다. 섹션은 편집 부서의 분류라 주제와 어긋날 수 있다"
        " (예: 기업 기사가 `business`와 `economy`에 나뉜다). 그래서 1차는 서비스의 7개 범주로 합친 라벨로 본다.",
        "",
        "## 라벨",
        "",
        "규칙(분석 전에 코드에 고정): " + "; ".join(
            f"{p} 호스트 `{r['host']}`, 경로 `{r['path_regex']}`" for p, r in prm["section_rules"].items())
        + ". 다른 여섯 언론사는 URL에 섹션이 없어 라벨이 없다.",
        "",
        *_table(["언론사", "기사", "섹션 있음", "서비스 범주로 옮김", "1차에서 뺀 섹션"],
                [[p, c["rows"], c["with_section"], c["mapped_to_service"],
                  ", ".join(f"`{s}` {c['sections'][s]}" for s in c["unmapped_sections"]) or "-"]
                 for p, c in cov.items()]),
        "",
        "서비스 범주 대응: " + "; ".join(
            f"{p}: " + ", ".join(f"`{s}`→{c}" for s, c in m.items()) for p, m in prm["service_map"].items()) + ".",
        "",
        "## 1차 — 서비스 7개 범주, 두 언론사 합침, k = 10 leave-one-out",
        "",
        *_table(["분석", "n", "범주", "정확도", "95% 구간", "macro-F1", "95% 구간"],
                [_metric_row("kNN k=10 (코사인)", pr)]),
        "",
        *_table(["기준선", "정확도", "95% 범위", "macro-F1", "95% 범위"], _baseline_rows(pr["baselines"])),
        "",
        f"**ADR 0031 결정 5 기준** ({v['rule']}, 단위는 {v['unit']}): 정확도 {_f(pr['accuracy'])}"
        f" {'≥' if v['accuracy_ge_threshold'] else '<'} {v['threshold']},"
        f" 다수 클래스 {_f(pr['baselines']['majority']['accuracy'])}보다 {'높다' if v['above_majority'] else '높지 않다'},"
        f" n = {pr['n']:,} {'≥' if v['n_ge_500'] else '<'} 500 → **{verdict_label(v)}**."
        f" 정확도 구간의 하한 {_f(pr['ci95']['accuracy'][0])}은 기준 {'이상' if v['ci_lower_ge_threshold'] else '미만'}이다"
        " (기준은 점추정에 건 것이다). 등록한 단위(뉴스레터 500건 이상)의 기준은 이 점검으로 채워지지 않는다(미측정).",
        "",
        "범주별(1차):",
        "",
        *_table(["범주", "기사", "예측된 수", "정밀도", "재현율", "F1"],
                [[c["label"], c["support"], c["predicted"], _f(c["precision"], 3), _f(c["recall"], 3), _f(c["f1"], 3)]
                 for c in pr["per_class"]]),
        "",
        "혼동 행렬(행 = 실제, 열 = 예측, 순서는 위 표와 같다):",
        "",
        *_table(["실제 \\ 예측", *pr["classes"]],
                [[c, *row] for c, row in zip(pr["classes"], pr["confusion"])]),
        "",
        "## 서술 — 판정에 쓰지 않는다",
        "",
        "### 민감도",
        "",
        *_table(["분석", "n", "범주", "정확도", "95% 구간", "macro-F1", "95% 구간"],
                [_metric_row("1차 그대로 (k=10)", pr),
                 _metric_row(f"코사인 ≥ {prm['near_dup_cos']}인 이웃을 뺌", result["sensitivity"]["near_dup_excluded"]),
                 *[_metric_row(f"k={kk}", result["sensitivity"][f"k{kk}"]) for kk in prm["k_sensitivity"]]]),
        "",
        f"가장 가까운 이웃(자기 자신 제외)의 코사인: p05 {_f(pr['top1_cos']['p05'], 3)},"
        f" 중앙값 {_f(pr['top1_cos']['p50'], 3)}, p95 {_f(pr['top1_cos']['p95'], 3)}, p99 {_f(pr['top1_cos']['p99'], 3)}."
        f" 최근접 코사인이 {prm['near_dup_cos']} 이상인 기사 {pr['top1_cos']['share_ge_near_dup']:.1%}"
        " (거의 같은 기사 — 같은 기사의 재게재나 통신 기사로 보인다. 원문을 보지 않았으므로 확인하지 않았다).",
        "",
        "### 언론사별 원래 섹션 (범주로 합치지 않음)",
        "",
        f"섹션이 {prm['min_section_n']}건 미만이면 뺐다.",
        "",
        *_table(["언론사", "n", "섹션", "정확도", "95% 구간", "macro-F1", "95% 구간", "다수 클래스", "뺀 섹션"],
                [[p, r["n"], len(r["classes"]), _f(r["accuracy"]), _ci(r["ci95"]["accuracy"]), _f(r["macro_f1"]),
                  _ci(r["ci95"]["macro_f1"]),
                  f"{_f(r['baselines']['majority']['accuracy'])} (`{r['baselines']['majority']['label']}`)",
                  ", ".join(f"`{s}` {c}" for s, c in r["dropped_sections"].items()) or "-"]
                 for p, r in result["within_press_sections"].items()]),
        "",
        "### 언론사를 건너는 예측 (서비스 범주, 이웃은 다른 언론사의 기사만)",
        "",
        "한 언론사 안의 문체·형식이 아니라 주제가 이웃을 정하는지 보려는 것이다. 이웃 쪽 언론사에 없거나 이웃 쪽 기사가"
        " k/2보다 적은 범주는 거의 맞힐 수 없다(k개 이웃 중 그 범주의 표가 이웃 쪽 건수를 넘지 못한다). 그런 범주가 있으면"
        " macro-F1이 그만큼 낮아진다. 질의 쪽 건수가 적은 범주도 macro-F1을 크게 흔든다(범주마다 같은 무게).",
        "",
        *_table(["질의 ← 이웃", "n (질의)", "n (이웃)", "정확도", "95% 구간", "macro-F1", "95% 구간", "다수 클래스(질의)"],
                [[key.replace("<-", " ← "), r["n"], r["n_ref"], _f(r["accuracy"]), _ci(r["ci95"]["accuracy"]),
                  _f(r["macro_f1"]), _ci(r["ci95"]["macro_f1"]), _f(r["baselines"]["majority"]["accuracy"])]
                 for key, r in result["cross_press"].items()]),
        "",
        "이웃 쪽 언론사의 범주별 기사 수(건수만):",
        "",
        *_table(["질의 ← 이웃", *next(iter(result["cross_press"].values()))["classes"]],
                [[key.replace("<-", " ← "), *[counts[c] for c in result["cross_press"][key]["classes"]]]
                 for key, counts in reference_class_counts(result).items()]),
        "",
        "이웃 쪽 기사가 k/2 미만인 범주: " + ("; ".join(
            f"{s['pair'].replace('<-', ' ← ')}의 `{s['label']}`(이웃 {s['n_ref']}건, 질의 {s['support']}건 중 맞힌 수"
            f" {s['correct']})" for s in sparse_reference_classes(result)) or "없음") + ".",
        "",
        f"### 언론사 라벨 ({len(result['press_label']['classes'])}개 언론사 전체)",
        "",
        "주제가 아니라 언론사를 맞히는 비율이다. 높으면 임베딩에 언론사(문체·형식·주력 분야)의 신호도 있다는 뜻이고,"
        " 그 자체로 좋거나 나쁜 값이 아니다.",
        "",
        *_table(["분석", "n", "언론사", "정확도", "95% 구간", "macro-F1", "95% 구간"],
                [_metric_row("kNN k=10 (코사인)", result["press_label"])]),
        "",
        *_table(["기준선", "정확도", "95% 범위", "macro-F1", "95% 범위"],
                _baseline_rows(result["press_label"]["baselines"])),
        "",
        "## 다른 수치와의 관계",
        "",
        "- 덴마크어 점검(ADR 0013, `reports/recsys/ebnerd_v1.md` 3절): EB-NeRD 기사 20,738건, 카테고리 25개,"
        " kNN(k=10) LOO 0.8458, 다수 클래스 0.2096. 같은 kNN 규칙이지만 언어·라벨 체계·범주 수·다수 클래스 비율이 달라"
        " 두 값을 바로 비교하지 않는다.",
        "- 팀 뉴스레터 47건 점검(ADR 0019 등록 후 보완, `reports/recsys/team_repro_v1.md`): 직접 라벨 47건, 카테고리 7개,"
        " kNN(k=5) LOO 29.8%(무작위 약 14.3%, 9개 k 중 최선값). 단위(팀 시절의 생성 뉴스레터)·라벨 출처(온보딩 노출 로그)·"
        "표본 크기가 이 리포트와 다르다. 이 리포트는 그 값이 맞았는지 틀렸는지를 판정하지 않는다.",
        "",
        "## 한계",
        "",
        "- 라벨이 있는 것은 8개 언론사 중 2곳(매일경제·동아일보)이다. 1차의 범주 분포는 두 언론사의 편집 구조를 따른다"
        " (1차 " + ", ".join(f"{g} {x['n']:,}건(정확도 {_f(x['accuracy'], 3)})" for g, x in pr["by_group"].items())
        + ", 언론사별 정확도는 서술용).",
        "- 수집 창은 5일이다. 같은 사건의 기사가 여럿 있어 이웃이 \"같은 사건\"이기 쉽다 — 그만큼 정확도가 주제 구조보다"
        " 사건 단위 중복을 반영할 수 있다. 거의 같은 기사를 뺀 민감도가 그 일부만 본다.",
        "- 부트스트랩은 (실제, 예측) 쌍을 다시 뽑는다. 이웃 집합의 변동과 날짜·사건 단위의 군집은 구간에 들어 있지 않아"
        " 구간이 실제보다 좁을 수 있다.",
        "- 임베딩 입력이 제목 + 본문 앞 8,000자다. 서빙에서 뉴스레터를 어떤 텍스트로 임베딩하는지와 다를 수 있다.",
        "",
        "## 쓸 수 있는 문장과 쓰면 안 되는 문장",
        "",
        "- 쓸 수 있다: \"한국어 수집 기사 "
        f"{pr['n']:,}건에서 BGE-M3 코사인 kNN(k=10)이 언론사 섹션 기반 {len(pr['classes'])}개 범주를 leave-one-out 정확도"
        f" {_f(pr['accuracy'], 3)} {_ci(pr['ci95']['accuracy'], 3)}로 맞혔다(다수 클래스"
        f" {_f(pr['baselines']['majority']['accuracy'], 3)}). ADR 0031 결정 5의 기사 단위 대리 기준은"
        f" {'충족' if v['met'] else '불충족'}이다.\"",
        "- 쓰면 안 된다: \"한국어 추천에 전이됨을 확인했다\", \"코사인 피처가 한국어 클릭을 예측한다\","
        " \"뉴스레터 단위에서 확인했다\". 이 점검은 클릭도 뉴스레터도 보지 않았다.",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def dump_json(result: Mapping[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="반출본에서 점검을 돌려 JSON과 md를 쓴다")
    r.add_argument("--export", required=True, type=Path)
    r.add_argument("--out-json", required=True, type=Path)
    r.add_argument("--out-md", required=True, type=Path)
    m = sub.add_parser("render", help="JSON에서 md를 다시 만든다")
    m.add_argument("--json", required=True, type=Path)
    m.add_argument("--out-md", required=True, type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "run":
        command = ("python -m evaluation.recsys.kr_embedding_check run --export data/exports/"
                   f"{args.export.resolve().name} --out-json {args.out_json} --out-md {args.out_md}")
        result = run(args.export, command=command)
        _write(args.out_json, dump_json(result))
        _write(args.out_md, render_markdown(result))
        pr = result["primary"]
        print(json.dumps({"n": pr["n"], "accuracy": round(pr["accuracy"], 4),
                          "macro_f1": round(pr["macro_f1"], 4), "met": pr["verdict"]["met"]}, ensure_ascii=False))
        return 0
    result = json.loads(args.json.read_text(encoding="utf-8"))
    _write(args.out_md, render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
