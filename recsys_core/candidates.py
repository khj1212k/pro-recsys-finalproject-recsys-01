"""후보 생성 단계의 공용 연산: 그룹(요청) 내 순위와 후보 출처(source) 플래그.

요청 시점 API에서는 인기도/최신성/히스토리 코사인 같은 여러 출처가 각자 상위 k개를
내고 그 합집합을 랭커에 넘긴다. 어떤 출처가 후보를 냈는지는 랭커 피처(src_*)이자
출처별 recall 진단의 기준이라 오프라인 평가와 같은 함수를 쓴다.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd


def group_ids(ptr: np.ndarray) -> np.ndarray:
    ptr = np.asarray(ptr, dtype=np.int64)
    return np.repeat(np.arange(len(ptr) - 1, dtype=np.int64), np.diff(ptr))


def rank_within_groups(scores: np.ndarray, ptr: np.ndarray, seed: int = 0) -> np.ndarray:
    """그룹 내 내림차순 0-based 순위. 동점은 seed 고정 무작위로 깬다(입력 순서가 결과를
    좌우하면 인기도 0인 후보가 많은 출처에서 카탈로그 정렬 순서가 새어 들어간다)."""
    scores = np.asarray(scores, dtype=np.float64)
    ptr = np.asarray(ptr, dtype=np.int64)
    g = group_ids(ptr)
    tiebreak = np.random.default_rng(seed).random(len(scores))
    order = np.lexsort((tiebreak, -scores, g))
    ranks = np.empty(len(scores), dtype=np.int64)
    ranks[order] = np.arange(len(scores)) - ptr[g[order]]
    return ranks


def source_flags(sources: Mapping[str, np.ndarray], ptr: np.ndarray, k: int, seed: int = 0) -> pd.DataFrame:
    """출처별 점수(클수록 앞)로 요청마다 상위 k개에 든 후보를 표시한다.

    반환 열: src_<이름>(0/1)과 src_count(해당 후보를 낸 출처 수). 합집합 후보 = src_count > 0.
    """
    ptr = np.asarray(ptr, dtype=np.int64)
    n = int(ptr[-1])
    cols: dict[str, np.ndarray] = {}
    count = np.zeros(n, dtype=np.float32)
    for name, scores in sources.items():
        scores = np.asarray(scores)
        if len(scores) != n:
            raise ValueError(f"source {name!r} 길이 {len(scores)} != 후보 수 {n}")
        flag = (rank_within_groups(scores, ptr, seed=seed) < k).astype(np.float32)
        cols[f"src_{name}"] = flag
        count += flag
    cols["src_count"] = count
    return pd.DataFrame(cols)
