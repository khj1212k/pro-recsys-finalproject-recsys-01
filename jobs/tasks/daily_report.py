"""daily_report: 최근 24시간 잡 실행/수집/추천 응답 현황 요약을 로그(+Slack 웹훅)로 남긴다(읽기 전용)."""
from typing import Any, Dict, List

WINDOW_HOURS = 24


def format_report(report: Dict[str, Any]) -> str:
    lines: List[str] = [f"📊 뉴스레터 파이프라인 일일 리포트 (최근 {WINDOW_HOURS}시간)"]
    for job, by_status in sorted(report["job_runs"].items()):
        counts = ", ".join(f"{status} {n}" for status, n in sorted(by_status.items()))
        lines.append(f"- job {job}: {counts}")
    if not report["job_runs"]:
        lines.append("- 실행된 잡 없음 (스케줄러가 멈췄는지 확인)")
    news = report["news_raw_24h"]
    lines.append(
        f"- 신규 기사 {news['total']}건 (본문 {news['ok']}, 필터 {news['dropped']}, 중복 {news['duplicate']}, "
        f"본문없음 {news['empty']}, 다운로드 실패 {news['fetch_failed']}, 추출 오류 {news['error']}, "
        f"임베딩 {news['embedded']})"
    )
    for press, p in sorted(news["per_press"].items()):
        lines.append(f"  · {press}: {p['total']}건 (본문 {p['ok']}, 임베딩 {p['embedded']})")
    totals = report["totals"]
    lines.append(
        f"- 누적 기사 {totals['news_raw']}건 / 임베딩 {totals['embedded']}건 / "
        f"최근 {WINDOW_HOURS}시간 뉴스레터 {totals['newsletters_24h']}건"
    )
    recsys = report.get("recsys_24h")
    if recsys is not None:
        by_source = sorted(recsys["by_source"].items(), key=lambda kv: (-kv[1]["responses"], kv[0]))
        total = sum(v["responses"] for _, v in by_source)
        if total:
            detail = ", ".join(f"{source} {v['responses']}" for source, v in by_source)
            lines.append(f"- 추천 응답 {total}건 (노출 로그 기준: {detail})")
        else:
            lines.append("- 추천 응답 0건 (노출 로그 기준)")
    return "\n".join(lines)


def impression_sources(cur, window: str) -> Dict[str, Dict[str, int]]:
    """노출 로그에서 출처(X-Rec-Source)별 응답 수와 노출 항목 수를 센다.

    GET /recsys/stats의 카운터는 프로세스 안의 값이라 재시작하면 사라지고 워커마다 따로다.
    기간별 출처 분포(실시간 계산이 얼마나 폴백으로 떨어졌는지)는 이 로그에서 본다. 다만 항목을
    하나 이상 보여 주고 로그 쓰기까지 성공한 응답만 여기 잡힌다 - 빈 응답과 유실된 쓰기는 없다.
    """
    cur.execute(
        """
        SELECT source, COUNT(DISTINCT request_id), COUNT(*)
        FROM recommendation_impression_log
        WHERE created_at >= now() - %s::interval
        GROUP BY source
        """,
        (window,),
    )
    return {source: {"responses": responses, "items": items} for source, responses, items in cur.fetchall()}


def run(ctx) -> Dict[str, Any]:
    from db.connection import get_db_connection
    from jobs.notify import post_message

    window = f"{WINDOW_HOURS} hours"
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT job, status, COUNT(*) FROM job_runs
            WHERE started_at >= now() - %s::interval
            GROUP BY job, status
            """,
            (window,),
        )
        job_runs: Dict[str, Dict[str, int]] = {}
        for job, status, n in cur.fetchall():
            job_runs.setdefault(job, {})[status] = n

        cur.execute(
            """
            SELECT P.press_name,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'ok'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'dropped'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'empty'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'fetch_failed'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'error'),
                   COUNT(*) FILTER (WHERE N.raw_news_extract_status = 'duplicate'),
                   COUNT(*) FILTER (WHERE N.embedding_result IS NOT NULL)
            FROM news_raw N JOIN press P ON P.press_id = N.press_id
            WHERE N.raw_news_crawled_at >= now() - %s::interval
            GROUP BY P.press_name
            """,
            (window,),
        )
        keys = ("total", "ok", "dropped", "empty", "fetch_failed", "error", "duplicate", "embedded")
        per_press = {row[0]: dict(zip(keys, row[1:])) for row in cur.fetchall()}
        news_24h = {k: sum(p[k] for p in per_press.values()) for k in keys}
        news_24h["per_press"] = per_press

        cur.execute(
            """
            SELECT (SELECT COUNT(*) FROM news_raw),
                   (SELECT COUNT(*) FROM news_raw WHERE embedding_result IS NOT NULL),
                   (SELECT COUNT(*) FROM news_letter WHERE news_letter_created_at >= now() - %s::interval)
            """,
            (window,),
        )
        total, embedded, newsletters = cur.fetchone()
        recsys_by_source = impression_sources(cur, window)

    report = {
        "job_runs": job_runs,
        "news_raw_24h": news_24h,
        "totals": {"news_raw": total, "embedded": embedded, "newsletters_24h": newsletters},
        "recsys_24h": {"by_source": recsys_by_source},
    }
    text = format_report(report)
    print(text)
    report["slack_posted"] = post_message(text)
    return {"report": report}
