"""E15의 스태킹(D): 시간 전진(forward-chaining) 교차 적합으로 만든 신경망 점수 피처 (ADR 0013 A3.4). numpy 전용.

fit 창을 시간 블록으로 나누고, 블록 j의 행은 블록 < j의 행만으로 학습한 모델이 채점한다. 첫 블록의 행은 점수가 없어
스태커 학습에서 빠진다. 유저 단위 폴드를 쓰지 않는 이유: 기사는 모든 유저가 공유하므로 폴드 모델이 그 행 시각 이후의
기사별 클릭률을 담을 수 있고, 그러면 fit 행 점수에만 미래 정보가 들어간다.

학습과 채점은 호출부가 함수로 넘긴다(신경망 없이 이 규칙만 테스트할 수 있게). 시간 전진의 확인은 이 모듈이 넘겨준 행 목록
(계획)이 아니라 **모델이 자기 학습 입력에서 직접 적어 둔 기록**(요청 수, 요청 시각의 최댓값)으로 한다 — 넘겨받은 행을 무시하고
더 많은 행으로 학습한 모델은 점수에 그 사실이 드러나지 않으므로, 계획만 다시 읽는 확인으로는 잡히지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from recsys_core import expand_ranges

from ..cold_transforms import average_rank01


class LeakageError(AssertionError):
    pass


def block_edges(window: tuple[int, int], block_hours: float, hour: int = 3600) -> np.ndarray:
    """[시작, 끝) 창을 시작 시각부터 block_hours씩 자른 경계. 마지막 블록은 창 끝에서 잘린다."""
    start, end = int(window[0]), int(window[1])
    step = int(block_hours * hour)
    if end <= start or step <= 0:
        raise ValueError("창과 블록 길이가 올바르지 않습니다")
    edges = list(range(start, end, step)) + [end]
    return np.asarray(edges, dtype=np.int64)


def block_of(times: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """요청 시각 -> 블록 번호(1부터). 창 밖이면 0."""
    times = np.asarray(times, dtype=np.int64)
    b = np.searchsorted(edges, times, side="right")
    return np.where((times >= edges[0]) & (times < edges[-1]), b, 0)


@dataclass
class ForwardChain:
    scores: np.ndarray           # [후보 쌍] float32, 점수 없는 행은 NaN
    scored: np.ndarray           # [후보 쌍] bool
    train_max_time: np.ndarray   # [요청] int64 — 그 요청을 채점한 모델이 기록한 학습 요청 시각의 최댓값(없으면 -1)
    block: np.ndarray            # [요청] 블록 번호
    models: list                 # 블록 j(2..)를 채점한 모델들의 기록(호출부의 train이 돌려준 것)
    audit: list = field(default_factory=list)    # 블록 모델마다: 모델이 기록한 학습 요청 수·최대 시각과 채점 대상의 최소 시각


def assert_trained_before(record: Mapping, expected_requests: int, target_min_time: int, what: str,
                          expected_pairs: Optional[int] = None) -> dict:
    """모델의 기록(observe가 돌려준 것)이 "정해진 요청만으로 학습했고 그 요청이 전부 채점 대상보다 이르다"와 맞는지 본다.

    record: {"n_requests": 학습 입력의 요청 수, "max_time": 그 요청 시각의 최댓값, ["stats_pairs": 표준화 통계를 계산한 행 수]}.
    기록이 없으면 확인할 수 없으므로 통과가 아니다.
    """
    n, t = record.get("n_requests"), record.get("max_time")
    if n is None or t is None:
        raise LeakageError(f"{what}: 모델에 학습 입력의 기록(요청 수, 최대 시각)이 없어 시간 전진을 확인할 수 없습니다")
    if int(n) != int(expected_requests):
        raise LeakageError(f"{what}: 모델이 기록한 학습 요청 수 {int(n)}가 정해진 {int(expected_requests)}와 다릅니다")
    if int(t) >= int(target_min_time):
        raise LeakageError(f"{what}: 모델이 기록한 학습 요청의 최대 시각 {int(t)}가 채점 대상의 최소 시각 "
                           f"{int(target_min_time)}보다 이르지 않습니다")
    out = {"train_requests": int(n), "train_max_time": int(t), "target_min_time": int(target_min_time)}
    if record.get("stats_pairs") is not None:
        if expected_pairs is not None and int(record["stats_pairs"]) != int(expected_pairs):
            raise LeakageError(f"{what}: 표준화 통계를 계산한 행 수 {int(record['stats_pairs'])}가 학습 행 수 "
                               f"{int(expected_pairs)}와 다릅니다")
        out["stats_pairs"] = int(record["stats_pairs"])
    return out


def forward_chain_scores(req_time: np.ndarray, cand_ptr: np.ndarray, edges: np.ndarray,
                         train: Callable[[np.ndarray], object],
                         score: Callable[[object, np.ndarray], np.ndarray],
                         observe: Callable[[object], Mapping]) -> ForwardChain:
    """블록 j >= 2의 요청을 블록 < j의 요청만으로 학습한 모델로 채점한다.

    train(rows) -> model: rows(요청 번호, 오름차순)만으로 학습.
    score(model, rows) -> rows의 후보 쌍 점수(요청 순서, 요청 안에서는 원래 순서).
    observe(model) -> 모델이 학습 입력에서 직접 적어 둔 기록(assert_trained_before의 record). 채점하기 전에 이 기록이
    "블록 < j의 요청 전부, 그 최대 시각 < 블록 j의 최소 시각"과 맞는지 보고, 아니면 LeakageError다.
    """
    req_time = np.asarray(req_time, dtype=np.int64)
    cand_ptr = np.asarray(cand_ptr, dtype=np.int64)
    block = block_of(req_time, edges)
    n_blocks = len(edges) - 1
    scores = np.full(int(cand_ptr[-1]), np.nan, dtype=np.float32)
    train_max = np.full(len(req_time), -1, dtype=np.int64)
    models, audit = [], []
    for j in range(2, n_blocks + 1):
        target = np.flatnonzero(block == j)
        source = np.flatnonzero((block >= 1) & (block < j))
        if len(target) == 0 or len(source) == 0:
            continue
        model = train(source)
        train_pairs = int((cand_ptr[source + 1] - cand_ptr[source]).sum())
        rec = assert_trained_before(observe(model), len(source), int(req_time[target].min()), f"블록 {j} 모델",
                                    expected_pairs=train_pairs)
        models.append(model)
        s = np.asarray(score(model, target), dtype=np.float32)
        _, pos = expand_ranges(cand_ptr[target], cand_ptr[target + 1])
        if len(s) != len(pos):
            raise ValueError("score가 돌려준 점수의 수가 대상 요청의 후보 수와 다릅니다")
        scores[pos] = s
        train_max[target] = rec["train_max_time"]
        audit.append({"block": j, **rec, "train_pairs": train_pairs, "target_requests": int(len(target))})
    chain = ForwardChain(scores=scores, scored=~np.isnan(scores), train_max_time=train_max, block=block, models=models,
                         audit=audit)
    assert_forward_only(chain, req_time, cand_ptr)
    return chain


def assert_forward_only(chain: ForwardChain, req_time: np.ndarray, cand_ptr: np.ndarray) -> dict:
    """모든 채점된 fit 행의 점수가 그 행의 요청 시각보다 이른 요청만으로 학습한 모델에서 나왔는지 본다.
    train_max_time은 forward_chain_scores가 모델의 기록에서 옮겨 적은 값이다."""
    req_time = np.asarray(req_time, dtype=np.int64)
    pair_req = np.repeat(np.arange(len(req_time)), np.diff(cand_ptr))
    scored_req = np.zeros(len(req_time), dtype=bool)
    scored_req[pair_req[chain.scored]] = True
    bad = scored_req & ((chain.train_max_time < 0) | (chain.train_max_time >= req_time))
    if bad.any():
        raise LeakageError(f"시간 전진 위반: {int(bad.sum())}개 요청이 자기 시각 이후의 요청으로 학습한 모델에 채점됐습니다")
    first = chain.block <= 1
    if chain.scored[first[pair_req]].any():
        raise LeakageError("첫 블록(또는 창 밖)의 행에 점수가 있습니다")
    margin = (req_time - chain.train_max_time)[scored_req]
    return {"scored_requests": int(scored_req.sum()), "unscored_requests": int((~scored_req).sum()),
            "min_seconds_after_training_data": int(margin.min()) if len(margin) else None,
            "blocks": int(chain.block.max()) if len(chain.block) else 0}


def stacker_rows(chain: ForwardChain, cand_ptr: np.ndarray) -> np.ndarray:
    """스태커 학습에 쓰는 후보 쌍(bool): 점수가 있는 요청의 행."""
    return chain.scored.copy()


def with_neural_rank(feats: pd.DataFrame, scores: np.ndarray, cand_ptr: np.ndarray, column: str) -> pd.DataFrame:
    """신경망 점수를 요청 안 평균 순위(0~1)로 바꿔 열로 더한 사본. 점수가 NaN인 행은 NaN으로 남는다."""
    out = feats.copy()
    out[column] = average_rank01(np.asarray(scores, dtype=np.float64), cand_ptr).astype(np.float32)
    return out


def blocks_mask(block: np.ndarray, cand_ptr: np.ndarray, keep: Sequence[int]) -> np.ndarray:
    """주어진 블록에 속한 요청의 후보 쌍(bool)."""
    return np.repeat(np.isin(block, list(keep)), np.diff(cand_ptr))
