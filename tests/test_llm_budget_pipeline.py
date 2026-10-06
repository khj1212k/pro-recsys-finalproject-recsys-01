# 지출 상한·서킷브레이커가 런을 멈췄을 때 파이프라인이 그것을 "깨끗한 중단"으로 드러내는지 본다
# - docs/adr/0035.
#
# 깨끗한 중단: (1) 그 뒤로 프로바이더에 요청이 나가지 않고, (2) LLM 없이 만든 로컬 초안이나 문체
# 변환 폴백이 저장되지 않으며, (3) 이미 만든 뉴스레터와 클러스터별 결과는 남고, (4) Stage5와 잡이
# 사유와 함께 0이 아닌 종료 코드로 끝난다. 전송 계층·DB·임베딩은 전부 페이크다.
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AI_WORKSPACE = str(REPO_ROOT / "ai_workspace")
sys.path.insert(0, AI_WORKSPACE)

from tests.llm_fakes import FakeOpenAIClient, make_response, make_status_error  # noqa: E402

import core.llm.adapters as adapters_module  # noqa: E402
import core.llm.registry as registry  # noqa: E402
import workflow.nodes as nodes_module  # noqa: E402
from config.settings import Settings  # noqa: E402
from core.llm import budget  # noqa: E402
from core.llm.budget import LLMBudgetExceeded, LLMCircuitOpen  # noqa: E402
from core.llm.schemas import (  # noqa: E402
    ClusterEval, CriterionScores, NewsletterContent, NewsletterEvalV2, NewsletterMeta, ToneResult,
)
from core.llm_metrics import LLMMetricsCollector  # noqa: E402
from jobs.runtime import EXIT_STOPPED, JobContext, JobSkipped, JobStopped, run_job  # noqa: E402
from pipeline.stages import (  # noqa: E402
    PIPELINE_STOPPED_EXIT_CODE,
    PipelineStopped,
    Stage5_NewsletterGeneration,
    run_clusters_bounded,
)
from workflow.graph import compile_workflow  # noqa: E402

GEN = "gemini-3.5-flash-lite"
JUDGE = "gemini-3.1-flash-lite"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path, llm_ledger_init):
    for name in ("GEN_PROVIDER", "GEN_MODEL", "JUDGE_PROVIDER", "JUDGE_MODEL", "TONE_PROVIDER", "TONE_MODEL",
                 "LLM_RUN_ID", "LLM_CIRCUIT_BREAKER_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", llm_ledger_init(str(tmp_path / "ledger.jsonl")))
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(tmp_path / "LLM_KILL_SWITCH"))
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)
    registry.reset_registry()
    budget.reset_run_state()
    LLMMetricsCollector().reset()
    yield
    registry.reset_registry()
    budget.reset_run_state()


def _stop(scope="run"):
    return LLMBudgetExceeded(f"cap({scope})", scope=scope, cap_usd=0.2, spent_usd=0.19, reserved_usd=0.0,
                             requested_usd=0.03)


# ---------------------------------------------------------------------------
# run_clusters_bounded: 중단 뒤에는 새 클러스터를 시작하지 않는다
# ---------------------------------------------------------------------------

def test_stop_in_one_cluster_keeps_finished_work_and_does_not_start_the_rest():
    calls = []

    def process(cid, idx):
        calls.append(cid)
        if cid == 2:
            raise _stop()
        return {"status": "completed", "newsletter_id": 100 + cid}

    outcomes = run_clusters_bounded(process, [(cid, cid) for cid in range(5)], [], max_workers=1)

    assert calls == [0, 1, 2]
    assert outcomes[0] == {"status": "completed", "newsletter_id": 100}
    assert outcomes[2]["status"] == "stopped" and outcomes[2]["failure_reason"] == "llm_budget_exceeded"
    assert outcomes[3] == outcomes[4] == {"status": "not_started", "failure_reason": "llm_budget_exceeded"}
    assert budget.run_stop().code == "llm_budget_exceeded"


def test_stop_also_ends_the_min_target_fill_loop():
    calls = []

    def process(cid, idx):
        calls.append(cid)
        if cid == 1:
            raise LLMCircuitOpen("503 x5", last_outcome="http_503", consecutive_failures=5)
        return {"status": "skipped"}

    outcomes = run_clusters_bounded(process, [(0, 0), (1, 1)], [(cid, cid) for cid in range(2, 8)],
                                    max_workers=1, min_target=3)

    assert calls == [0, 1]
    assert sorted(outcomes) == [0, 1]
    assert outcomes[1]["failure_reason"] == "llm_circuit_open"


def test_ordinary_cluster_error_is_still_isolated_and_the_batch_continues():
    def process(cid, idx):
        if cid == 1:
            raise ValueError("bad cluster")
        return {"status": "completed"}

    outcomes = run_clusters_bounded(process, [(cid, cid) for cid in range(3)], [], max_workers=1)

    assert [outcomes[c]["status"] for c in range(3)] == ["completed", "error", "completed"]
    assert budget.run_stop() is None


def test_stop_in_a_worker_thread_reaches_the_other_workers():
    """워커 3개: 한 스레드가 멈추면 아직 시작하지 않은 클러스터는 어느 스레드에서도 시작하지 않는다."""
    started = []

    def process(cid, idx):
        started.append(cid)
        if cid == 0:
            raise _stop("day")
        return {"status": "completed"}

    outcomes = run_clusters_bounded(process, [(cid, cid) for cid in range(30)], [], max_workers=3)

    assert outcomes[0]["status"] == "stopped"
    assert len(started) <= 3 + 2  # 이미 다른 워커가 집어 든 것까지만
    assert sum(1 for o in outcomes.values() if o["status"] == "not_started") >= 25
    assert len(outcomes) == 30


# ---------------------------------------------------------------------------
# Stage5: 기록을 남기고 PipelineStopped로 끝난다
# ---------------------------------------------------------------------------

def _stage5(invoke, clusters=None, workers=1):
    clusters = clusters or {103: ["c"], 102: ["b"], 101: ["a"]}
    fake_app = MagicMock()
    fake_app.invoke.side_effect = invoke
    fake_clusterer = MagicMock()
    fake_clusterer.cluster_news.return_value = clusters
    fake_clusterer.get_clustered_articles.return_value = {"dummy": "data"}
    fake_clusterer.cluster_meta = None
    settings = SimpleNamespace(HDBSCAN_MIN_CLUSTER_SIZE=3, HDBSCAN_MIN_SAMPLES=2, MIN_NEWSLETTER_TARGET=0,
                               CLUSTER_LOOKBACK_HOURS=24, NEWSLETTER_WORKERS=workers)
    return fake_app, fake_clusterer, settings


def test_stage5_records_outcomes_and_metrics_then_raises_pipeline_stopped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # 메트릭 요약은 logs/ 아래에 쓴다

    def invoke(state):
        if state["current_cluster_id"] == 102:  # 처리 순서: 103, 102, 101
            raise _stop("day")
        return {"completed_newsletters": [900 + state["current_cluster_id"]]}

    fake_app, fake_clusterer, settings = _stage5(invoke)
    logged = {}

    with patch("core.clusterer.NewsClusterer", return_value=fake_clusterer), \
         patch("workflow.graph.compile_workflow", return_value=fake_app), \
         patch("db.batch_manager.create_new_batch", return_value=42), \
         patch("db.batch_manager.update_cluster_log", side_effect=lambda run_id, log: logged.update(run_id=run_id, log=log)):
        with pytest.raises(PipelineStopped) as exc:
            Stage5_NewsletterGeneration(settings).execute()

    stop = exc.value
    assert (stop.reason, stop.newsletters_created, stop.run_id) == ("llm_budget_exceeded", 1, 42)
    assert stop.details["scope"] == "day" and stop.exit_code == PIPELINE_STOPPED_EXIT_CODE == 3
    # 클러스터별 결과: 끝난 것은 끝난 대로, 멈춘 것과 시작하지 않은 것은 사유와 함께
    outcomes = logged["log"]["cluster_outcomes"]
    assert outcomes[103]["status"] == "completed" and outcomes[103]["newsletter_id"] == 1003
    assert outcomes[102]["status"] == "stopped"
    assert outcomes[101] == {"status": "not_started", "failure_reason": "llm_budget_exceeded"}
    assert logged["log"]["llm_stop"]["code"] == "llm_budget_exceeded"
    # 메트릭 요약 파일에도 중단 사유와 지출 스냅숏이 남는다
    summary = json.loads((tmp_path / "logs" / "llm_metrics_run42.json").read_text(encoding="utf-8"))
    assert summary["notes"]["llm_stop"]["scope"] == "day"
    assert summary["notes"]["llm_spend"]["run_id"].startswith("stage5-42-")
    assert summary["notes"]["llm_spend"]["ledger"] == str(tmp_path / "ledger.jsonl")


def test_stage5_starts_a_fresh_run_so_an_earlier_stop_does_not_block_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    budget.latch_run_stop(_stop())  # 같은 프로세스에서 앞선 실행이 멈춘 상태
    fake_app, fake_clusterer, settings = _stage5(lambda state: {"completed_newsletters": [1]})

    with patch("core.clusterer.NewsClusterer", return_value=fake_clusterer), \
         patch("workflow.graph.compile_workflow", return_value=fake_app), \
         patch("db.batch_manager.create_new_batch", return_value=7), \
         patch("db.batch_manager.update_cluster_log"):
        created = Stage5_NewsletterGeneration(settings).execute()

    assert created == 3
    assert budget.current_run_id().startswith("stage5-7-")


def test_metrics_summary_has_no_notes_key_unless_something_was_noted():
    collector = LLMMetricsCollector()
    collector.start_batch()
    assert "notes" not in collector.get_summary()

    collector.note("llm_stop", {"code": "llm_circuit_open"})

    assert collector.get_summary()["notes"] == {"llm_stop": {"code": "llm_circuit_open"}}
    collector.start_batch()
    assert "notes" not in collector.get_summary()


# ---------------------------------------------------------------------------
# 실제 그래프 + 실제 어댑터(페이크 전송): 폴백이 발행되지 않는다
# ---------------------------------------------------------------------------

ARTICLES_STATE = {
    "run_id": 1,
    "all_cluster_groups": {1: [10, 11, 12]},
    "all_cluster_ids": [1],
    "current_cluster_index": 0,
    "data": {
        "ids": [10, 11, 12],
        "titles": ["삼성전자 HBM 증설", "SK하이닉스 투자 확대", "메모리 업황 개선"],
        "contents": ["삼성전자와 SK하이닉스가 HBM 생산 확대에 나섰다. " * 10] * 3,
        "press_names": ["동아일보", "경향신문", "한국경제"],
    },
    "completed_newsletters": [],
    "failed_clusters": [],
    "skipped_clusters": [],
}
CONTENT = "삼성전자와 SK하이닉스가 HBM 생산 확대에 나섰다. " * 5


def _role_transports(gen_responses, judge_responses, on_gen_call=None, on_judge_call=None):
    """레지스트리의 실제 role 클라이언트에 페이크 전송 계층만 끼운다(generator와 tone은 같은 인스턴스).

    그래프의 호출 순서: judge(클러스터 평가) -> generator(본문) -> generator(메타) -> judge(뉴스레터 평가)
    -> tone(문체 변환).
    """
    gen_fake = FakeOpenAIClient(responses=gen_responses, on_call=on_gen_call)
    judge_fake = FakeOpenAIClient(responses=judge_responses, on_call=on_judge_call)
    registry.get_client("generator")._client = gen_fake
    registry.get_client("judge")._client = judge_fake
    assert registry.get_client("tone")._client is gen_fake
    return gen_fake, judge_fake


def _cap_spent_from_call(monkeypatch, nth):
    """전송 계층의 nth번째 요청이 나가는 순간 런 상한을 0으로 만든다 - 그 요청은 끝나고, 다음 예약부터 거부된다."""
    seen = {"n": 0}

    def on_call(_mode, _kwargs):
        seen["n"] += 1
        if seen["n"] == nth:
            monkeypatch.setenv("LLM_BUDGET_RUN_USD", "0")

    return on_call


def _cluster_pass():
    return make_response("{}", parsed=ClusterEval(decision="PASS", confidence=0.9))


def _judge_pass():
    return make_response("{}", parsed=NewsletterEvalV2(
        scores=CriterionScores(faithfulness=5, coverage=4, coherence=4, style=4), unsupported_claims=[], feedback=""))


def _content():
    return make_response("{}", parsed=NewsletterContent(content=CONTENT))


def _meta():
    return make_response("{}", parsed=NewsletterMeta(
        title="반도체 HBM 증설", sentence="메모리 반도체가 새 국면을 맞았습니다",
        keywords=["삼성전자", "SK하이닉스", "HBM", "메모리", "반도체"], categories=["IT/과학"]))


def _tone():
    return make_response("{}", parsed=ToneResult(
        title="📰 반도체 HBM 증설", summary="메모리 반도체 업황이 좋아진대요 ✅",
        content="📰 삼성전자와 SK하이닉스가 HBM 생산을 늘리고 있어요.", keywords=["삼성전자", "SK하이닉스", "HBM"]))


@pytest.fixture
def no_db(monkeypatch):
    saved = []

    class _Conn:
        def cursor(self):
            raise AssertionError("임베딩이 없으므로 cursor()는 불리면 안 된다")

        def commit(self):
            pass

    def save(conn, article_ids, newsletter, run_id=None, generation_history=None):
        saved.append(newsletter)
        return 999

    monkeypatch.setattr(nodes_module, "get_connection", lambda: _Conn())
    monkeypatch.setattr(nodes_module, "release_connection", lambda conn: None)
    monkeypatch.setattr(nodes_module, "save_news_letter", save)
    return saved


def test_graph_completes_under_the_guard_when_the_cap_is_not_reached(no_db):
    """대조군: 같은 페이크로 상한이 넉넉하면 저장까지 간다(아래 중단 테스트가 다른 이유로 통과하는 게 아님)."""
    gen_fake, judge_fake = _role_transports([_content(), _meta(), _tone()], [_cluster_pass(), _judge_pass()])

    final = compile_workflow().invoke(dict(ARTICLES_STATE))

    assert final["completed_newsletters"] == [999]
    assert no_db[0]["title"] == "📰 반도체 HBM 증설"
    assert gen_fake.chat.completions.call_count == 3 and judge_fake.chat.completions.call_count == 2


def test_cap_reached_at_the_tone_step_stops_the_graph_instead_of_saving_a_fallback(no_db, monkeypatch):
    """문체 변환 노드는 실패를 `except Exception`으로 받아 형식체 초안을 대신 저장한다 - 중단은 거기 걸리지 않는다."""
    gen_fake, judge_fake = _role_transports(
        [_content(), _meta()], [_cluster_pass(), _judge_pass()],
        on_judge_call=_cap_spent_from_call(monkeypatch, 2),  # 뉴스레터 평가까지는 나가고, 문체 변환 예약이 거부된다
    )

    with pytest.raises(LLMBudgetExceeded) as exc:
        compile_workflow().invoke(dict(ARTICLES_STATE))

    assert (exc.value.details["scope"], exc.value.details["purpose"]) == ("run", "tone_convert")
    assert no_db == []  # 형식체 초안도, 결정론적 문체 폴백도 저장되지 않았다
    assert gen_fake.chat.completions.call_count == 2  # 본문·메타뿐 - 문체 변환 요청은 나가지 않았다
    assert judge_fake.chat.completions.call_count == 2


def test_cap_reached_at_the_judge_step_stops_instead_of_regenerating(no_db, monkeypatch):
    """판정을 못 받으면 예전 경로는 FAIL로 보고 초안을 다시 생성한다(최대 3회) - 중단은 그 루프에 들어가지 않는다."""
    gen_fake, judge_fake = _role_transports(
        [_content(), _meta()], [_cluster_pass()],
        on_gen_call=_cap_spent_from_call(monkeypatch, 2),  # 메타 호출까지 나가고, 뉴스레터 평가 예약이 거부된다
    )

    with pytest.raises(LLMBudgetExceeded) as exc:
        compile_workflow().invoke(dict(ARTICLES_STATE))

    assert exc.value.details["purpose"] == "newsletter_eval"
    assert no_db == []
    assert gen_fake.chat.completions.call_count == 2 and judge_fake.chat.completions.call_count == 1


def test_cap_reached_at_generation_raises_instead_of_building_a_local_fallback_draft(no_db, monkeypatch):
    """생성기는 LLM 결과가 없으면 기사 제목을 나열한 로컬 초안을 만든다 - 중단은 그 분기에 닿지 않는다."""
    gen_fake, judge_fake = _role_transports(
        [], [_cluster_pass()],
        on_judge_call=_cap_spent_from_call(monkeypatch, 1),  # 클러스터 평가만 나가고, 본문 생성 예약이 거부된다
    )

    with pytest.raises(LLMBudgetExceeded) as exc:
        compile_workflow().invoke(dict(ARTICLES_STATE))

    assert exc.value.details["purpose"] == "newsletter_content_gen"
    assert gen_fake.chat.completions.call_count == 0
    assert no_db == []


def test_payment_required_during_generation_stops_the_graph_without_retrying_the_cluster(no_db):
    """402: 예전에는 로컬 폴백 초안 -> 사실성 게이트 차단 -> 재생성 3회를 돌았다. 이제 한 번에 멈춘다."""
    gen_fake, judge_fake = _role_transports([make_status_error(402, "prepay required")], [_cluster_pass()])

    with pytest.raises(LLMCircuitOpen) as exc:
        compile_workflow().invoke(dict(ARTICLES_STATE))

    assert exc.value.details["http_status"] == 402
    assert gen_fake.chat.completions.call_count == 1 and judge_fake.chat.completions.call_count == 1
    assert no_db == []


# ---------------------------------------------------------------------------
# 잡: job_runs 기록과 종료 코드
# ---------------------------------------------------------------------------

class _Store:
    def __init__(self):
        self.rows, self.unlocked = {}, []

    def try_lock(self, job):
        return True

    def unlock(self, job):
        self.unlocked.append(job)

    def abandon_stale(self, job):
        return 0

    def start(self, job, git_sha):
        self.rows[1] = {"status": "running", "stats": {}, "error": None}
        return 1

    def finish(self, run_id, status, stats, error):
        self.rows[run_id].update(status=status, stats=stats, error=error)

    def update_stats(self, run_id, stats):
        self.rows[run_id]["stats"] = stats

    def close(self):
        pass


def _generate_ctx():
    return JobContext(job="generate", args=argparse.Namespace(limit=None, min_target=None, lookback_hours=None))


def test_generate_job_turns_a_pipeline_stop_into_job_stopped_with_the_partial_result(monkeypatch):
    import pipeline.stages as stages
    from jobs.tasks import generate

    class StoppingStage:
        def __init__(self, settings):
            pass

        def execute(self, **kwargs):
            raise PipelineStopped("llm_budget_exceeded", "LLM 지출 상한(day) $0.3000", newsletters_created=2,
                                  run_id=42, details={"code": "llm_budget_exceeded", "scope": "day"})

    monkeypatch.setattr(stages, "Stage5_NewsletterGeneration", StoppingStage)

    with pytest.raises(JobStopped) as exc:
        generate.run(_generate_ctx())

    assert exc.value.reason == "llm_budget_exceeded"
    assert exc.value.stats["newsletters_created"] == 2
    assert exc.value.stats["llm_stop"] == {"code": "llm_budget_exceeded", "scope": "day"}
    assert "llm_spend" in exc.value.stats and "llm" in exc.value.stats


def test_generate_job_skips_before_clustering_when_the_day_cap_is_already_spent(monkeypatch):
    import pipeline.stages as stages
    from jobs.tasks import generate

    class ExplodingStage:
        def __init__(self, *a, **k):
            raise AssertionError("상한이 이미 찼으면 클러스터링/생성 단계에 들어가면 안 된다")

    monkeypatch.setattr(stages, "Stage5_NewsletterGeneration", ExplodingStage)
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "0")

    with pytest.raises(JobSkipped) as exc:
        generate.run(_generate_ctx())

    assert exc.value.reason == "llm_budget_exhausted"
    assert exc.value.stats["llm_budget"]["scope"] == "day"


@pytest.mark.parametrize("breakage", ["unwritable_ledger", "malformed_cap"])
def test_generate_job_stops_before_clustering_when_no_call_could_be_allowed(monkeypatch, tmp_path, breakage):
    """읽기 전용 마운트(컨테이너 기본)나 잘못된 상한 설정: 어차피 첫 호출이 거부되므로 클러스터링 전에 끝낸다."""
    import pipeline.stages as stages
    from jobs.tasks import generate

    class ExplodingStage:
        def __init__(self, *a, **k):
            raise AssertionError("호출이 전부 거부될 설정이면 클러스터링/생성 단계에 들어가면 안 된다")

    monkeypatch.setattr(stages, "Stage5_NewsletterGeneration", ExplodingStage)
    if breakage == "unwritable_ledger":
        blocker = tmp_path / "read-only-mount"
        blocker.write_text("x")
        monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", str(blocker / "llm_spend_ledger.jsonl"))
    else:
        monkeypatch.setenv("LLM_BUDGET_TOTAL_USD", "three dollars")

    with pytest.raises(JobStopped) as exc:
        generate.run(_generate_ctx())

    assert exc.value.reason == "llm_budget_unavailable"
    assert exc.value.stats["llm_budget"]["problem"] == exc.value.detail


def test_generate_job_reports_spend_in_its_stats_on_success(monkeypatch):
    import pipeline.stages as stages
    from jobs.tasks import generate

    class OkStage:
        def __init__(self, settings):
            pass

        def execute(self, **kwargs):
            return 4

    monkeypatch.setattr(stages, "Stage5_NewsletterGeneration", OkStage)

    stats = generate.run(_generate_ctx())

    assert stats["newsletters_created"] == 4
    assert set(stats["llm_spend"]) >= {"run_usd", "day_usd", "total_usd", "caps_usd", "ledger"}


def test_job_stopped_is_recorded_as_failed_with_its_own_exit_code_and_one_alert():
    store, notified = _Store(), []

    def job(ctx):
        ctx.stats["clusters"] = 9
        raise JobStopped("llm_budget_exceeded", {"newsletters_created": 2, "llm_stop": {"scope": "day"}},
                         detail="LLM 지출 상한(day) $0.3000")

    result = run_job("generate", job, args=argparse.Namespace(), store_factory=lambda: store,
                     notify=notified.append, git_sha="abc")

    assert (result.status, result.exit_code) == ("failed", EXIT_STOPPED) and EXIT_STOPPED == 3
    row = store.rows[1]
    assert row["status"] == "failed"
    assert row["error"].startswith("stopped: llm_budget_exceeded")
    assert row["stats"]["reason"] == "llm_budget_exceeded"
    assert row["stats"]["newsletters_created"] == 2 and row["stats"]["clusters"] == 9
    assert row["stats"]["llm_stop"] == {"scope": "day"}
    assert len(notified) == 1 and "중단" in notified[0] and "llm_budget_exceeded" in notified[0]
    assert store.unlocked == ["generate"]


def test_pipeline_main_exits_with_the_stop_code():
    """ai_workspace/main.py 경로(잡 러너 없이 직접 실행)도 실패(1)와 구분되는 종료 코드로 끝난다."""
    script = """
import sys
sys.argv = ["main.py", "--from-stage", "4", "--to-stage", "5"]
import main
from pipeline.stages import PipelineStopped

class Runner:
    def __init__(self, settings):
        pass
    def run_full_pipeline(self, **kwargs):
        raise PipelineStopped("llm_circuit_open", "HTTP 402", newsletters_created=1, run_id=7, details={})

main.PipelineRunner = Runner
main.main()
"""
    env = {**os.environ, "PYTHONPATH": AI_WORKSPACE, "DB_NAME": "not-test-db"}
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env,
                          cwd=AI_WORKSPACE, timeout=120)

    assert proc.returncode == 3, proc.stderr[-2000:]
    assert "llm_circuit_open" in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# bake-off 러너: 중단은 기록하지 않고 멈춘다(재개 가능)
# ---------------------------------------------------------------------------

def test_bakeoff_recording_client_turns_a_run_stop_into_an_infra_failure():
    from evaluation.llm.bakeoff import InfraFailure, RecordingClient

    class Stopping:
        provider, model = "gemini", GEN

        def complete(self, *a, **k):
            raise _stop("total")

    client = RecordingClient(Stopping())

    with pytest.raises(InfraFailure, match="llm_budget_exceeded"):
        client.complete([{"role": "user", "content": "x"}], purpose="newsletter_content_gen")

    assert client.calls == []
