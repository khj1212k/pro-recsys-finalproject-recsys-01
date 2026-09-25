import sys
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"),
)


def _nl(id_, created_at, raw_news_count, title="t"):
    return SimpleNamespace(
        news_letter_id=id_, news_letter_created_at=created_at, raw_news_count=raw_news_count, news_letter_title=title
    )


def test_compute_scores_ranks_fresher_and_more_popular_higher():
    from scheduler.calculate_ranking import compute_scores

    cutoff = datetime(2026, 1, 15, 8, 45, tzinfo=timezone.utc)
    newsletters = [
        _nl(1, cutoff - timedelta(hours=1), raw_news_count=2),   # 신선하지만 인기 낮음
        _nl(2, cutoff - timedelta(hours=47), raw_news_count=50),  # 오래됐지만 인기 높음
        _nl(3, cutoff - timedelta(hours=1), raw_news_count=50),   # 신선하고 인기 높음 -> 1위
    ]

    scored = compute_scores(newsletters, cutoff)

    assert [s["id"] for s in scored][0] == 3
    assert scored == sorted(scored, key=lambda x: x["score"], reverse=True)


def test_compute_scores_clamps_negative_age_to_zero():
    from scheduler.calculate_ranking import compute_scores

    cutoff = datetime(2026, 1, 15, 8, 45, tzinfo=timezone.utc)
    # created_at이 cutoff보다 미래인 비정상 케이스 (시계 오차 등)
    future_nl = _nl(1, cutoff + timedelta(hours=5), raw_news_count=1)

    scored = compute_scores([future_nl], cutoff)

    assert scored[0]["age"] == 0


def test_kst_day_utc_bounds_covers_exactly_one_kst_calendar_day():
    from scheduler.calculate_ranking import get_kst_day_utc_bounds

    korea_tz = timezone(timedelta(hours=9))
    cutoff_kst = datetime(2026, 1, 15, 17, 45, tzinfo=korea_tz)

    start_utc, end_utc = get_kst_day_utc_bounds(cutoff_kst)

    # KST 2026-01-15 00:00 == UTC 2026-01-14 15:00
    assert start_utc == datetime(2026, 1, 14, 15, 0, tzinfo=timezone.utc)
    assert end_utc == datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)
    # cutoff 자체(2026-01-15 17:45 KST = 08:45 UTC)는 그 구간 안에 있어야 함
    cutoff_utc = cutoff_kst.astimezone(timezone.utc)
    assert start_utc <= cutoff_utc < end_utc
