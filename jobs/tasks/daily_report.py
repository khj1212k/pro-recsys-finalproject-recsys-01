"""daily_report: 최근 24시간 잡 실행/수집 현황 요약을 로그(+Slack 웹훅)로 남긴다(읽기 전용)."""
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
        f"- 신규 기사 {news['total']}건 (본문 {news['ok']}, 필터 {news['dropped']}, "
        f"본문없음 {news['empty']}, 다운로드 실패 {news['fetch_failed']}, 임베딩 {news['embedded']})"
    )
    for press, p in sorted(news["per_press"].items()):
        lines.append(f"  · {press}: {p['total']}건 (본문 {p['ok']}, 임베딩 {p['embedded']})")
    totals = report["totals"]
    lines.append(
        f"- 누적 기사 {totals['news_raw']}건 / 임베딩 {totals['embedded']}건 / "
        f"최근 {WINDOW_HOURS}시간 뉴스레터 {totals['newsletters_24h']}건"
    )
    return "\n".join(lines)


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
                   COUNT(*) FILTER (WHERE N.embedding_result IS NOT NULL)
            FROM news_raw N JOIN press P ON P.press_id = N.press_id
            WHERE N.raw_news_crawled_at >= now() - %s::interval
            GROUP BY P.press_name
            """,
            (window,),
        )
        keys = ("total", "ok", "dropped", "empty", "fetch_failed", "embedded")
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

    report = {
        "job_runs": job_runs,
        "news_raw_24h": news_24h,
        "totals": {"news_raw": total, "embedded": embedded, "newsletters_24h": newsletters},
    }
    text = format_report(report)
    print(text)
    report["slack_posted"] = post_message(text)
    return {"report": report}
