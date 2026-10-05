"""후보 생성 단계의 공용 연산: 그룹(요청) 내 순위와 후보 출처(source) 플래그.

요청 시점 API에서는 인기도/최신성/히스토리 코사인 같은 여러 출처가 각자 상위 k개를
내고 그 합집합을 랭커에 넘긴다. 어떤 출처가 후보를 냈는지는 랭커 피처(src_*)이자
출처별 recall 진단의 기준이라 오프라인 평가와 같은 함수를 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd


@dataclass(frozen=True)
class CandidateSpec:
    """후보 생성기 구성: 신선도 창, 출처별 상위 k(라운드로빈 순서), 합집합 상한.

    서빙(backend/app/recsys)의 기본값과 하네스의 서빙 구성(E8)이 같은 값인지 비교하는 단위다.
    """
    window_h: float
    sources: tuple[tuple[str, int], ...]
    cap: int

    def as_dict(self) -> dict:
        return {"window_h": float(self.window_h), "sources": [[s, int(k)] for s, k in self.sources],
                "cap": int(self.cap)}


# 서빙 후보 생성기의 기본 구성. backend/app/recsys/config.py의 기본값이 여기서 나오고,
# 하네스의 사전 등록 구성(evaluation/recsys/ebnerd/preregistration/cold-v1.2.yaml e8.serving)은
# 이 값과 같아야 한다(테스트와 parity 게이트가 비교한다).
SERVING_CANDIDATE_SPEC = CandidateSpec(
    window_h=72,
    sources=(("knn_profile", 100), ("knn_short", 100), ("recent", 100), ("popular", 100), ("category", 50)),
    cap=300,
)


def round_robin_union(sources: Mapping[str, Sequence[int]], cap: int) -> tuple[list[int], dict[str, int]]:
    """출처를 번갈아 돌며 아직 없는 첫 아이템을 하나씩 넣고 cap에서 멈춘다.

    한 출처(예: KNN 100개)가 상한을 독식하지 않게 하는 합치기다. 반환: (합친 목록, 출처별로 넣은 수).
    서빙과 하네스가 이 함수 하나를 쓴다.
    """
    seen: set[int] = set()
    merged: list[int] = []
    contributed = {name: 0 for name in sources}
    iters = {name: iter(ids) for name, ids in sources.items()}
    while iters and len(merged) < cap:
        for name in list(iters):
            for nid in iters[name]:
                if nid not in seen:
                    seen.add(nid)
                    merged.append(nid)
                    contributed[name] += 1
                    break
            else:
                del iters[name]
                continue
            if len(merged) >= cap:
                break
    return merged, contributed


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


def source_flags(sources: Mapping[str, np.ndarray], ptr: np.ndarray, k: int, seed: int = 0) -> "pd.DataFrame":
    """출처별 점수(클수록 앞)로 요청마다 상위 k개에 든 후보를 표시한다.

    반환 열: src_<이름>(0/1)과 src_count(해당 후보를 낸 출처 수). 합집합 후보 = src_count > 0.
    """
    import pandas as pd

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
