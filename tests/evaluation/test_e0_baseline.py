"""E0 러너(evaluation/llm/e0_baseline.py) - 합성 반출본 + 페이크 LLM 전송 계층으로 사슬 전체를 돈다.

실제 기사·실제 호출은 없다. 운영 그래프·어댑터·openai SDK·지출 가드(ADR 0035)·E0 계측·파일 대역은 전부 실제 코드다.
보는 것: 완주 사슬(pre-flight → warmup → 점검 → eval → 리포트 → 블라인드 내보내기), 시작 검사의 거절, 예산 중단과
부분 실행, 402 인프라 중단과 void, 재개(시작 행만 있는 클러스터는 interrupted로 닫고 다시 돌지 않는다), 벽시계 상한,
`app.stream`과 `app.invoke`의 호출 순서가 같음.
"""
import copy
import dataclasses
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "ai_workspace"))

from evaluation.llm import e0_baseline as base  # noqa: E402
from evaluation.llm import e0_instrument as ins  # noqa: E402
from evaluation.llm import e0_registered as reg  # noqa: E402
from evaluation.llm import e0_synthetic as syn  # noqa: E402

KST = timezone(timedelta(hours=9))
T0 = datetime(2026, 10, 18, 14, 0, tzinfo=KST)


def _no_wait(monkeypatch):
    import core.llm.adapters as adapters

    monkeypatch.setattr(adapters.OpenAICompatLLMClient, "_wait_before_retry", staticmethod(lambda b, s: True))


def _read(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture(scope="module")
def happy(tmp_path_factory):
    """결함(429·500·스키마 실패)을 섞은 페이크 응답으로 끝까지 돈 드라이 런 하나를 여러 테스트가 본다."""
    import core.llm.adapters as adapters

    root = tmp_path_factory.mktemp("e0dry")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(adapters.OpenAICompatLLMClient, "_wait_before_retry", staticmethod(lambda b, s: True))
        lines = []
        out = base.dry_run(root, t0=T0, progress=lines.append)
    out["progress"] = lines
    return out


# ---------------------------------------------------------------- 완주 사슬


def test_dry_run_runs_preflight_warmup_and_all_fifteen_in_order(happy):
    summary = happy["summary"]
    assert (summary["n_eval"], summary["n_warmup"], summary["n_processed"]) == (15, 2, 15)
    assert summary["void"] is None and summary["stop"] is None and summary["db_clean"] is True
    run = Path(happy["run_dir"])
    pre = json.loads((run / base.PREFLIGHT_FILE).read_text())
    assert [c["purpose"] for c in pre["calls"]] == list(reg.CALL_PARAMS)
    assert pre["call_params_mismatch"] == [] and not pre["infra_failure"]
    rows = _read(run / ins.CLUSTERS_FILE)
    starts = [r for r in rows if r["kind"] == "start"]
    items = {r["item_id"]: r for r in _read(happy["inputs"].manifest)}
    first_eval = next(i for i, r in enumerate(starts) if r["split"] == "eval")
    assert all(r["split"] == "warmup" for r in starts[:first_eval]) and first_eval == 2
    # 워커 3개: 등록한 순서에서 p번째 클러스터는 p+3번째보다 늦게 시작하지 않는다
    position = {r["item_id"]: i for i, r in enumerate(starts)}
    order = sorted((r for r in items.values() if r["split"] == "eval"), key=lambda r: r["order"])
    assert all(position[a["item_id"]] < position[b["item_id"]] for a, b in zip(order, order[3:]))
    state = json.loads((run / base.RUN_STATE).read_text())
    assert state["finished"] and state["audit_after_warmup"] == [] and state["phases"]["eval"] == "done"


def test_records_have_the_registered_fields_and_add_up(happy):
    run = Path(happy["run_dir"])
    http, calls = _read(run / ins.HTTP_FILE), _read(run / ins.CALLS_FILE)
    assert all(sorted(h) == sorted(reg.HTTP_ATTEMPT_FIELDS) for h in http)
    assert all(sorted(c) == sorted(reg.CALL_FIELDS) for c in calls)
    assert len(http) == happy["fake_requests"]           # 보낸 요청마다 정확히 1행
    totals = ins.Ledger.totals(run / ins.LEDGER_FILE)
    assert totals["open_ids"] == [] and totals["settled_usd"] == pytest.approx(sum(h["cost_usd"] for h in http))
    assert {h["status"] for h in http} >= {200, 429}      # 결함 섞기가 실제로 들어갔다
    assert any(c["schema_failures"] for c in calls)
    nodes = _read(run / ins.NODES_FILE)
    assert {n["node"] for n in nodes} >= {"init_cluster", "eval_cluster", "generate_newsletter", "check_faithfulness",
                                         "eval_newsletter", "embed_newsletter", "convert_tone", "check_tone_drift",
                                         "save_newsletter"}


def test_texts_live_only_in_newsletters_jsonl_and_the_console_shows_counts_and_cost(happy):
    run = Path(happy["run_dir"])
    drafts = [r for r in _read(run / "newsletters.jsonl") if r["kind"] == "draft_attempt"]
    assert drafts and all(d["draft"]["content"] for d in drafts)
    probe = drafts[0]["draft"]["content"][:20]
    for name in (ins.HTTP_FILE, ins.CALLS_FILE, ins.NODES_FILE, ins.CLUSTERS_FILE, ins.LEDGER_FILE, base.RUN_SUMMARY,
                 base.RUN_MANIFEST, base.RUN_STATE, base.PREFLIGHT_FILE):
        assert probe not in (run / name).read_text(encoding="utf-8"), name
    assert happy["progress"] and all(line.startswith("[e0] ") and "누적 $" in line for line in happy["progress"])
    assert all(probe not in line for line in happy["progress"])


def test_run_manifest_records_versions_config_and_the_in_client_cap(happy):
    manifest = json.loads((Path(happy["run_dir"]) / base.RUN_MANIFEST).read_text())
    assert manifest["packages"]["openai"] and manifest["packages"]["langgraph"] and manifest["pip_freeze_sha256"]
    assert manifest["python_version"] and manifest["forbid_database"] is False
    assert manifest["in_client_cap"]["LLM_BUDGET_RUN_USD"] == reg.IN_CLIENT_RUN_DAY_CAP_USD
    assert manifest["effective_config"]["pipeline.settings"]["NEWSLETTER_WORKERS"] == 3


def test_report_and_blind_export_come_out_of_the_same_run(happy):
    report = happy["report_json"]
    assert report["header"]["dry_run"] is True and report["denominator"]["n_p"] == 15
    assert Path(happy["report"]["json"]).exists() and Path(happy["report"]["md"]).exists()
    labels = happy["labels"]
    assert labels["clusters"] == 15 and labels["outputs"] >= 15 and labels["relabel"]["min_hours_between_rounds"] == 48


# ---------------------------------------------------------------- 시작 검사 거절


@pytest.fixture
def inputs(tmp_path):
    with base.dry_run_sandbox(tmp_path) as data:
        yield base.dry_inputs(tmp_path, data, T0)


def _refuse(inputs, mutate=None, **overrides):
    prereg = copy.deepcopy(inputs.prereg)
    if mutate:
        mutate(prereg["pre_run_record"])
    cfg = base.dry_config(dataclasses.replace(inputs, prereg=prereg), syn.FakeLLMTransport(), **overrides)
    with pytest.raises(base.Refused) as refused:
        base.run_e0(cfg)
    assert not (base.e0_root()).exists() or not list(base.e0_root().glob("run-*"))  # run 디렉터리를 만들지 않았다
    return " ".join(refused.value.problems)


def test_missing_pre_run_record_fields_are_refused(inputs):
    problems = _refuse(inputs, lambda r: r.update(code_sha=None, snapshot_sha256=None))
    assert "code_sha" in problems and "snapshot_sha256" in problems


def test_hash_mismatches_are_refused(inputs):
    assert "embeddings_sha256" in _refuse(inputs, lambda r: r.update(embeddings_sha256="0" * 64))
    assert "export_manifest_sha256" in _refuse(inputs, lambda r: r.update(export_manifest_sha256="1" * 64))
    assert "스냅숏" in _refuse(inputs, lambda r: r.update(snapshot_sha256={**r["snapshot_sha256"], "run2": "2" * 64}))
    assert "매니페스트" in _refuse(inputs, lambda r: r.update(sample_manifest_sha256="3" * 64))


def test_code_identity_problems_and_dirty_tree_are_refused(inputs, monkeypatch):
    monkeypatch.setattr(reg, "check_code_identity", lambda repo, sha, **kw: ["(b) 작업 트리가 깨끗하지 않다"])
    monkeypatch.setattr(reg, "_git", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})())
    assert "(b)" in _refuse(inputs, check_code_identity=True)


def test_a_filled_record_or_manifest_that_is_not_committed_at_head_is_refused(inputs, monkeypatch):
    """(a)~(d)가 통과해도(깨끗한 main 체크아웃이라고 두고), 커밋되지 않은 채운 기록·다른 매니페스트로는 시작하지 않는다."""
    monkeypatch.setattr(reg, "check_code_identity", lambda repo, sha, **kw: [])
    problems = _refuse(inputs, check_code_identity=True)
    assert "(e) 실행에 쓰는 사전 등록이 HEAD에 커밋된" in problems   # 이 저장소 HEAD의 yaml은 기록이 비어 있다
    assert "표본 매니페스트" in problems


@pytest.mark.parametrize("option", ["--prereg", "--manifest"])
def test_run_cannot_be_pointed_at_another_record_or_manifest(option, capsys):
    with pytest.raises(SystemExit) as exited:
        base.main(["run", "--export-dir", "x", "--pack-dir", "y", option, "/somewhere/filled-copy"])
    assert exited.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


def test_prepare_refuses_outside_a_clean_main_checkout_after_fetching(monkeypatch, tmp_path):
    seen = []

    def head_problems(repo, **kw):
        seen.append((repo, kw))
        return ["(a) HEAD가 origin/main에 있는 커밋이 아니다"]

    monkeypatch.setattr(reg, "head_problems", head_problems)
    manifest = tmp_path / "e0.jsonl"
    assert base.main(["prepare", "--export-dir", "x", "--pack-dir", "y", "--manifest", str(manifest)]) == 2
    assert seen == [(reg.REPO_ROOT, {})] and not manifest.exists()   # 기본값 fetch=True


def test_settings_kill_switch_and_missing_key_are_refused(inputs, monkeypatch):
    from config.settings import Settings

    monkeypatch.setattr(Settings, "TONE_DRIFT_GATE_MODE", "shadow")
    assert "TONE_DRIFT_GATE_MODE" in _refuse(inputs)
    monkeypatch.setattr(Settings, "TONE_DRIFT_GATE_MODE", "enforce")
    monkeypatch.setenv("LLM_KILL_SWITCH", "1")
    assert "킬 스위치" in _refuse(inputs)
    monkeypatch.delenv("LLM_KILL_SWITCH")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert "GEMINI_API_KEY" in _refuse(inputs, require_api_key=True)


# ---------------------------------------------------------------- 중단·재개


def test_an_unusable_in_client_spend_cap_is_refused_before_any_run_directory(inputs, monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_TOTAL_USD", "1.00")   # 전체 상한 여유 < $2.00 - E0 가드보다 먼저 막힌다
    assert "전체 상한 여유" in _refuse(inputs)
    monkeypatch.setenv("LLM_BUDGET_TOTAL_USD", "3.00")
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", str(inputs.root / "missing-ledger.jsonl"))
    assert "지출 가드" in _refuse(inputs)


def test_budget_cap_makes_a_partial_run_with_aborted_and_not_started_clusters(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    out = base.dry_run(tmp_path, t0=T0, faults=syn.FaultPlan(), cap_run_usd=0.06, run_report=True)
    summary = out["summary"]
    assert summary["stop"]["reason"] == "budget" and summary["void"] is None   # 예산 중단은 void가 아니라 부분 실행
    assert summary["outcomes"].get("aborted_budget", 0) >= 1 and summary["outcomes"].get("not_started", 0) >= 1
    run = Path(out["run_dir"])
    assert ins.Ledger.sum_usd(run / ins.LEDGER_FILE) <= 0.06
    http = _read(run / ins.HTTP_FILE)
    stop_at = next(e["at"] for e in _read(run / ins.EVENTS_FILE) if e["kind"] == "stop")
    assert http and all(h["started_at"] <= stop_at for h in http)        # 중단 뒤에 시작한 요청은 없다
    assert out["report_json"]["header"]["partial_run"] is True
    assert out["report_json"]["metrics"]["13_failure_taxonomy"]["clusters"]
    aborted = [t for t in out["report_json"]["metrics"]["13_failure_taxonomy"]["clusters"] if t["outcome"] == "aborted_budget"]
    assert all(t["root_cause"] == "aborted_budget" for t in aborted)


def test_402_aborts_the_cluster_as_infra_not_skipped_and_the_run_is_void_below_eight(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    # pre-flight 5 + warmup 2 x 5 = 15번째 요청까지는 정상, 그 뒤 모든 요청이 402
    out = base.dry_run(tmp_path, t0=T0, faults=syn.FaultPlan(always=402, after_requests=16))
    summary = out["summary"]
    assert summary["stop"]["reason"] == "infra" and summary["void"] == {"reason": "infra_below_8",
                                                                         "n_processed": summary["n_processed"]}
    ends = [r for r in _read(Path(out["run_dir"]) / ins.CLUSTERS_FILE) if r["kind"] == "end"]
    aborted = [r for r in ends if r["outcome"] == "aborted_infra"]
    assert aborted and all(r["root_cause"] == "aborted_infra" for r in aborted)
    assert not any(r["outcome"] == "skipped" for r in ends)
    assert "labels" not in out   # void 실행에는 라벨을 달지 않는다


def test_an_infra_failure_in_preflight_stops_before_warmup_and_is_not_void(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    out = base.dry_run(tmp_path, t0=T0, faults=syn.FaultPlan(always=401), run_report=False)
    summary = out["summary"]
    assert summary["reason"] == "preflight_infra_failure" and summary["void"] is None
    assert summary["stop"]["reason"] == "infra" and summary["outcomes"] == {"not_started": 15}
    assert out["fake_requests"] == 1   # 첫 pre-flight 호출이 401 - 그 뒤 아무것도 보내지 않았다
    pre = json.loads((Path(out["run_dir"]) / base.PREFLIGHT_FILE).read_text())
    assert pre["infra_failure"] is True


def test_resume_closes_the_in_flight_cluster_as_interrupted_and_continues_in_order(tmp_path, monkeypatch):
    _no_wait(monkeypatch)

    class ProcessDied(BaseException):
        pass

    original = base.Runner.run_phase

    def crash_after_four(self, phase, items):
        if phase != "eval":
            return original(self, phase, items)
        original(self, phase, items[:4])
        self.recorder.start_cluster(items[4], phase="eval")                # 그때 돌던 클러스터
        self.recorder.ledger.append({"type": "reserve", "attempt_id": "a999999", "worst_usd": 0.02,  # 그때 나가던 요청
                                     "cluster_id": items[4]["cluster_id"], "run_k": items[4]["run_k"]})
        raise ProcessDied()

    with base.dry_run_sandbox(tmp_path) as data:
        inputs = base.dry_inputs(tmp_path, data, T0)
        cfg = base.dry_config(inputs, syn.FakeLLMTransport())
        monkeypatch.setattr(base.Runner, "run_phase", crash_after_four)
        with pytest.raises(ProcessDied):
            base.run_e0(cfg)
        monkeypatch.setattr(base.Runner, "run_phase", original)
        run_name = sorted(p.name for p in base.e0_root().glob("run-*"))[0]
        transport = syn.FakeLLMTransport()
        summary = base.run_e0(base.dry_config(inputs, transport, resume=run_name, resume_reason="절전"))
        run = base.e0_root() / run_name
        rows = _read(run / ins.CLUSTERS_FILE)

    order = [r["item_id"] for r in sorted(_read(inputs.manifest), key=lambda r: r["order"]) if r["split"] == "eval"]
    in_flight = order[4]
    starts = [r["item_id"] for r in rows if r["kind"] == "start"]
    ends = {r["item_id"]: r for r in rows if r["kind"] == "end"}
    assert starts.count(in_flight) == 1 and ends[in_flight]["outcome"] == "interrupted"
    resumed = [i for i in starts if i in order[5:]]
    assert sorted(resumed) == sorted(order[5:])
    assert all(resumed.index(a) < resumed.index(b) for a, b in zip(order[5:], order[8:]))   # 워커 3개, 등록 순서
    assert all(starts.count(i) == 1 for i in order)                        # 끝난 것도 다시 돌지 않았다
    assert summary["n_processed"] == 14 and summary["outcomes"]["interrupted"] == 1
    state = json.loads((run / base.RUN_STATE).read_text())
    assert [r["reason"] for r in state["resumes"]] == ["절전"] and state["finished"]
    died = [h for h in _read(run / ins.HTTP_FILE) if h["transport_error"] == "process_died"]
    assert len(died) == 1 and died[0]["cost_usd"] == 0.02


def test_finished_runs_are_not_resumed(tmp_path, monkeypatch):
    _no_wait(monkeypatch)
    with base.dry_run_sandbox(tmp_path) as data:
        inputs = base.dry_inputs(tmp_path, data, T0)
        summary = base.run_e0(base.dry_config(inputs, syn.FakeLLMTransport()))
        with pytest.raises(base.Refused):
            base.run_e0(base.dry_config(inputs, syn.FakeLLMTransport(), resume=summary["run"]))


def test_wall_clock_cap_stops_starting_new_clusters(tmp_path, monkeypatch):
    _no_wait(monkeypatch)

    class Clock:
        t = 1_000_000.0

        def __call__(self):
            self.t += 120.0
            return self.t

    out = base.dry_run(tmp_path, t0=T0, faults=syn.FaultPlan(), clock=Clock(), wall_clock_cap_s=7200.0)
    summary = out["summary"]
    ends = [r for r in _read(Path(out["run_dir"]) / ins.CLUSTERS_FILE) if r["kind"] == "end" and r["split"] == "eval"]
    assert summary["wall_clock_reached"] and summary["void"] is None and summary["stop"] is None
    assert any(r["outcome"] == "completed" for r in ends)
    assert any(r["outcome"] == "not_started" and r.get("reason") == "wall_clock" for r in ends)


# ---------------------------------------------------------------- stream = invoke


def test_stream_and_invoke_make_the_same_calls_in_the_same_order(tmp_path):
    from core.llm.client import LLMResult, LLMUsage
    from core.llm import schemas
    import core.reconstruction.generator as generator_module
    import core.tone_converter as tone_module
    import workflow.evaluators as evaluators_module
    from evaluation.llm import e0_sample, e0_store
    from workflow.graph import compile_workflow

    class Fake:
        provider, model = "fake", "fake"

        def __init__(self):
            self.purposes = []

        def complete(self, messages, *, schema=None, purpose="unknown", temperature=0.2, max_tokens=4096):
            self.purposes.append(purpose)
            n = len(self.purposes)
            parsed = {
                schemas.ClusterEval: lambda: schemas.ClusterEval(decision="FAIL" if n == 1 else "PASS", confidence=0.5,
                                                                 outlier_indices=[0]),
                schemas.NewsletterContent: lambda: schemas.NewsletterContent(content="합성 문단입니다.\n\n둘째 문단입니다."),
                schemas.NewsletterMeta: lambda: schemas.NewsletterMeta(title="합성", sentence="합성 요약",
                                                                       keywords=["합성"], categories=["사회"]),
                schemas.NewsletterEvalV2: lambda: schemas.NewsletterEvalV2(
                    scores=schemas.CriterionScores(faithfulness=2, coverage=4, coherence=4, style=4)),
                schemas.ToneResult: lambda: schemas.ToneResult(title="합성", summary="합성 요약",
                                                              content="합성 문단이에요.\n\n둘째 문단이에요."),
            }[schema]()
            return LLMResult(text=None, parsed=parsed, usage=LLMUsage(), latency_s=0.0, attempts=1,
                             provider="fake", model="fake")

    with base.dry_run_sandbox(tmp_path) as data:
        inputs = base.dry_inputs(tmp_path, data, T0)
        source = e0_store.FileArticleSource.open(inputs.export_dir, inputs.pack_dir, now=T0)
        plan = e0_sample.build_plan(source)
        item = next(r for r in plan.eval_items if r["size"] >= 6)
        state = plan.runs[item["run_k"] - 1].state_for(item["cluster_id"], run_id=1)
        app = compile_workflow()
        finals, purposes = [], []
        for mode in ("invoke", "stream"):
            fake = Fake()
            store = e0_store.FileGenerationStore(data / "experiments" / "e0" / f"cmp-{mode}")
            with pytest.MonkeyPatch.context() as mp, e0_store.installed(store, forbid_database=False):
                for module in (evaluators_module, generator_module, tone_module):
                    mp.setattr(module, "get_client", lambda role, fake=fake: fake)
                if mode == "invoke":
                    final = app.invoke(copy.deepcopy(state))
                else:
                    final = None
                    for kind, chunk in app.stream(copy.deepcopy(state), stream_mode=["updates", "values"]):
                        if kind == "values":
                            final = chunk
            finals.append({k: final.get(k) for k in ("completed_newsletters", "failed_clusters", "skipped_clusters",
                                                     "failure_reason", "tone_fallback")})
            purposes.append(fake.purposes)
    assert purposes[0] == purposes[1] and purposes[0][:2] == ["cluster_eval", "cluster_eval"]
    assert finals[0] == finals[1]
