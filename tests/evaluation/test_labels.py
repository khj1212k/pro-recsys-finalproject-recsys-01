# 라벨 저장소(evaluation/llm/labels.py)와 로컬 라벨링 UI(evaluation/llm/labeling_app.py).
import json
from datetime import datetime, timedelta, timezone

import pytest

from evaluation.llm.labels import LabelError, LabelStore, load_labels

T0 = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
CONTENT = "삼성전자는 평택 공장에 3조 원을 투자한다. D램 가격은 10% 올랐다."


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def labels_dir(tmp_path):
    clusters = [
        {"item_id": "r1-c0", "articles": [{"raw_news_id": i, "press_name": "테스트일보", "title": f"t{i}",
                                          "body": f"b{i}", "url": f"u{i}"} for i in (1, 2, 3)]},
        {"item_id": "r1-c1", "articles": [{"raw_news_id": i, "press_name": "테스트일보", "title": f"t{i}",
                                          "body": f"b{i}", "url": f"u{i}"} for i in (4, 5, 6)]},
    ]
    outputs = [
        {"output_id": "o2", "item_id": "r1-c1", "draft": {"title": "t", "sentence": "s", "content": CONTENT},
         "converted": {"title": "ct", "summary": "cs", "content": "cc"}},
        {"output_id": "o1", "item_id": "r1-c0", "draft": {"title": "HBM 투자", "sentence": "s", "content": CONTENT},
         "converted": {"title": "ct", "summary": "cs", "content": "cc"}},
    ]
    for name, rows in (("clusters.jsonl", clusters), ("outputs.jsonl", outputs)):
        (tmp_path / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    (tmp_path / "relabel.json").write_text(json.dumps(
        {"outputs": ["o1"], "clusters": ["r1-c0"], "min_hours_between_rounds": 48}), encoding="utf-8")
    return tmp_path


def _cluster_label(ids=(1, 2, 3), n=3, single=True):
    return {"single_event": single, "outlier_ids": [],
            "key_facts": [{"text": f"사실 {k}", "source_ids": [ids[k % len(ids)]]} for k in range(n)]}


def _output_label(**kw):
    return {"fact_errors": [], "key_facts_covered": [0], "style": 4, "publishable": True, "tone_drift": False, **kw}


def test_all_clusters_come_before_any_output_and_outputs_keep_the_blind_order(labels_dir):
    s = LabelStore(labels_dir, now=Clock(T0))
    assert s.next_task(1) == {"kind": "cluster", "target_id": "r1-c0"}
    s.save("cluster", "r1-c0", 1, _cluster_label())
    assert s.next_task(1) == {"kind": "cluster", "target_id": "r1-c1"}
    s.save("cluster", "r1-c1", 1, _cluster_label(ids=(4, 5, 6)))
    assert s.next_task(1) == {"kind": "output", "target_id": "o2"}  # export 순서 그대로


def test_cluster_label_rules(labels_dir):
    s = LabelStore(labels_dir, now=Clock(T0))
    with pytest.raises(LabelError, match="3~6"):
        s.save("cluster", "r1-c0", 1, _cluster_label(n=2))
    with pytest.raises(LabelError, match="없는 기사 id"):
        s.save("cluster", "r1-c0", 1, _cluster_label(ids=(1, 99)))
    with pytest.raises(LabelError):
        s.save("cluster", "r1-c0", 1, _cluster_label(n=7))
    # 단일 사건이 아니면 핵심 사실은 선택
    s.save("cluster", "r1-c0", 1, {"single_event": False, "outlier_ids": [3], "key_facts": []})


def test_output_needs_its_cluster_label_first_and_valid_fact_indexes(labels_dir):
    s = LabelStore(labels_dir, now=Clock(T0))
    with pytest.raises(LabelError, match="클러스터 라벨"):
        s.save("output", "o1", 1, _output_label())
    s.save("cluster", "r1-c0", 1, _cluster_label())
    with pytest.raises(LabelError, match="핵심 사실 번호"):
        s.save("output", "o1", 1, _output_label(key_facts_covered=[3]))
    s.save("output", "o1", 1, _output_label(key_facts_covered=[0, 2]))


def test_fact_error_span_must_match_the_draft_text(labels_dir):
    s = LabelStore(labels_dir, now=Clock(T0))
    s.save("cluster", "r1-c0", 1, _cluster_label())
    start = CONTENT.index("3조 원")
    good = {"field": "content", "start": start, "end": start + 4, "text": "3조 원", "type": "number"}
    s.save("output", "o1", 1, _output_label(fact_errors=[good]))
    with pytest.raises(LabelError, match="구간"):
        s.save("output", "o1", 1, _output_label(fact_errors=[{**good, "start": start + 1, "end": start + 5}]))


def test_relabel_accepts_only_listed_targets_after_the_minimum_interval(labels_dir):
    clock = Clock(T0)
    s = LabelStore(labels_dir, now=clock)
    s.save("cluster", "r1-c0", 1, _cluster_label())
    s.save("cluster", "r1-c1", 1, _cluster_label(ids=(4, 5, 6)))

    clock.t = T0 + timedelta(hours=47)
    assert s.next_task(2) is None
    with pytest.raises(LabelError, match="간격"):
        s.save("cluster", "r1-c0", 2, _cluster_label(n=4))

    clock.t = T0 + timedelta(hours=48)
    assert s.next_task(2) == {"kind": "cluster", "target_id": "r1-c0"}
    with pytest.raises(LabelError, match="재라벨 대상"):
        s.save("cluster", "r1-c1", 2, _cluster_label(ids=(4, 5, 6)))
    s.save("cluster", "r1-c0", 2, _cluster_label(n=4))

    labels = load_labels(labels_dir)
    assert len(labels["cluster"][1]["r1-c0"]["key_facts"]) == 3
    assert len(labels["cluster"][2]["r1-c0"]["key_facts"]) == 4


def test_relabeling_within_a_round_keeps_history_and_latest_wins(labels_dir):
    s = LabelStore(labels_dir, now=Clock(T0))
    s.save("cluster", "r1-c0", 1, _cluster_label())
    s.save("cluster", "r1-c0", 1, {**_cluster_label(), "single_event": False})
    assert load_labels(labels_dir)["cluster"][1]["r1-c0"]["single_event"] is False
    assert len((labels_dir / "cluster_labels.jsonl").read_text(encoding="utf-8").splitlines()) == 2


class TestApp:
    @pytest.fixture
    def client(self, labels_dir):
        from fastapi.testclient import TestClient

        from evaluation.llm.labeling_app import create_app

        self.clock = Clock(T0)
        return TestClient(create_app(labels_dir, now=self.clock))

    def test_index_page_is_served(self, client):
        r = client.get("/")
        assert r.status_code == 200 and "뉴스레터 라벨링" in r.text

    def test_label_flow_and_validation_errors(self, client):
        task = client.get("/api/next?round=1").json()["task"]
        assert task["kind"] == "cluster" and [a["raw_news_id"] for a in task["articles"]] == [1, 2, 3]

        bad = client.post("/api/labels/cluster/r1-c0?round=1", json=_cluster_label(n=1))
        assert bad.status_code == 422

        nxt = client.post("/api/labels/cluster/r1-c0?round=1", json=_cluster_label()).json()
        assert nxt["task"]["target_id"] == "r1-c1"
        client.post("/api/labels/cluster/r1-c1?round=1", json=_cluster_label(ids=(4, 5, 6)))
        out = client.get("/api/next?round=1").json()["task"]
        assert out["kind"] == "output" and out["key_facts"][0]["text"] == "사실 0"
        assert set(out) >= {"draft", "converted", "articles"}
        assert "candidate" not in json.dumps(out)

    def test_round_two_never_shows_the_first_round_label(self, client):
        client.post("/api/labels/cluster/r1-c0?round=1", json=_cluster_label())
        assert client.get("/api/task/cluster/r1-c0?round=1").json()["existing"] is not None
        self.clock.t = T0 + timedelta(hours=49)
        task = client.get("/api/next?round=2").json()["task"]
        assert task["target_id"] == "r1-c0" and task["existing"] is None
