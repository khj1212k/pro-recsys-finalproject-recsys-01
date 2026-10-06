"""generate: HDBSCAN 클러스터링 + LangGraph 뉴스레터 생성 (기존 main.py --from-stage 4 --to-stage 5).

LLM 비용 가드 (docs/adr/0035):
- 시작 전: LLM 클라이언트(core/llm/)와 같은 킬 스위치 규칙(env LLM_KILL_SWITCH 또는
  LLM_KILL_SWITCH_FILE)을 한 번 확인하고, 지출 원장의 일·전체 상한이 이미 찼는지도 본다. 어느
  쪽이든 클러스터링과 cluster_history 기록까지 하지 않고 'skipped'로 끝낸다. 상한 설정이나 원장을
  읽고 쓸 수 없으면(예: 읽기 전용 마운트) 모든 호출이 거부될 것이므로 같은 시점에 JobStopped로 끝낸다.
- 실행 중: 호출마다 클라이언트가 최악 비용을 원장에 예약한다. 상한에 닿거나 서킷브레이커가
  열리면 Stage5가 그때까지의 결과를 남기고 PipelineStopped를 내고, 이 잡은 JobStopped로 바꿔
  job_runs에 사유(stats.reason, stats.llm_stop)와 함께 'failed', 종료 코드 3으로 끝난다.
"""
from typing import Any, Dict

from jobs.runtime import JobSkipped, JobStopped

_LLM_SUMMARY_KEYS = ("total_calls", "successful_calls", "failed_calls", "total_input_tokens",
                     "total_output_tokens", "avg_latency_seconds", "by_model")


def add_arguments(parser) -> None:
    parser.add_argument("--limit", type=int, default=None, help="처리할 클러스터 최대 개수")
    parser.add_argument("--min-target", type=int, default=None, help="최소 생성 목표 (기본: Settings.MIN_NEWSLETTER_TARGET)")
    parser.add_argument("--lookback-hours", type=int, default=None, help="클러스터링 lookback (기본: Settings.CLUSTER_LOOKBACK_HOURS)")


def _llm_stats() -> Dict[str, Any]:
    from core.llm import budget
    from core.llm_metrics import get_metrics_collector

    llm = get_metrics_collector().get_summary()
    return {
        "llm": {key: llm.get(key) for key in _LLM_SUMMARY_KEYS},
        # 이번 런·오늘·전체 사용액(정산 + 열린 예약)과 상한. 원장을 못 읽으면 {"error": ...}
        "llm_spend": budget.spend_snapshot(),
    }


def run(ctx) -> Dict[str, Any]:
    from core.llm import budget
    from core.llm.kill_switch import kill_switch_reason

    reason = kill_switch_reason()
    if reason is not None:
        raise JobSkipped("llm_kill_switch", {"kill_switch": reason})
    problem = budget.preflight_problem()
    if problem is not None:
        # 건너뜀이 아니라 중단(알림 있음): 설정을 고치지 않으면 매번 같은 이유로 아무것도 만들지 못한다.
        raise JobStopped("llm_budget_unavailable", {"llm_budget": {"problem": problem}}, detail=problem)
    exhausted = budget.exhausted_scope()
    if exhausted is not None:
        raise JobSkipped("llm_budget_exhausted", {"llm_budget": exhausted})

    from config.settings import Settings
    from pipeline.stages import PipelineStopped, Stage5_NewsletterGeneration

    args = ctx.args
    try:
        created = Stage5_NewsletterGeneration(Settings).execute(
            limit=args.limit, min_target=args.min_target, lookback_hours=args.lookback_hours,
        )
    except PipelineStopped as stop:
        raise JobStopped(
            stop.reason,
            {"newsletters_created": stop.newsletters_created, "llm_stop": stop.details, **_llm_stats()},
            detail=stop.detail,
        ) from stop
    return {"newsletters_created": created, **_llm_stats()}
