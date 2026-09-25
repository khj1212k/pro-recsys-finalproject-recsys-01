"""ingest: RSS 수집 -> 본문 추출 -> BGE-M3 임베딩 (기존 main.py --from-stage 1 --to-stage 3).

세 단계 모두 재실행 안전하다: RSS는 ON CONFLICT DO NOTHING, 추출은
raw_news_extract_status가 비어 있는(또는 재시도 여지가 남은) 기사만, 임베딩은
embedding_result IS NULL인 기사만 처리한다.
"""
import time
from typing import Any, Dict

STAGES = ("rss", "extract", "embed")


def add_arguments(parser) -> None:
    parser.add_argument(
        "--stages", default=",".join(STAGES),
        help="쉼표로 구분한 실행 단계 (기본: rss,extract,embed). 예: 호스트 MPS로 임베딩만 돌릴 때 --stages embed",
    )
    parser.add_argument("--rss-hours", type=int, default=100, help="RSS 항목 발행 시각 cutoff(시간)")
    parser.add_argument("--workers", type=int, default=None, help="본문 추출 프로세스 수 (기본: Settings.PARALLEL_WORKERS)")
    parser.add_argument("--embed-batch-size", type=int, default=None, help="임베딩 배치 크기 (기본: Settings.EMBEDDING_BATCH_SIZE)")
    parser.add_argument("--embed-limit", type=int, default=None, help="이번 실행에서 임베딩할 최대 기사 수 (오래된 것부터)")
    parser.add_argument("--force-cpu", action="store_true", help="GPU/MPS가 있어도 CPU로 임베딩")


def parse_stages(value: str):
    stages = [s.strip() for s in value.split(",") if s.strip()]
    unknown = sorted(set(stages) - set(STAGES))
    if unknown or not stages:
        raise ValueError(f"알 수 없는 ingest 단계: {unknown or value!r} (가능: {', '.join(STAGES)})")
    return [s for s in STAGES if s in stages]


def _timed(fn, *args, **kwargs) -> Dict[str, Any]:
    started = time.monotonic()
    result = fn(*args, **kwargs)
    result = dict(result)
    result["duration_s"] = round(time.monotonic() - started, 3)
    return result


def news_raw_snapshot() -> Dict[str, Any]:
    """실행 직후 news_raw 누적 현황(언론사별) - 수집이 실제로 쌓이고 있는지 한눈에 보기 위함."""
    from db.connection import get_db_connection

    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT P.press_name,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'ok') AS with_content,
                   COUNT(*) FILTER (WHERE N.embedding_result IS NOT NULL) AS embedded
            FROM news_raw N JOIN press P ON P.press_id = N.press_id
            GROUP BY P.press_name
            ORDER BY P.press_name
            """
        )
        per_press = {
            name: {"total": total, "with_content": with_content, "embedded": embedded}
            for name, total, with_content, embedded in cur.fetchall()
        }
    return {
        "total": sum(p["total"] for p in per_press.values()),
        "with_content": sum(p["with_content"] for p in per_press.values()),
        "embedded": sum(p["embedded"] for p in per_press.values()),
        "per_press": per_press,
    }


def run(ctx) -> Dict[str, Any]:
    from config.settings import Settings

    args = ctx.args
    stages = parse_stages(args.stages)
    ctx.stats["stages"] = stages

    if "rss" in stages:
        from crawler.rss_collector import collect_rss

        rss = _timed(collect_rss, hours=args.rss_hours)
        ctx.stats["rss"] = rss
        if rss["per_feed"] and rss["failed_feeds"] == len(rss["per_feed"]):
            raise RuntimeError(f"모든 RSS 피드({rss['failed_feeds']}개) 수집 실패 - 네트워크/DNS를 확인하세요")

    if "extract" in stages:
        from crawler.content_extractor import ContentExtractor

        ctx.stats["extract"] = _timed(ContentExtractor().extract_parallel_with_stats, args.workers)

    if "embed" in stages:
        from pipeline.stages import embed_pending_articles

        embed = _timed(
            embed_pending_articles, Settings,
            force_cpu=args.force_cpu, batch_size=args.embed_batch_size, limit=args.embed_limit,
        )
        ctx.stats["embed"] = embed
        if embed.get("error") or (embed["targets"] and not embed["embedded"]):
            raise RuntimeError(
                f"임베딩 실패: 대상 {embed['targets']}건 중 0건 저장 "
                f"(failed_batches={embed['failed_batches']}, error={embed.get('error')})"
            )

    ctx.stats["news_raw"] = news_raw_snapshot()
    return {}
