"""E0 표본 만들기(evaluation/llm/e0_sample.py, ADR 0009 A8.1): 운영 클러스터링 → 등록한 층화 추출 → 처리 순서.

합성 반출본(evaluation/llm/e0_synthetic.py - 지어낸 기사, 주제별로 가까운 난수 벡터)에서 돈다. LLM은 부르지 않는다.
"""
import hashlib
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from evaluation.llm import e0_registered as reg  # noqa: E402
from evaluation.llm import e0_sample, e0_store, e0_synthetic, evalset  # noqa: E402
from tests.frozen_news_fakes import use_tmp_main_checkout  # noqa: E402

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 8, 14, 0, tzinfo=KST)


@pytest.fixture(autouse=True)
def _main_checkout(monkeypatch, tmp_path):
    return use_tmp_main_checkout(monkeypatch, tmp_path)


def _source(tmp_path, days=5, sub="x"):
    export_dir, pack_dir = e0_synthetic.write_synthetic_export(tmp_path / "data" / "exports" / sub, T0, days=days)
    return e0_store.FileArticleSource.open(export_dir, pack_dir, now=T0)


@pytest.fixture(scope="module")
def plan_and_rows(tmp_path_factory):
    import evaluation.llm.frozen_export as fx

    root = tmp_path_factory.mktemp("sample")
    saved = fx.main_checkout_data
    fx.main_checkout_data = lambda: root / "data"
    try:
        export_dir, pack_dir = e0_synthetic.write_synthetic_export(root / "data" / "exports", T0)
        source = e0_store.FileArticleSource.open(export_dir, pack_dir, now=T0)
        return e0_sample.build_plan(source), source.export.rows
    finally:
        fx.main_checkout_data = saved


def test_three_most_recent_day_windows_give_fifteen_eval_and_two_warmup(plan_and_rows):
    plan, _ = plan_and_rows
    assert plan.frame == "full" and len(plan.eval_items) == 15 and len(plan.warmup_items) == 2
    assert plan.bucket_allocation() == {"3-4": 5, "5-9": 5, "10+": 5}
    # 마지막 세 창의 끝 = T0(10-08 14:00 KST) 직전 완결된 KST 날짜 10-05·06·07의 06:00 KST(= 전날 21:00 UTC)
    assert plan.day_windows_utc() == [["2026-10-03T21:00:00", "2026-10-04T21:00:00"],
                                      ["2026-10-04T21:00:00", "2026-10-05T21:00:00"],
                                      ["2026-10-05T21:00:00", "2026-10-06T21:00:00"]]
    assert [r.pseudo_now for r in plan.runs] == sorted(r.pseudo_now for r in plan.runs)


def test_the_sample_is_the_registered_stratified_sample_of_production_candidates(plan_and_rows):
    plan, _ = plan_and_rows
    candidates = [c for k, run in enumerate(plan.runs, start=1) for c in e0_sample.candidates_for(run, k)]
    again = evalset.stratified_sample(candidates, n=15, seed=20261006, hard_fraction=0.0, warmup=2, min_size=3)
    assert [(s.candidate.item_id, s.split) for s in again] == [(s.candidate.item_id, s.split) for s in plan.sample]
    assert all(c.category == "미분류" and c.split_v2 in ("yes", "no") for c in candidates)


def test_processing_order_is_warmup_first_then_the_seeded_shuffle(plan_and_rows):
    plan, _ = plan_and_rows
    rows = plan.rows
    assert [r["order"] for r in rows] == list(range(17))
    assert [r["split"] for r in rows[:2]] == ["warmup", "warmup"]
    assert [(r["run_k"], r["cluster_id"]) for r in rows[:2]] == sorted((r["run_k"], r["cluster_id"]) for r in rows[:2])
    evals = sorted((r for r in rows if r["split"] == "eval"), key=lambda r: (r["run_k"], r["cluster_id"]))
    expected = [r["item_id"] for r in evals]
    random.Random(20261007).shuffle(expected)
    assert [r["item_id"] for r in rows[2:]] == expected


def test_manifest_has_ids_hashes_lengths_and_order_only(plan_and_rows):
    plan, export_rows = plan_and_rows
    data = plan.manifest_bytes().decode("utf-8")
    for row in export_rows:
        assert row["title"] not in data and row["body"][:30] not in data
    allowed = {"format", "item_id", "split", "order", "run_k", "cluster_id", "pseudo_now_utc", "size", "size_bucket",
               "category", "split_v2", "hard_case", "sampling_weight", "articles"}
    for row in plan.rows:
        assert set(row) <= allowed
        assert all(set(a) == {"raw_news_id", "url", "press_name", "body_sha256", "title_sha256", "body_chars"}
                   for a in row["articles"])


def test_same_inputs_give_the_same_bytes_and_the_record_check_catches_changes(tmp_path, plan_and_rows):
    plan, _ = plan_and_rows
    again = e0_sample.build_plan(_source(tmp_path))
    assert again.manifest_bytes() == plan.manifest_bytes() and again.snapshot_sha256() == plan.snapshot_sha256()
    manifest = tmp_path / "e0.jsonl"
    sha = e0_sample.write_manifest(manifest, again)
    record = {**again.record_fields(), "sample_manifest_sha256": sha}
    assert sha == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert e0_sample.compare_with_record(again, record, manifest) == []
    tampered = dict(record, snapshot_sha256={**record["snapshot_sha256"], "run1": "0" * 64})
    assert any("스냅숏" in p for p in e0_sample.compare_with_record(again, tampered, manifest))
    rows = e0_sample.read_manifest(manifest)
    rows[2], rows[3] = rows[3], rows[2]
    manifest.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))
    problems = e0_sample.compare_with_record(again, record, manifest)
    assert any("sha256" in p for p in problems) and any("커밋된 매니페스트" in p for p in problems)


def test_a_short_frame_extends_a_day_at_a_time_up_to_five(tmp_path, monkeypatch):
    # 하루에 주제 4개 → 3일로는 17개를 다 뽑지 못한다 → 앞으로 하루씩 늘려 같은 seed로 처음부터 다시 뽑는다
    monkeypatch.setattr(e0_synthetic, "TOPIC_SIZES", (3, 6, 10, 12))
    source = _source(tmp_path)
    plan = e0_sample.build_plan(source)
    assert len(plan.windows) in (4, 5) and plan.frame == "full" and len(plan.eval_items) == 15
    assert plan.windows == source.export.day_windows()[-len(plan.windows):]   # 가장 최근 창들, 이어진 날짜
    assert [r.pseudo_now for r in plan.runs] == sorted(r.pseudo_now for r in plan.runs)
    three = [c for k, run in enumerate(plan.runs[-3:], start=1) for c in e0_sample.candidates_for(run, k)]
    short = evalset.stratified_sample(three, n=15, seed=20261006, hard_fraction=0.0, warmup=2, min_size=3)
    assert len(short) < 17


def test_an_export_without_enough_days_is_refused_rather_than_shrunk(tmp_path, monkeypatch):
    monkeypatch.setattr(e0_synthetic, "TOPIC_SIZES", (3, 6, 10, 12))
    with pytest.raises(e0_sample.ExportTooNarrow):
        e0_sample.build_plan(_source(tmp_path, days=3))


def test_nine_to_fourteen_eval_proceeds_with_that_n_and_below_nine_is_void(tmp_path, monkeypatch):
    monkeypatch.setattr(e0_synthetic, "TOPIC_SIZES", (3, 6))  # 5일 10개
    short = e0_sample.build_plan(_source(tmp_path, sub="a"))
    assert short.frame == "short_n" and 9 <= len(short.eval_items) <= 14
    monkeypatch.setattr(e0_synthetic, "TOPIC_SIZES", (3,))   # 5일 5개
    with pytest.raises(e0_sample.FrameTooSmall):
        e0_sample.build_plan(_source(tmp_path, sub="b"))


def test_rows_extracted_after_the_pseudo_now_are_counted(plan_and_rows):
    plan, _ = plan_and_rows
    # 합성 기사는 수집 5분 뒤에 추출됐다 - 창 끝 5분 안에 수집된 기사만 의사 시각 뒤에 추출됐다
    assert set(plan.extracted_after_pseudo_now) == {"run1", "run2", "run3"}
    assert all(isinstance(v, int) and v >= 0 for v in plan.extracted_after_pseudo_now.values())
