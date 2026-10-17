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
import argparse
import time
from typing import Any, Dict, List, Tuple

STAGES = ("rss", "extract", "embed")

# 추출 건전성 검사(ADR 0026 결정 9 - ADR 0006 결정 9의 "대상 절반 이상 실패면 실패"를 언론사별로 나눴다).
# 한 언론사만의 실패(그 사이트의 차단·개편)는 경고로 알리고 실행은 성공시킨다. 2026-10-07 ~ 10-16 Tier 0에서는
# 동아일보 한 곳의 다운로드 실패가 fetch_failed의 99.6%였는데 실행 전체를 실패로 만들었다(증거 10).
# 실패시키는 경우는 실패가 한 언론사로 설명되지 않을 때다:
#   (1) 건강하지 않은 언론사가 UNHEALTHY_PRESS_LIMIT곳 이상이거나
#   (2) 실패가 가장 많은 언론사를 빼고도, 받은 기사 EXTRACT_MIN_TARGETS건 이상 중 절반 이상이 실패했을 때.
# 미룬 기사(차단기·시간 예산)는 받지 않았으므로 분모에 넣지 않는다.
EXTRACT_FAILURE_RATIO_LIMIT = 0.5
EXTRACT_MIN_TARGETS = 5
# 건강하지 않은 언론사: 이번 실행에서 차단기가 열렸거나, 받은 기사 PRESS_ALL_FAILED_MIN건 이상 중 절반 이상 실패.
PRESS_ALL_FAILED_MIN = 3
PRESS_FAILURE_RATIO_LIMIT = 0.5
UNHEALTHY_PRESS_LIMIT = 2


def add_arguments(parser) -> None:
    parser.add_argument(
        "--stages", default=",".join(STAGES),
        help="쉼표로 구분한 실행 단계 (기본: rss,extract,embed). 예: 호스트 MPS로 임베딩만 돌릴 때 --stages embed",
    )
    parser.add_argument("--rss-hours", type=int, default=100, help="RSS 항목 발행 시각 cutoff(시간)")
    parser.add_argument("--workers", type=int, default=None, help="본문 추출 프로세스 수 (기본: Settings.PARALLEL_WORKERS)")
    parser.add_argument("--extract-time-budget-s", type=_positive_float, default=None,
                        help="본문 추출 단계 시간 예산(초). 넘으면 새 기사를 보내지 않고 남은 기사는 다음 실행으로 미룸 "
                             "(시도 횟수를 쓰지 않음). 스케줄러는 다음 정시 회차 전에 끝나도록 지정한다")
    parser.add_argument("--embed-batch-size", type=int, default=None, help="임베딩 배치 크기 (기본: Settings.EMBEDDING_BATCH_SIZE)")
    parser.add_argument("--embed-limit", type=int, default=None, help="이번 실행에서 임베딩할 최대 기사 수 (오래된 것부터)")
    parser.add_argument("--embed-time-budget-s", type=float, default=None,
                        help="임베딩 단계 시간 예산(초). 넘으면 새 배치를 시작하지 않음")
    parser.add_argument("--force-cpu", action="store_true", help="GPU/MPS가 있어도 CPU로 임베딩")


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not parsed > 0:
        raise argparse.ArgumentTypeError(f"0보다 커야 한다: {value!r}")
    return parsed


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
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'ok'
                                      AND N.raw_news_content_purged_at IS NULL) AS with_content,
                   COUNT(*) FILTER (WHERE N.raw_news_content_purged_at IS NOT NULL) AS body_purged,
                   COUNT(*) FILTER (WHERE N.embedding_result IS NOT NULL) AS embedded
            FROM news_raw N JOIN press P ON P.press_id = N.press_id
            GROUP BY P.press_name
            ORDER BY P.press_name
            """
        )
        # with_content = 지금 본문이 있는 'ok' 행. 보존 기한(ADR 0023)이 지나 본문을 비운 행은 body_purged로 따로 센다.
        per_press = {
            name: {"total": total, "with_content": with_content, "body_purged": body_purged, "embedded": embedded}
            for name, total, with_content, body_purged, embedded in cur.fetchall()
        }
    return {
        "total": sum(p["total"] for p in per_press.values()),
        "with_content": sum(p["with_content"] for p in per_press.values()),
        "body_purged": sum(p["body_purged"] for p in per_press.values()),
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


def _press_counts(counts: Dict[str, int]) -> Tuple[int, int]:
    """(받은 기사 수, 그중 실패 수). 미룬 기사는 받지 않았으므로 빼고 센다."""
    attempted = sum(n for status, n in counts.items() if status != "deferred")
    failed = counts.get("fetch_failed", 0) + counts.get("error", 0)
    return attempted, failed


def _cause_text(causes: Dict[str, int]) -> str:
    return ", ".join(f"{cause} {n}" for cause, n in sorted(causes.items(), key=lambda kv: (-kv[1], kv[0])))


def check_extract_health(ctx, stats: Dict[str, Any]) -> None:
    per_press = stats.get("per_press") or {}
    causes = stats.get("fetch_fail_causes") or {}
    breaker = stats.get("press_breaker") or {}

    unhealthy: List[str] = []
    for press, counts in sorted(per_press.items()):
        attempted, failed = _press_counts(counts)
        tripped = press in breaker
        if not tripped and not (attempted >= PRESS_ALL_FAILED_MIN and failed / attempted >= PRESS_FAILURE_RATIO_LIMIT):
            continue
        unhealthy.append(press)
        detail = f"{press}: 기사 {attempted}건 중 {failed}건 본문 다운로드 실패"
        if causes.get(press):
            detail += f" (원인: {_cause_text(causes[press])})"
        if tripped:
            detail += f", 연속 실패로 남은 {breaker[press].get('deferred', 0)}건은 다음 실행으로 미룸"
        ctx.warn(detail + " - 차단/사이트 개편 확인")

    budget_deferred = (stats.get("deferred_reasons") or {}).get("time_budget", 0)
    if stats.get("time_budget_exhausted"):
        ctx.warn(f"본문 추출 시간 예산 {stats.get('time_budget_s')}초를 다 써서 {budget_deferred}건을 다음 실행으로 미룸")

    attempted_total = failed_total = 0
    worst, worst_attempted, worst_failed = None, 0, 0
    for press, counts in per_press.items():
        attempted, failed = _press_counts(counts)
        attempted_total += attempted
        failed_total += failed
        if failed > worst_failed:
            worst, worst_attempted, worst_failed = press, attempted, failed
    if attempted_total == 0:
        return
    summary = f"본문 추출 대상 {attempted_total}건 중 {failed_total}건 실패({failed_total / attempted_total:.0%})"
    if len(unhealthy) >= UNHEALTHY_PRESS_LIMIT:
        raise RuntimeError(
            f"{summary} - 언론사 {len(unhealthy)}곳({', '.join(unhealthy)})에서 실패해 한 사이트 문제로 볼 수 없다. "
            "네트워크/차단을 확인하세요"
        )
    rest_attempted, rest_failed = attempted_total - worst_attempted, failed_total - worst_failed
    if rest_attempted >= EXTRACT_MIN_TARGETS and rest_failed / rest_attempted >= EXTRACT_FAILURE_RATIO_LIMIT:
        raise RuntimeError(
            f"{summary} - 가장 많이 실패한 {worst}를 빼도 {rest_attempted}건 중 {rest_failed}건 실패. "
            "네트워크/차단을 확인하세요"
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

        ctx.stats["extract"] = _timed(
            ContentExtractor().extract_parallel_with_stats, args.workers,
            time_budget_s=getattr(args, "extract_time_budget_s", None),
        )
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
