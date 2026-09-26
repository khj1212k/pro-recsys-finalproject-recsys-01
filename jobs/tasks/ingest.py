"""ingest: RSS 수집 -> 본문 추출 -> BGE-M3 임베딩 (기존 main.py --from-stage 1 --to-stage 3).

rss 단계는 정책브리핑 정책뉴스(공공누리 제1유형, Open API)도 함께 수집한다(ADR 0023). 본문을
API가 주므로 extract 단계를 거치지 않는다. 인증키(DATA_GO_KR_SERVICE_KEY)가 없으면 건너뛰고,
실패해도 RSS 결과는 그대로 두고 경고만 남긴다.

스케줄러는 `--stages rss,extract`로 수집만 돌리고 임베딩은 embed 잡이 따로 한다(docker/crontab).
세 단계를 한 번에 도는 기본값은 수동 부트스트랩용이다 - 스케줄러의 embed 잡과 동시에 돌리면
같은 기사를 두 번 임베딩할 수 있다(결과는 같고 CPU만 낭비).

세 단계 모두 재실행 안전하다: RSS는 ON CONFLICT DO NOTHING, 추출은
raw_news_extract_status가 비어 있는(또는 재시도 여지가 남은) 기사만, 임베딩은
embedding_result IS NULL인 기사만 처리한다.
"""
import time
from typing import Any, Dict

STAGES = ("rss", "extract", "embed")

# 본문 다운로드 실패(fetch_failed + error)가 대상의 절반 이상이면 네트워크/차단 문제로 보고 실패시킨다.
# 2026-09-26 첫날 ingest 9회 실측은 fetch_failed·error 0건 - 정상 수집에서는 걸리지 않는다.
EXTRACT_FAILURE_RATIO_LIMIT = 0.5
EXTRACT_MIN_TARGETS = 5
# 한 언론사 기사가 이 건수 이상 전부 실패하면(사이트 개편·차단) 실패는 아니어도 경고한다.
PRESS_ALL_FAILED_MIN = 3


def add_arguments(parser) -> None:
    parser.add_argument(
        "--stages", default=",".join(STAGES),
        help="쉼표로 구분한 실행 단계 (기본: rss,extract,embed). 예: 호스트 MPS로 임베딩만 돌릴 때 --stages embed",
    )
    parser.add_argument("--rss-hours", type=int, default=100, help="RSS 항목 발행 시각 cutoff(시간)")
    parser.add_argument("--workers", type=int, default=None, help="본문 추출 프로세스 수 (기본: Settings.PARALLEL_WORKERS)")
    parser.add_argument("--embed-batch-size", type=int, default=None, help="임베딩 배치 크기 (기본: Settings.EMBEDDING_BATCH_SIZE)")
    parser.add_argument("--embed-limit", type=int, default=None, help="이번 실행에서 임베딩할 최대 기사 수 (오래된 것부터)")
    parser.add_argument("--embed-time-budget-s", type=float, default=None,
                        help="임베딩 단계 시간 예산(초). 넘으면 새 배치를 시작하지 않음")
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


def collect_policy_briefing_safely(ctx) -> Dict[str, Any]:
    """정책브리핑 수집. 이 출처의 실패(인증키·API 오류·네트워크)는 잡을 실패시키지 않고
    stats에 에러와 경고로만 남긴다 - pipeline/stages.py의 Stage1_RSSCollection과 같은 규칙."""
    from crawler.policy_briefing import collect_policy_briefing

    started = time.monotonic()
    try:
        result = dict(collect_policy_briefing())
    except Exception as e:  # noqa: BLE001
        result = {"error": f"{type(e).__name__}: {e}"[:300]}
        ctx.warn(f"정책브리핑 수집 실패 (RSS 수집 결과는 유지): {result['error']}")
    result["duration_s"] = round(time.monotonic() - started, 3)
    return result


def check_extract_health(ctx, stats: Dict[str, Any]) -> None:
    for press, counts in sorted((stats.get("per_press") or {}).items()):
        attempted = sum(counts.values())
        failed = counts.get("fetch_failed", 0) + counts.get("error", 0)
        if attempted >= PRESS_ALL_FAILED_MIN and failed == attempted:
            ctx.warn(f"{press}: 기사 {attempted}건 본문 다운로드 전부 실패 (차단/사이트 개편 확인)")

    targets = stats.get("targets", 0)
    failed = stats.get("fetch_failed", 0) + stats.get("error", 0)
    if targets >= EXTRACT_MIN_TARGETS and failed / targets >= EXTRACT_FAILURE_RATIO_LIMIT:
        raise RuntimeError(
            f"본문 추출 대상 {targets}건 중 {failed}건 실패({failed / targets:.0%}) - 네트워크/차단을 확인하세요"
        )


def run(ctx) -> Dict[str, Any]:
    from config.settings import Settings

    args = ctx.args
    stages = parse_stages(args.stages)
    ctx.stats["stages"] = stages

    if "rss" in stages:
        from crawler.rss_collector import collect_rss

        rss = _timed(collect_rss, hours=args.rss_hours)
        ctx.stats["rss"] = rss
        ctx.stats["policy_briefing"] = collect_policy_briefing_safely(ctx)
        if rss["per_feed"] and rss["failed_feeds"] == len(rss["per_feed"]):
            raise RuntimeError(f"모든 RSS 피드({rss['failed_feeds']}개) 수집 실패 - 네트워크/DNS를 확인하세요")
        ctx.checkpoint()

    if "extract" in stages:
        from crawler.content_extractor import ContentExtractor

        ctx.stats["extract"] = _timed(ContentExtractor().extract_parallel_with_stats, args.workers)
        ctx.checkpoint()
        check_extract_health(ctx, ctx.stats["extract"])

    if "embed" in stages:
        from jobs.tasks.embed import run_embedding

        started = time.monotonic()
        run_embedding(
            ctx, limit=args.embed_limit, time_budget_s=args.embed_time_budget_s,
            batch_size=args.embed_batch_size, force_cpu=args.force_cpu,
        )
        ctx.stats["embed"]["duration_s"] = round(time.monotonic() - started, 3)

    ctx.stats["news_raw"] = news_raw_snapshot()
    return {}
