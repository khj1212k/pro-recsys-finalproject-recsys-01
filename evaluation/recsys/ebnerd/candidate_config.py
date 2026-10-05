"""후보 생성기 구성(E8): 하네스 구성(v1)과 서빙 구성을 같은 P2 과제 위에서 재현한다.

서빙(backend/app/recsys/pipeline.generate_candidates)은 출처마다 상위 k개를 뽑아 **라운드로빈**으로 합치고 cap에서
자른 뒤, 그다음에 이미 클릭한 아이템을 뺀다. 하네스 v1은 출처별 상위 k개의 단순 합집합이었다. 두 방식을 한 모듈에
두고, 서빙 쪽 합치기는 서빙 코드와 같은 결과를 내는지 테스트로 묶는다(구성이 어긋나면 2단계 수치가 서빙의 것이 아니다).

출처의 EB-NeRD 대응(ADR 0013 A2 사전 등록 E8):
  knn_profile = hist_cos 상위(히스토리가 있을 때만), knn_short = short_cos 상위(24h 이벤트가 있을 때만),
  recent = 최신순, popular = pop_clicks_6h 상위, category = 온보딩 대체 카테고리 안 최신순(카테고리가 있을 때만).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from recsys_core import rank_within_groups, source_flags


@dataclass(frozen=True)
class CandidateConfig:
    name: str
    window_h: float
    sources: tuple[tuple[str, int], ...]   # (출처, 상위 k) — 라운드로빈 순서
    cap: Optional[int] = None              # None이면 단순 합집합(하네스 v1)
    model: Optional[str] = None            # 2단계에서 점수를 내는 arm


def config_from_prereg(name: str, d: Mapping) -> CandidateConfig:
    """사전 등록 yaml의 e8.<name> 블록에서 구성을 만든다. order가 있으면 그 순서, 없으면 적힌 순서."""
    order = d.get("order") or list(d["sources"])
    if set(order) != set(d["sources"]):
        raise ValueError(f"{name}: order와 sources의 출처가 다릅니다")
    return CandidateConfig(name=name, window_h=float(d["window_h"]),
                           sources=tuple((s, int(d["sources"][s])) for s in order),
                           cap=d.get("cap"), model=d.get("model"))


def source_scores(feats: pd.DataFrame, names: Sequence[str]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """출처별 (점수, 후보 자격). 자격이 없는 후보는 그 출처가 내지 않는다(비활성 요청은 전부 자격 없음)."""
    n = len(feats)
    everyone = np.ones(n, dtype=bool)
    recency = -feats["hours_since_pub"].to_numpy(np.float64)
    table = {
        "popularity_6h": lambda: (feats["pop_clicks_6h"].to_numpy(np.float64), everyone),
        "popularity_24h": lambda: (feats["pop_clicks_24h"].to_numpy(np.float64), everyone),
        "recency": lambda: (recency, everyone),
        "cosine_history": lambda: (feats["hist_cos"].to_numpy(np.float64), everyone),
        "knn_profile": lambda: (feats["hist_cos"].to_numpy(np.float64), feats["hist_len"].to_numpy() > 0),
        "knn_short": lambda: (feats["short_cos"].to_numpy(np.float64), feats["short_len"].to_numpy() > 0),
        "recent": lambda: (recency, everyone),
        "popular": lambda: (feats["pop_clicks_6h"].to_numpy(np.float64), everyone),
        "category": lambda: (recency, feats["is_cat_match"].to_numpy() > 0),
    }
    unknown = [s for s in names if s not in table]
    if unknown:
        raise ValueError(f"알 수 없는 후보 출처: {unknown}")
    return {s: table[s]() for s in names}


def round_robin_union(sources: Mapping[str, Sequence[int]], cap: int) -> tuple[list[int], dict[str, int]]:
    """서빙 `_round_robin_union`과 같은 합치기: 출처를 번갈아 돌며 아직 없는 첫 아이템을 하나씩 넣고 cap에서 멈춘다."""
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


def union_mask(config: CandidateConfig, feats: pd.DataFrame, ptr: np.ndarray,
               seed: int = 0) -> tuple[np.ndarray, dict]:
    """후보 쌍마다 "합집합에 들었는가". 동점은 seed 고정 무작위로 깬다(v1 source_flags와 같은 규칙).

    cap이 없으면 v1과 같은 계산(source_flags)이고, 있으면 요청마다 서빙 방식 라운드로빈을 돌린다.
    반환 info: 평균 합집합 크기와 출처별 평균 기여 수.
    """
    ptr = np.asarray(ptr, dtype=np.int64)
    n_req = len(ptr) - 1
    names = [s for s, _ in config.sources]
    scored = source_scores(feats, names)
    if config.cap is None:
        ks = {k for _, k in config.sources}
        if len(ks) != 1 or not all(elig.all() for _, elig in scored.values()):
            raise ValueError("cap 없는 구성은 출처별 k가 같고 자격 조건이 없어야 합니다")
        flags = source_flags({s: scored[s][0] for s in names}, ptr, k=ks.pop(), seed=seed)
        mask = flags["src_count"].to_numpy() > 0
        sizes = np.add.reduceat(mask.astype(np.int64), ptr[:-1]) if n_req else np.zeros(0)
        return mask, {"mean_union_size": float(sizes.mean()) if n_req else 0.0, "cap": None}

    ranks = {}
    for (s, k) in config.sources:
        score, elig = scored[s]
        r = rank_within_groups(np.where(elig, score, -np.inf), ptr, seed=seed)
        ranks[s] = np.where(elig & (r < k), r, -1)
    mask = np.zeros(int(ptr[-1]), dtype=bool)
    contributed = {s: 0 for s in names}
    for g in range(n_req):
        a, b = int(ptr[g]), int(ptr[g + 1])
        lists = {}
        for s in names:
            r = ranks[s][a:b]
            local = np.flatnonzero(r >= 0)
            if len(local):   # 서빙처럼 신호가 없는 출처는 목록 자체가 없다
                lists[s] = local[np.argsort(r[local], kind="stable")].tolist()
        merged, contrib = round_robin_union(lists, config.cap)
        mask[a + np.asarray(merged, dtype=np.int64)] = True
        for s, c in contrib.items():
            contributed[s] += c
    sizes = np.add.reduceat(mask.astype(np.int64), ptr[:-1]) if n_req else np.zeros(0)
    return mask, {"mean_union_size": float(sizes.mean()) if n_req else 0.0, "cap": config.cap,
                  "max_union_size": int(sizes.max()) if n_req else 0,
                  "mean_contributed": {s: contributed[s] / max(n_req, 1) for s in names}}
