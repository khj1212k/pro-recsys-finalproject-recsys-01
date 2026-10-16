"""E0 사람 라벨(evaluation/llm/e0_labels.py, ADR 0009 A8.8)과 라벨 마감(ADR 0023 개정).

블라인드 내보내기는 합성 드라이 런의 run에서 한다(지어낸 기사, 페이크 LLM). 라벨 지표는 손으로 만든 기록으로 본다.
"""
import copy
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from evaluation.llm import e0_baseline as base  # noqa: E402
from evaluation.llm import e0_labels as lab  # noqa: E402
from evaluation.llm import e0_registered as reg  # noqa: E402
from evaluation.llm import labels as label_store  # noqa: E402

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 18, 14, 0, tzinfo=KST)


@pytest.fixture(scope="module")
def dry(tmp_path_factory):
    import core.llm.adapters as adapters

    root = tmp_path_factory.mktemp("labels-dry")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(adapters.OpenAICompatLLMClient, "_wait_before_retry", staticmethod(lambda b, s: True))
        out = base.dry_run(root, t0=T0, run_report=False)
    return out


@pytest.fixture
def run_copy(dry, tmp_path):
    """드라이 런의 run 디렉터리 사본(내보내기는 run마다 한 번이라 테스트마다 새 사본을 쓴다)."""
    dest = tmp_path / "run"
    shutil.copytree(dry["run_dir"], dest)
    return dest


def _export(dry, run_dir, tmp_path, prereg=None, **kw):
    inputs = dry["inputs"]
    return lab.export_blind(run_dir, export_dir=inputs.export_dir, manifest_path=inputs.manifest,
                            prereg=prereg or inputs.prereg, labels_root=tmp_path / "labels", seed=7, now=T0, **kw)


def _rows(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


def test_export_writes_first_drafts_and_differing_saved_versions_blind(dry, run_copy, tmp_path):
    texts = _rows(run_copy / "newsletters.jsonl")
    evals = {r["item_id"] for r in _rows(dry["inputs"].manifest) if r["split"] == "eval"}
    target = next(t for t in texts if t["kind"] == "saved_formal" and t["item_id"] in evals)
    target["formal"]["content"] = target["formal"]["content"] + "\n\n덧붙인 문단."
    target["formal_sha256"] = "f" * 64
    (run_copy / "newsletters.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in texts))

    out = _export(dry, run_copy, tmp_path)
    labels_dir = Path(out["dir"])
    outputs = _rows(labels_dir / "outputs.jsonl")
    key = json.loads((run_copy / lab.BLIND_KEY).read_text())["outputs"]
    assert out["clusters"] == 15 and len(outputs) == 16 == len(key)
    kinds = sorted(k["kind"] for k in key.values())
    assert kinds.count("saved") == 1 and kinds.count("first_draft") == 15
    saved_id = next(o for o, k in key.items() if k["kind"] == "saved")
    saved = next(o for o in outputs if o["output_id"] == saved_id)
    assert saved["converted"]["content"] and saved["draft"]["content"].endswith("덧붙인 문단.")
    assert all(set(o) == {"output_id", "item_id", "draft", "converted"} for o in outputs)  # 구분·게이트 결과 없음
    first_ids = [o["output_id"] for o in outputs if key[o["output_id"]]["kind"] == "first_draft"]
    assert all(not o["converted"]["content"] for o in outputs if o["output_id"] in first_ids)
    relabel = json.loads((labels_dir / "relabel.json").read_text())
    assert (len(relabel["outputs"]), len(relabel["clusters"]), relabel["min_hours_between_rounds"]) == (5, 3, 48.0)
    for name in ("outputs.jsonl", "clusters.jsonl", "relabel.json", label_store.RETENTION_FILE):
        assert stat.S_IMODE(os.stat(labels_dir / name).st_mode) == 0o600
    assert not (labels_dir / lab.BLIND_KEY).exists()                       # 매핑은 run 디렉터리에만


def test_label_deadline_is_the_oldest_sample_article_plus_thirty_days(dry, run_copy, tmp_path):
    from evaluation.llm import frozen_export as fx

    out = _export(dry, run_copy, tmp_path)
    items = _rows(dry["inputs"].manifest)
    ids = {a["raw_news_id"] for r in items for a in r["articles"]}
    rows = fx.load_export(dry["inputs"].export_dir, now=T0).rows
    oldest = min(r["crawled_at"] for r in rows if r["id"] in ids)
    expected = (fx._parse(oldest) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out["expires_at_utc"] == expected
    assert json.loads((Path(out["dir"]) / label_store.RETENTION_FILE).read_text())["expires_at_utc"] == expected


def test_reduced_subset_labels_outputs_of_the_first_ten_clusters_only(dry, run_copy, tmp_path):
    prereg = copy.deepcopy(dry["inputs"].prereg)
    prereg["pre_run_record"]["user_decisions"]["label_subset"] = "reduced_first10"
    out = _export(dry, run_copy, tmp_path, prereg=prereg)
    items = sorted((r for r in _rows(dry["inputs"].manifest) if r["split"] == "eval"), key=lambda r: r["order"])
    first10 = {r["item_id"] for r in items[:10]}
    outputs = _rows(Path(out["dir"]) / "outputs.jsonl")
    assert {o["item_id"] for o in outputs} == first10 and out["clusters"] == 15
    assert (len(out["relabel"]["outputs"]), len(out["relabel"]["clusters"])) == (3, 2)


def test_fallback_first_drafts_are_left_out_and_counted(dry, run_copy, tmp_path):
    texts = _rows(run_copy / "newsletters.jsonl")
    evals = {r["item_id"] for r in _rows(dry["inputs"].manifest) if r["split"] == "eval"}
    first = next(t for t in texts if t["kind"] == "draft_attempt" and t["attempt"] == 1 and t["item_id"] in evals)
    first["fallback"] = ["content"]
    (run_copy / "newsletters.jsonl").write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in texts))
    out = _export(dry, run_copy, tmp_path)
    assert out["excluded"]["first_draft_fallback"] == 1 and out["outputs"] == 14


def test_void_runs_and_second_exports_are_refused(dry, run_copy, tmp_path):
    _export(dry, run_copy, tmp_path)
    with pytest.raises(lab.ExportRefused):
        _export(dry, run_copy, tmp_path)
    void = tmp_path / "void-run"
    shutil.copytree(dry["run_dir"], void)
    summary = json.loads((void / "run_summary.json").read_text())
    summary["void"] = {"reason": "infra_below_8"}
    (void / "run_summary.json").write_text(json.dumps(summary))
    with pytest.raises(lab.ExportRefused):
        _export(dry, void, tmp_path / "v")


# ---------------------------------------------------------------- 라벨 마감


def test_label_store_purges_bodies_after_the_deadline_and_the_ui_stops(dry, run_copy, tmp_path):
    from fastapi.testclient import TestClient

    from evaluation.llm.labeling_app import create_app

    out = _export(dry, run_copy, tmp_path)
    labels_dir = Path(out["dir"])
    expires = datetime.fromisoformat(out["expires_at_utc"].replace("Z", "+00:00"))
    before = label_store.LabelStore(labels_dir, now=lambda: expires - timedelta(days=1))
    assert not before.expired and label_store.has_bodies(labels_dir)
    client = TestClient(create_app(labels_dir, now=lambda: expires - timedelta(days=1)))
    assert client.get("/api/next").status_code == 200

    after = label_store.LabelStore(labels_dir, now=lambda: expires)
    assert after.expired and not label_store.has_bodies(labels_dir)
    titles = [a["title"] for r in _rows(labels_dir / "clusters.jsonl") for a in r["articles"]]
    assert all(titles)                                                     # 제목·URL·id는 남는다
    assert label_store.purge_expired_bodies(labels_dir, expires) == 0      # 여러 번 불러도 같다
    late = TestClient(create_app(labels_dir, now=lambda: expires + timedelta(hours=1)))
    assert late.get("/api/next").status_code == 410


def test_the_labeling_app_refuses_inputs_without_a_deadline(tmp_path, monkeypatch):
    from evaluation.llm import labeling_app

    run = tmp_path / "labels" / "x"
    run.mkdir(parents=True)
    (run / "outputs.jsonl").write_text("")
    (run / "clusters.jsonl").write_text(json.dumps({"item_id": "r1-c1", "articles": [{"raw_news_id": 1, "body": "본문"}]})
                                        + "\n")
    monkeypatch.setattr(labeling_app, "LABELS_ROOT", tmp_path / "labels")
    with pytest.raises(SystemExit):
        labeling_app.main(["--run", "x"])
    (run / label_store.RETENTION_FILE).write_text(json.dumps({"expires_at_utc": "2020-01-01T00:00:00Z"}))
    with pytest.raises(SystemExit):
        labeling_app.main(["--run", "x"])
    assert not label_store.has_bodies(run)


# ---------------------------------------------------------------- 라벨 지표


def _label_fixture(tmp_path):
    run = tmp_path / "run"
    labels_dir = tmp_path / "labels"
    run.mkdir()
    labels_dir.mkdir()
    draft = {"title": "가람시 도서관", "sentence": "도서관을 짓는다", "content": "예산 120억 원이다. 개관은 3월이다."}
    items = [{"item_id": "r1-c1", "split": "eval", "order": 0, "run_k": 1, "cluster_id": 1,
              "articles": [{"raw_news_id": i} for i in (1, 2, 3)]},
             {"item_id": "r1-c2", "split": "eval", "order": 1, "run_k": 1, "cluster_id": 2,
              "articles": [{"raw_news_id": i} for i in (4, 5, 6)]}]
    ends = {"r1-c1": {"outcome": "completed"}, "r1-c2": {"outcome": "skipped"}}
    nodes = [{"node": "check_faithfulness", "item_id": "r1-c1", "attempt": 1, "reason": None,
              "blocking": {"numbers": 1, "quotes": 0},
              "spans": [{"group": "blocking", "type": "numbers", "field": "content", "start": 3, "end": 9},
                        {"group": "advisory", "type": "entities", "field": "title", "start": 0, "end": 3}]},
             {"node": "eval_newsletter", "item_id": "r1-c1", "attempt": 1, "decision": "PASS",
              "criteria": {"faithfulness": 4, "coverage": 4, "coherence": 4, "style": 4}}]
    (run / "nodes.jsonl").write_text("".join(json.dumps(n, ensure_ascii=False) + "\n" for n in nodes))
    (run / lab.BLIND_KEY).write_text(json.dumps({"outputs": {"o1": {"item_id": "r1-c1", "kind": "first_draft"}}}))
    (labels_dir / "outputs.jsonl").write_text(json.dumps({"output_id": "o1", "item_id": "r1-c1", "draft": draft,
                                                          "converted": {"title": "", "summary": "", "content": ""}},
                                                         ensure_ascii=False) + "\n")
    (labels_dir / "clusters.jsonl").write_text("".join(json.dumps({"item_id": i["item_id"], "articles": [
        {"raw_news_id": a["raw_news_id"], "title": "t", "body": "b"} for a in i["articles"]]}, ensure_ascii=False) + "\n"
        for i in items))
    (labels_dir / "relabel.json").write_text(json.dumps({"outputs": ["o1"], "clusters": ["r1-c1"],
                                                         "min_hours_between_rounds": 48}))
    return run, labels_dir, items, ends, draft


def test_label_metrics_on_hand_made_labels(tmp_path):
    run, labels_dir, items, ends, draft = _label_fixture(tmp_path)
    t = datetime(2026, 10, 20, tzinfo=timezone.utc)
    store = label_store.LabelStore(labels_dir, now=lambda: t)
    facts = [{"text": f"사실 {k}", "source_ids": [1]} for k in range(3)]
    store.save("cluster", "r1-c1", 1, {"single_event": True, "outlier_ids": [], "key_facts": facts})
    store.save("cluster", "r1-c2", 1, {"single_event": False, "outlier_ids": [4], "key_facts": []})
    span = draft["content"][3:9]
    store.save("output", "o1", 1, {"fact_errors": [{"field": "content", "start": 3, "end": 9, "text": span,
                                                     "type": "number"}],
                                   "key_facts_covered": [0, 1], "style": 4, "publishable": True, "tone_drift": False})
    later = label_store.LabelStore(labels_dir, now=lambda: t + timedelta(hours=49))
    later.save("output", "o1", 2, {"fact_errors": [], "key_facts_covered": [0], "style": 3, "publishable": False,
                                   "tone_drift": False})
    later.save("cluster", "r1-c1", 2, {"single_event": True, "outlier_ids": [], "key_facts": facts})

    m = lab.label_metrics(labels_dir, run, items, ends, {})
    assert m["publishable_primary"] == {"k": 1, "n": 2, "rate": 0.5, "wilson95": m["publishable_primary"]["wilson95"]}
    assert m["outcome_by_single_event"] == {"saved": {"single_event_Y": 1, "single_event_N": 0},
                                            "skipped": {"single_event_Y": 0, "single_event_N": 1}}
    checker = m["checker_vs_human_first_draft"]
    assert checker["precision"]["k"] == 1 and checker["precision"]["n"] == 1
    assert checker["recall_number"]["k"] == 1 and checker["recall_quote"]["n"] == 0
    assert checker["draft_level_2x2"]["blocked_and_error"] == 1
    assert m["saved"]["gate_miss_number_quote_errors"] == 1
    assert m["judge_shadow_vs_publishable"]["table"]["judge_pass_pub_Y"] == 1
    assert m["key_fact_coverage"]["k"] == 2 and m["key_fact_coverage"]["n"] == 3
    assert m["relabel"]["outputs_publishable_agreement"]["k"] == 0 and m["relabel"]["disagreement_ids"] == ["o1"]
    assert m["relabel"]["clusters_single_event_agreement"]["k"] == 1


def test_label_metrics_are_pending_without_labels(tmp_path):
    run, labels_dir, items, ends, _ = _label_fixture(tmp_path)
    assert lab.label_metrics(labels_dir, run, items, ends, {}) == {"status": "pending"}
