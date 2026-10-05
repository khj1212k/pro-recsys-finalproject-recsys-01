"""P2 2단계(후보 생성 -> 랭커)의 전체 풀 대비 쌍체 차이와 리포트 표(데이터 없이)."""
from types import SimpleNamespace

import numpy as np
import pytest

from evaluation.recsys.ebnerd.make_report import two_stage_table
from evaluation.recsys.ebnerd.run_ebnerd import _two_stage


def _task():
    # 요청 3개(유저 3명), 후보 4개씩. 각 요청의 정답은 1개.
    ptr = np.array([0, 4, 8, 12])
    labels = np.zeros(12, dtype=bool)
    labels[[0, 5, 10]] = True
    return SimpleNamespace(req=SimpleNamespace(cand_ptr=ptr), labels=labels, n_pos_total=np.array([1, 1, 1]),
                           group_user=np.array([0, 1, 2]))


def test_two_stage_with_full_union_equals_full_pool_exactly():
    task = _task()
    sc = np.array([4, 3, 2, 1, 1, 4, 3, 2, 2, 1, 4, 3], dtype=float)  # 정답이 모두 1위
    res = _two_stage(task, [sc, sc], [0, 1], {50: np.ones(12, dtype=bool)}, n_boot=50)
    p = res["union@50"]["paired_vs_full_pool"]
    assert p["ndcg@10"]["diff"] == 0.0 and p["ndcg@10"]["ci95"] == [0.0, 0.0]
    assert res["union@50"]["ndcg@10"]["mean"] == pytest.approx(1.0)


def test_two_stage_paired_diff_is_negative_when_union_drops_a_top_ranked_positive():
    task = _task()
    sc = np.array([4, 3, 2, 1, 1, 4, 3, 2, 2, 1, 4, 3], dtype=float)
    mask = np.ones(12, dtype=bool)
    mask[0] = False  # 요청 0의 정답(1위)이 합집합 밖 -> 맨 뒤(4위)로
    res = _two_stage(task, [sc], [0], {50: mask}, n_boot=50)
    p = res["union@50"]["paired_vs_full_pool"]["ndcg@10"]
    want = (1 / np.log2(5) - 1.0) / 3  # 요청 0만 nDCG 1 -> 1/log2(5)
    assert p["diff"] == pytest.approx(want)
    assert p["n"] == 3


def test_two_stage_table_adds_paired_columns_only_when_present():
    stage = {"ndcg@10": {"mean": 0.27, "ci95": [0.26, 0.28]}, "recall@10": {"mean": 0.51, "ci95": [0.50, 0.52]}}
    old = two_stage_table({"p2_two_stage": {"union@50": stage}})
    assert old.splitlines()[0] == "| 후보 생성 | nDCG@10 | Recall@10 |"

    diff = {"diff": 0.0013, "ci95": [0.0008, 0.0019], "n": 20000}
    new = two_stage_table({"p2_two_stage": {"union@50": {**stage, "paired_vs_full_pool": {
        "ndcg@10": diff, "recall@10": diff}}}})
    assert "ΔnDCG@10 vs 전체 풀 (쌍체)" in new.splitlines()[0]
    assert new.splitlines()[2].endswith("| +0.0013 [+0.0008, +0.0019] | +0.0013 [+0.0008, +0.0019] |")
