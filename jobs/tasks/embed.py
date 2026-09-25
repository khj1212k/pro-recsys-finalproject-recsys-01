"""embed: 본문이 있고 임베딩이 없는 기사를 BGE-M3로 임베딩한다(수집 잡과 락을 따로 쓴다).

CPU 임베딩은 백로그가 크면 몇 시간 걸릴 수 있다. ingest(rss,extract)와 같은 잡으로 묶으면
그동안 ingest 락이 잡혀 수집 실행이 건너뛰어지므로 별도 잡으로 두고, --time-budget-s로
한 번의 실행 시간을 제한한다(남은 기사는 다음 실행이 오래된 것부터 이어서 처리).
"""
from typing import Any, Dict


def add_arguments(parser) -> None:
    parser.add_argument("--limit", type=int, default=None, help="이번 실행에서 임베딩할 최대 기사 수 (오래된 것부터)")
    parser.add_argument("--time-budget-s", type=float, default=None,
                        help="인코딩 시작 후 이 시간(초)이 지나면 새 배치를 시작하지 않음")
    parser.add_argument("--batch-size", type=int, default=None, help="임베딩 배치 크기 (기본: Settings.EMBEDDING_BATCH_SIZE)")
    parser.add_argument("--force-cpu", action="store_true", help="GPU/MPS가 있어도 CPU로 임베딩")


def run_embedding(ctx, *, limit=None, time_budget_s=None, batch_size=None, force_cpu=False) -> Dict[str, Any]:
    """ingest의 embed 단계와 embed 잡이 공유한다. 진행 상황은 ctx.stats["embed"]에 바로 쌓인다."""
    from config.settings import Settings
    import pipeline.stages as stages

    progress = ctx.stats.setdefault("embed", {})
    stats = stages.embed_pending_articles(
        Settings, force_cpu=force_cpu, batch_size=batch_size, limit=limit,
        time_budget_s=time_budget_s, stats=progress,
    )
    if stats.get("error") or (stats["targets"] and not stats["embedded"]):
        raise RuntimeError(
            f"임베딩 실패: 대상 {stats['targets']}건 중 0건 저장 "
            f"(failed_batches={stats['failed_batches']}, error={stats.get('error')})"
        )
    return stats


def run(ctx) -> Dict[str, Any]:
    args = ctx.args
    run_embedding(
        ctx, limit=args.limit, time_budget_s=args.time_budget_s,
        batch_size=args.batch_size, force_cpu=args.force_cpu,
    )
    return {}
