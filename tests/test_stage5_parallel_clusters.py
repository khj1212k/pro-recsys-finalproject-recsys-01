# Stage5가 클러스터를 제한된 스레드 풀로 병렬 처리하고(ADR 0010), 클러스터별 결과와
# split_v2 메타를 cluster_history.cluster_log에 남기는지(평가셋 추출용, ADR 0009) 검증한다.
import sys
import os
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from pipeline.stages import run_clusters_bounded, summarize_cluster_outcome


def _tracking_process(results, delay=0.02):
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "calls": []}

    def process(cid, idx):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["calls"].append(cid)
        time.sleep(delay)
        with lock:
            state["active"] -= 1
        outcome = results.get(cid, "completed")
        if isinstance(outcome, Exception):
            raise outcome
        return {"status": outcome}

    return process, state


def test_pool_never_exceeds_max_workers_and_processes_every_cluster():
    process, st = _tracking_process({})
    attempt = [(cid, i) for i, cid in enumerate(range(10))]

    outcomes = run_clusters_bounded(process, attempt, [], max_workers=3, min_target=0)

    assert st["peak"] <= 3
    assert st["peak"] > 1  # 실제로 병렬로 돌았다
    assert sorted(outcomes) == list(range(10))


def test_min_target_fill_stops_exactly_at_target_without_overshoot():
    process, st = _tracking_process({0: "skipped", 1: "completed"})
    attempt = [(0, 0), (1, 1)]
    fill = [(cid, cid) for cid in range(2, 12)]

    outcomes = run_clusters_bounded(process, attempt, fill, max_workers=4, min_target=3)

    completed = [c for c, o in outcomes.items() if o["status"] == "completed"]
    assert len(completed) == 3
    assert len(st["calls"]) == 4  # 첫 2개 + 부족분(2개)만큼만 보충 - 워커 수(4)만큼 넘쳐 돌리지 않는다


def test_one_cluster_raising_does_not_abort_the_others():
    process, _ = _tracking_process({2: RuntimeError("boom")})

    outcomes = run_clusters_bounded(process, [(c, c) for c in range(4)], [], max_workers=2, min_target=0)

    assert outcomes[2]["status"] == "error"
    assert "boom" in outcomes[2]["error"]
    assert [outcomes[c]["status"] for c in (0, 1, 3)] == ["completed"] * 3


def test_summarize_outcome_classifies_final_states():
    assert summarize_cluster_outcome({"completed_newsletters": [9]}, 1)["status"] == "completed"
    skipped = summarize_cluster_outcome(
        {"skipped_clusters": [1], "cluster_eval": {"decision": "FAIL", "confidence": 0.8}}, 1
    )
    assert skipped["status"] == "skipped"
    assert skipped["cluster_eval"] == {"decision": "FAIL", "confidence": 0.8}
    failed = summarize_cluster_outcome({"failed_clusters": [1], "failure_reason": "faithfulness"}, 1)
    assert failed["status"] == "failed" and failed["failure_reason"] == "faithfulness"


def test_stage5_persists_outcomes_and_split_meta_to_cluster_log():
    from pipeline.stages import Stage5_NewsletterGeneration

    def fake_invoke(state):
        cid = state["all_cluster_ids"][state["current_cluster_index"]]
        if cid == 1:
            return {"skipped_clusters": [1], "cluster_eval": {"decision": "FAIL", "confidence": 0.7}}
        return {"completed_newsletters": [500 + cid], "cluster_eval": {"decision": "PASS", "confidence": 0.9}}

    fake_app = MagicMock()
    fake_app.invoke.side_effect = fake_invoke
    fake_clusterer = MagicMock()
    fake_clusterer.cluster_news.return_value = {0: [10, 11, 12], 1: [13, 14, 15], 2: [16, 17, 18]}
    fake_clusterer.cluster_meta = {0: {"split_v2": False}, 1: {"split_v2": True}, 2: {"split_v2": False}}
    fake_clusterer.labels_ = np.array([0, 0, 0, 1, 1, 1, 2, 2, 2])
    fake_clusterer.get_clustered_articles.return_value = {}

    settings = MagicMock()
    settings.NEWSLETTER_WORKERS = 3
    settings.MIN_NEWSLETTER_TARGET = 0
    settings.HDBSCAN_MIN_CLUSTER_SIZE = 3
    settings.HDBSCAN_MIN_SAMPLES = 2
    settings.CLUSTER_LOOKBACK_HOURS = 24

    with patch("core.clusterer.NewsClusterer", return_value=fake_clusterer), \
         patch("workflow.graph.compile_workflow", return_value=fake_app), \
         patch("core.llm_metrics.get_metrics_collector", return_value=MagicMock()), \
         patch("db.batch_manager.create_new_batch", return_value=77) as create_batch, \
         patch("db.batch_manager.update_cluster_log", return_value=True) as update_log:
        count = Stage5_NewsletterGeneration(settings=settings).execute()

    assert count == 2
    assert create_batch.call_args[0][0]["cluster_meta"] == fake_clusterer.cluster_meta
    run_id, log = update_log.call_args[0]
    assert run_id == 77
    assert log["cluster_outcomes"][1]["status"] == "skipped"
    assert log["cluster_outcomes"][1]["cluster_eval"]["decision"] == "FAIL"
    assert log["cluster_outcomes"][2]["newsletter_id"] == 502
    assert log[0] == [10, 11, 12]  # 클러스터 원본 매핑도 그대로 유지


def test_clusterer_records_which_final_groups_came_from_a_split_v2(monkeypatch):
    import core.clustering.hdbscan_clusterer as hc

    class Decision:
        def __init__(self, split):
            self.should_split = split
            self.reason = "r"
            self.debug = {"idx0": [0, 1, 2], "idx1": [3, 4, 5]} if split else {}

    decisions = iter([Decision(True), Decision(False)])
    monkeypatch.setattr(hc, "decide_split_v2", lambda X, titles: next(decisions))
    clusterer = hc.NewsClusterer()
    monkeypatch.setattr(clusterer, "fit_predict", lambda emb: np.array([0] * 6 + [1] * 3))
    data = {"embeddings": np.zeros((9, 4)), "titles": [f"t{i}" for i in range(9)], "ids": np.arange(100, 109)}

    groups = clusterer.cluster_with_split(data)

    assert [g[0] for g in groups] == [[100, 101, 102], [103, 104, 105], [106, 107, 108]]
    assert [m["split_v2"] for m in clusterer.group_meta_] == [True, True, False]
