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
from typing import Any, Dict, List, Optional, Tuple

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

# 같은 본문 반복 감지(ADR 0026 결정 9, 2026-10-25 절). 국민일보는 2026-10-19 페이지 템플릿이 바뀐 뒤 trafilatura가 모든
# 기사에서 같은 특집 글을 뽑았고, 본문 해시 중복 제거가 나머지를 duplicate로 지웠다. 다운로드는 성공하므로 위 실패 규칙에
# 걸리지 않아 ingest는 매시 succeeded로 끝났다(10-25까지 엿새). 한 언론사에서 본문을 받은(ok·dropped·empty·duplicate)
# 기사가 SAME_BODY_MIN_ARTICLES건 이상이고 그 중 SAME_BODY_RATIO_LIMIT 이상이 duplicate면 경고한다.
# 같은 기준을 Tier 0의 ingest 기록에 대 봤다(job_runs.stats, 읽기만): 개편 전(2026-10-07 ~ 10-19 05시 UTC) 235회 x 언론사
# 8곳 = 1,323건에서 걸린 곳이 0이고(한 회차 한 언론사의 최대 duplicate 7건), 개편 뒤 국민일보는 123회 중 44회가 걸렸다
# (첫 감지 10-20 03:05 UTC - 개편 뒤 약 22시간. 개편 직후 회차들은 특집 글이 기사마다 본문과 섞여 해시가 서로 달랐다).
# 다른 언론사는 개편 뒤 714건에서도 0이다. 잡을 실패시키지 않는다: 추출·저장은 정상이고 다음 실행에 영향이 없으며
# 되돌리는 방법이 있다(scripts/oci/fix_kmib_body_extraction.sql). 다운로드 실패와 달리 '건강하지 않은 언론사' 수
# (UNHEALTHY_PRESS_LIMIT)에도 넣지 않는다.
SAME_BODY_MIN_ARTICLES = 5
SAME_BODY_RATIO_LIMIT = 0.5
SAME_BODY_LABEL = "같은 본문 반복 - 추출 이상 의심"
_PARSED_STATUSES = ("ok", "dropped", "empty", "duplicate")

# --extract-deadline-s는 ingest 시작부터 잰다 - RSS 단계가 늦을수록 추출 시간이 준다. RSS가 마감을 거의 다 써도
# 추출은 이만큼은 돈다(앞서 실패한 기사만 계속 미뤄지지 않게).
EXTRACT_MIN_BUDGET_S = 60.0
_clock = time.monotonic  # 테스트에서 바꿔 끼운다


def add_arguments(parser) -> None:
    parser.add_argument(
        "--stages", default=",".join(STAGES),
        help="쉼표로 구분한 실행 단계 (기본: rss,extract,embed). 예: 호스트 MPS로 임베딩만 돌릴 때 --stages embed",
    )
    parser.add_argument("--rss-hours", type=int, default=100, help="RSS 항목 발행 시각 cutoff(시간)")
    parser.add_argument("--workers", type=int, default=None, help="본문 추출 프로세스 수 (기본: Settings.PARALLEL_WORKERS)")
    parser.add_argument("--extract-deadline-s", type=_positive_float, default=None,
                        help="본문 추출 마감(초, ingest 시작부터 잰다). 지나면 새 기사를 보내지 않고 남은 기사는 다음 실행으로 "
                             "미룸(시도 횟수를 쓰지 않음). RSS 단계가 길어지면 그만큼 추출 시간이 준다(최소 60초). "
                             "스케줄러는 다음 정시 회차 전에 끝나도록 지정한다")
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


def warn_failed_feeds(ctx, rss: Dict[str, Any]) -> None:
    """RSS 단계에서 읽지 못한 피드마다 경고한다(ADR 0026 결정 9).

    피드를 못 읽으면 그 언론사의 이번 회차 신규 기사가 0건이라 추출 단계에 그 언론사 대상이 없을 수 있고, 그러면
    추출 경고도 나지 않는다. 2026-10-16 ~ 10-17 Tier 0에서 동아일보 RSS가 RemoteDisconnected로 실패한 회차 다수는
    추출 대상에 동아일보가 없어 succeeded·경고 없음으로 끝났다 - 수집량이 0인데 알림이 없었다.
    모든 피드가 실패한 경우는 run()이 실패로 끝내므로 여기까지 오지 않는다.

    피드가 여럿인 언론사(국민일보 여섯, 전자신문 셋)는 언론사마다 한 줄로 묶는다: 모두 실패하면 "피드 N개 모두 실패 -
    이번 회차 신규 기사 0건", 일부만 실패하면 실패한 피드 이름을 적고 신규 0건이라고 하지 않는다."""
    by_press: Dict[str, List[Tuple[str, Optional[str]]]] = {}
    for feed, feed_stats in (rss.get("per_feed") or {}).items():
        feed_stats = feed_stats or {}
        by_press.setdefault(feed_stats.get("press") or feed, []).append((feed, feed_stats.get("error")))
    for press, feeds in by_press.items():
        failed = [(feed, error) for feed, error in feeds if error]
        if not failed:
            continue
        # 원인 종류만 적는다: "RemoteDisconnected: Remote end closed ..." -> "RemoteDisconnected",
        # "feed unreadable (HTTP 403): ..." -> "feed unreadable (HTTP 403)"
        kinds = ", ".join(dict.fromkeys(str(error).split(":", 1)[0].strip()[:80] for _, error in failed))
        if len(feeds) == 1:
            ctx.warn(f"{feeds[0][0]}: RSS 수집 실패 ({kinds}) - 이번 회차 신규 기사 0건")
        elif len(failed) == len(feeds):
            ctx.warn(f"{press}: RSS 피드 {len(feeds)}개 모두 실패 ({kinds}) - 이번 회차 신규 기사 0건")
        else:
            names = ", ".join(feed for feed, _ in failed)
            ctx.warn(f"{press}: RSS 피드 {len(feeds)}개 중 {len(failed)}개 실패 ({kinds}) - {names}")


def _press_counts(counts: Dict[str, int]) -> Tuple[int, int]:
    """(받은 기사 수, 그중 실패 수). 미룬 기사는 받지 않았으므로 빼고 센다."""
    attempted = sum(n for status, n in counts.items() if status != "deferred")
    failed = counts.get("fetch_failed", 0) + counts.get("error", 0)
    return attempted, failed


def _same_body_repeats(per_press: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, Any]]:
    """한 실행 안에서 본문을 받은 기사 중 duplicate가 SAME_BODY_RATIO_LIMIT 이상인 언론사(받은 기사 SAME_BODY_MIN_ARTICLES건
    이상일 때만) -> job_runs.stats에 남기는 항목. 다운로드 실패·오류·미룬 기사는 분모에 넣지 않는다."""
    suspects: Dict[str, Dict[str, Any]] = {}
    for press, counts in sorted(per_press.items()):
        parsed = sum(counts.get(status, 0) for status in _PARSED_STATUSES)
        duplicate = counts.get("duplicate", 0)
        if parsed >= SAME_BODY_MIN_ARTICLES and duplicate / parsed >= SAME_BODY_RATIO_LIMIT:
            suspects[press] = {"reason": "same_body_repeated", "label": SAME_BODY_LABEL,
                               "parsed": parsed, "duplicate": duplicate}
    return suspects


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

    for press, found in sorted((stats.get("body_container") or {}).items()):
        if found.get("container_missing"):
            ctx.warn(
                f"{press}: 본문 컨테이너를 못 찾아 전체 페이지로 추출한 기사 {found['container_missing']}건 "
                f"(컨테이너로 추출 {found.get('container', 0)}건) - 페이지 개편 확인"
            )

    suspects = _same_body_repeats(per_press)
    if suspects:
        stats["suspect_press"] = suspects  # 언론사별 통계: daily_report가 읽는다
    for press, found in suspects.items():
        ctx.warn(
            f"{press}: 본문을 받은 기사 {found['parsed']}건 중 {found['duplicate']}건이 duplicate - "
            f"{SAME_BODY_LABEL} (본문 컨테이너·페이지 개편 확인)"
        )

    budget_deferred = (stats.get("deferred_reasons") or {}).get("time_budget", 0)
    if stats.get("time_budget_exhausted"):
        deadline = stats.get("deadline_s")
        when = f"(ingest 시작 뒤 {deadline:g}초 마감)" if deadline else ""
        ctx.warn(f"본문 추출 시간 예산 {stats.get('time_budget_s'):g}초{when}를 다 써서 {budget_deferred}건을 다음 실행으로 미룸")

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

    job_started = _clock()
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
        warn_failed_feeds(ctx, rss)
        ctx.checkpoint()

    if "extract" in stages:
        from crawler.content_extractor import ContentExtractor

        deadline_s = getattr(args, "extract_deadline_s", None)
        budget_s = None
        if deadline_s is not None:
            # 다음 정시 회차와 겹치지 않게 하는 것은 추출 단계 길이가 아니라 잡이 끝나는 시각이다(ADR 0026 결정 9)
            budget_s = round(max(EXTRACT_MIN_BUDGET_S, deadline_s - (_clock() - job_started)), 1)
        ctx.stats["extract"] = _timed(
            ContentExtractor().extract_parallel_with_stats, args.workers, time_budget_s=budget_s,
        )
        ctx.stats["extract"]["deadline_s"] = deadline_s
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
