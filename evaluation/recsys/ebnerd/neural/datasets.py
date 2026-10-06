"""E15 신경망 arm의 배치: 가변 길이 후보 그룹을 길이별로 묶어 패딩한다 (ADR 0013 A3.3). numpy 전용.

과제(RankTask)의 후보·라벨 배열을 그대로 들고 있고, 배치는 그룹의 순서만 바꾼다. 그래서 네거티브는 epoch 사이에 바뀌지
않는다(신경망이 GBDT보다 더 많은 네거티브를 보지 않게 하려는 규칙이다).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from recsys_core import expand_ranges

from ..prepare import RankTask
from .sequences import ScalarBlock, Sequences


@dataclass
class NeuralInputs:
    """한 과제 분할의 신경망 입력. 행 순서는 RankTask의 후보 쌍 순서와 같다."""
    cand_ptr: np.ndarray
    cand_item: np.ndarray
    labels: np.ndarray
    cont: np.ndarray
    category: np.ndarray
    gender: np.ndarray
    seq: Sequences
    aux_neg: Optional[np.ndarray] = None       # [로그 이벤트 수, K] — 보조 손실을 쓰는 학습 입력에만
    req_time: Optional[np.ndarray] = None      # [요청] — 모델에 넣지 않는다. 학습 함수가 "무엇으로 학습했는가"를 적는 데만 쓴다

    @property
    def n_groups(self) -> int:
        return len(self.cand_ptr) - 1

    @property
    def n_candidates(self) -> np.ndarray:
        return np.diff(self.cand_ptr)

    def subset(self, rows: np.ndarray) -> "NeuralInputs":
        """요청 일부만 담은 입력(블록 모델, 결정론 게이트의 서브샘플)."""
        rows = np.asarray(rows, dtype=np.int64)
        _, pairs = expand_ranges(self.cand_ptr[rows], self.cand_ptr[rows + 1])
        counts = self.cand_ptr[rows + 1] - self.cand_ptr[rows]
        return NeuralInputs(cand_ptr=np.concatenate([[0], np.cumsum(counts)]), cand_item=self.cand_item[pairs],
                            labels=self.labels[pairs], cont=self.cont[pairs], category=self.category[pairs],
                            gender=self.gender[pairs], seq=self.seq.subset(rows), aux_neg=self.aux_neg,
                            req_time=None if self.req_time is None else self.req_time[rows])


def build_inputs(task: RankTask, block: ScalarBlock, seq: Sequences, aux_neg: Optional[np.ndarray] = None) -> NeuralInputs:
    """RankTask와 같은 후보·라벨 배열(사본 아님)에 스칼라 블록과 시퀀스를 붙인다."""
    n_pairs = len(task.req.cand_item)
    if not (len(block.cont) == len(block.category) == len(block.gender) == n_pairs == len(task.labels)):
        raise ValueError("스칼라 블록의 행 수가 과제의 후보 쌍 수와 다릅니다")
    if len(seq.items) != task.req.n:
        raise ValueError("시퀀스의 행 수가 요청 수와 다릅니다")
    return NeuralInputs(cand_ptr=task.req.cand_ptr, cand_item=task.req.cand_item, labels=task.labels, cont=block.cont,
                        category=block.category, gender=block.gender, seq=seq, aux_neg=aux_neg, req_time=task.req.time)


def group_batches(n_candidates: np.ndarray, batch_groups: int, seed: int, epoch: int,
                  sort_window: int = 32) -> list[np.ndarray]:
    """학습용 배치(그룹 번호 배열의 목록). 후보가 없는 그룹은 뺀다.

    그룹을 섞은 뒤 batch_groups × sort_window개씩 끊어 그 안에서 길이순으로 정렬해 배치로 나누고(패딩을 줄인다),
    배치 순서를 다시 섞는다. 난수는 (seed, epoch)에만 달려 있어 같은 인자면 같은 순서다.
    """
    rng = np.random.default_rng([int(seed), int(epoch)])
    groups = np.flatnonzero(np.asarray(n_candidates) > 0)
    groups = groups[rng.permutation(len(groups))]
    batches: list[np.ndarray] = []
    window = batch_groups * sort_window
    for a in range(0, len(groups), window):
        chunk = groups[a:a + window]
        chunk = chunk[np.argsort(n_candidates[chunk], kind="stable")]
        batches += [chunk[i:i + batch_groups] for i in range(0, len(chunk), batch_groups)]
    return [batches[i] for i in rng.permutation(len(batches))]


def eval_batches(n_candidates: np.ndarray, max_pairs: int = 65536) -> list[np.ndarray]:
    """채점용 배치: 길이순으로 세워 (그룹 수 × 그 배치의 최대 길이)가 max_pairs를 넘지 않게 끊는다. 결정론적이다."""
    n_candidates = np.asarray(n_candidates)
    groups = np.flatnonzero(n_candidates > 0)
    groups = groups[np.argsort(n_candidates[groups], kind="stable")]
    batches, a = [], 0
    while a < len(groups):
        b = a + 1
        while b < len(groups) and (b + 1 - a) * int(n_candidates[groups[b]]) <= max_pairs:
            b += 1
        batches.append(groups[a:b])
        a = b
    return batches


def collate(inp: NeuralInputs, groups: Sequence[int], with_aux: bool = False) -> dict[str, np.ndarray]:
    """그룹들을 한 배치로 패딩한다. pair_index는 각 칸이 원래 쌍 배열의 어느 행인지(-1 = 패딩)다."""
    groups = np.asarray(groups, dtype=np.int64)
    lo, hi = inp.cand_ptr[groups], inp.cand_ptr[groups + 1]
    rows, pos = expand_ranges(lo, hi)
    col = pos - lo[rows]
    shape = (len(groups), int((hi - lo).max()) if len(groups) else 0)
    pair_index = np.full(shape, -1, dtype=np.int64)
    pair_index[rows, col] = pos
    mask = pair_index >= 0

    def pad(values: np.ndarray, fill, dtype) -> np.ndarray:
        out = np.full(shape + values.shape[1:], fill, dtype=dtype)
        out[rows, col] = values[pos]
        return out

    batch = {
        "pair_index": pair_index, "cand_mask": mask, "cand_item": pad(inp.cand_item, -1, np.int64),
        "labels": pad(np.asarray(inp.labels, dtype=np.float32), 0.0, np.float32),
        "cont": pad(inp.cont, 0.0, np.float32), "category": pad(inp.category, 0, np.int64),
        "gender": pad(inp.gender, 0, np.int64),
        "seq_items": inp.seq.items[groups].astype(np.int64), "seq_mask": inp.seq.mask[groups],
    }
    if with_aux:
        if inp.aux_neg is None:
            raise ValueError("보조 손실용 네거티브 표가 없습니다")
        p = inp.seq.pos[groups]
        batch["aux_neg"] = np.where((p >= 0)[..., None], inp.aux_neg[np.where(p >= 0, p, 0)], -1).astype(np.int64)
    return batch
