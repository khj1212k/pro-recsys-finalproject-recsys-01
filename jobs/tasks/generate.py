"""generate: HDBSCAN 클러스터링 + LangGraph 뉴스레터 생성 (기존 main.py --from-stage 4 --to-stage 5).

LLM 비용 가드: LLM 클라이언트(core/llm/)와 같은 킬 스위치 규칙(env LLM_KILL_SWITCH
또는 LLM_KILL_SWITCH_FILE)을 시작 전에 한 번 확인해, 켜져 있으면 클러스터링과
cluster_history 기록까지 하지 않고 'skipped'로 끝낸다. 실행 도중에 켜지면 그 뒤
호출은 클라이언트가 막는다(별도 메커니즘 없음).
"""
from typing import Any, Dict

from jobs.runtime import JobSkipped


def add_arguments(parser) -> None:
    parser.add_argument("--limit", type=int, default=None, help="처리할 클러스터 최대 개수")
    parser.add_argument("--min-target", type=int, default=None, help="최소 생성 목표 (기본: Settings.MIN_NEWSLETTER_TARGET)")
    parser.add_argument("--lookback-hours", type=int, default=None, help="클러스터링 lookback (기본: Settings.CLUSTER_LOOKBACK_HOURS)")


def run(ctx) -> Dict[str, Any]:
    from core.llm.kill_switch import kill_switch_reason

    reason = kill_switch_reason()
    if reason is not None:
        raise JobSkipped("llm_kill_switch", {"kill_switch": reason})

    from config.settings import Settings
    from core.llm_metrics import get_metrics_collector
    from pipeline.stages import Stage5_NewsletterGeneration

    args = ctx.args
    created = Stage5_NewsletterGeneration(Settings).execute(
        limit=args.limit, min_target=args.min_target, lookback_hours=args.lookback_hours,
    )
    llm = get_metrics_collector().get_summary()
    return {
        "newsletters_created": created,
        "llm": {
            key: llm.get(key)
            for key in ("total_calls", "successful_calls", "failed_calls", "total_input_tokens",
                        "total_output_tokens", "avg_latency_seconds", "by_model")
        },
    }
