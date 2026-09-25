"""jobs/tasks/* 중 DB 없이 검증 가능한 동작(인자 해석, 킬 스위치 건너뜀, 폴백 목록 구성)."""
import argparse
from datetime import datetime, timezone

import pytest

from jobs.run import JOBS, build_parser, main
from jobs.runtime import JobContext, JobSkipped


def test_every_registered_job_parses_with_defaults():
    parser = build_parser()
    for name in JOBS:
        args = parser.parse_args([name])
        assert args.job == name


def test_unknown_job_is_a_usage_error_with_exit_code_2():
    with pytest.raises(SystemExit) as exc:
        main(["no_such_job"])
    assert exc.value.code == 2


def test_ingest_stages_keep_pipeline_order_and_reject_unknown():
    from jobs.tasks.ingest import parse_stages

    assert parse_stages("embed,rss") == ["rss", "embed"]
    with pytest.raises(ValueError):
        parse_stages("rss,crawl")


def test_ingest_fails_loudly_when_every_rss_feed_failed(monkeypatch):
    from jobs.tasks import ingest

    def fake_collect_rss(hours):
        return {"inserted": 0, "skipped": 0, "failed_feeds": 2,
                "per_feed": {"a": {"error": "dns"}, "b": {"error": "dns"}}}

    import crawler.rss_collector as rss_module
    monkeypatch.setattr(rss_module, "collect_rss", fake_collect_rss)

    parser = build_parser()
    ctx = JobContext(job="ingest", args=parser.parse_args(["ingest", "--stages", "rss"]))
    with pytest.raises(RuntimeError, match="모든 RSS 피드"):
        ingest.run(ctx)
    # 실패해도 여기까지의 수집 통계는 job_runs에 남도록 ctx.stats에 들어가 있어야 한다
    assert ctx.stats["rss"]["failed_feeds"] == 2


def test_generate_skips_before_clustering_when_kill_switch_file_exists(tmp_path, monkeypatch):
    from config.settings import Settings
    from jobs.tasks import generate

    kill_file = tmp_path / "LLM_KILL_SWITCH"
    kill_file.write_text("")
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(kill_file))
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)

    import pipeline.stages as stages

    class ExplodingStage:
        def __init__(self, *a, **k):
            raise AssertionError("킬 스위치가 켜져 있으면 클러스터링/생성 단계에 들어가면 안 된다")

    monkeypatch.setattr(stages, "Stage5_NewsletterGeneration", ExplodingStage)

    ctx = JobContext(job="generate", args=argparse.Namespace(limit=None, min_target=None, lookback_hours=None))
    with pytest.raises(JobSkipped) as exc:
        generate.run(ctx)
    assert exc.value.reason == "llm_kill_switch"
    assert str(kill_file) in exc.value.stats["kill_switch"]


def test_generate_skips_when_env_kill_switch_is_on(monkeypatch):
    from jobs.tasks import generate

    monkeypatch.setenv("LLM_KILL_SWITCH", "true")
    ctx = JobContext(job="generate", args=argparse.Namespace(limit=None, min_target=None, lookback_hours=None))
    with pytest.raises(JobSkipped):
        generate.run(ctx)


def test_fallback_list_prefers_user_categories_keeps_popularity_order_and_drops_seen():
    from jobs.tasks.batch_fallback import build_fallback_list

    ranked = [10, 11, 12, 13, 14]
    categories = {10: {1}, 11: {2}, 12: {2, 3}, 13: {1}, 14: {3}}

    result = build_fallback_list(ranked, categories, preferred_categories={2, 3}, seen_ids={12}, k=3)

    assert result == [11, 14, 10]


def test_fallback_list_without_preferences_is_plain_popularity_top_k():
    from jobs.tasks.batch_fallback import build_fallback_list

    assert build_fallback_list([5, 4, 3], {}, preferred_categories=set(), seen_ids=set(), k=2) == [5, 4]


def test_kst_day_start_is_midnight_kst_in_utc():
    from jobs.tasks.batch_fallback import kst_day_start_utc

    # 2026-01-15 01:30 UTC = 2026-01-15 10:30 KST -> KST 자정 = 2026-01-14 15:00 UTC
    assert kst_day_start_utc(datetime(2026, 1, 15, 1, 30, tzinfo=timezone.utc)) == datetime(
        2026, 1, 14, 15, 0, tzinfo=timezone.utc
    )


def test_daily_report_text_flags_a_silent_scheduler():
    from jobs.tasks.daily_report import format_report

    report = {
        "job_runs": {},
        "news_raw_24h": {"total": 0, "ok": 0, "dropped": 0, "empty": 0, "fetch_failed": 0,
                         "embedded": 0, "per_press": {}},
        "totals": {"news_raw": 10, "embedded": 8, "newsletters_24h": 0},
    }
    assert "실행된 잡 없음" in format_report(report)
