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


def _ingest_extract_only(monkeypatch, extract_stats):
    from jobs.tasks import ingest
    import crawler.content_extractor as extractor_pkg

    class FakeExtractor:
        def extract_parallel_with_stats(self, workers=None):
            return dict(extract_stats)

    monkeypatch.setattr(extractor_pkg, "ContentExtractor", FakeExtractor)
    monkeypatch.setattr(ingest, "news_raw_snapshot", lambda: {"total": 0})
    checkpoints = []
    ctx = JobContext(job="ingest", args=build_parser().parse_args(["ingest", "--stages", "extract"]))
    ctx._checkpoint = lambda: checkpoints.append(dict(ctx.stats))
    return ingest, ctx, checkpoints


def _extract_stats(per_press):
    totals = {k: 0 for k in ("ok", "dropped", "empty", "fetch_failed", "error")}
    for counts in per_press.values():
        for k, v in counts.items():
            totals[k] += v
    return {"targets": sum(totals.values()), **totals, "per_press": per_press}


def test_ingest_fails_when_most_article_fetches_failed(monkeypatch):
    """네트워크가 끊기거나 차단되면 RSS는 되는데 본문은 전부 fetch_failed가 된다 - 그래도
    succeeded로 끝나면 아무도 모른다."""
    stats = _extract_stats({"A": {"ok": 2, "fetch_failed": 5}, "B": {"fetch_failed": 2, "error": 1}})
    ingest, ctx, checkpoints = _ingest_extract_only(monkeypatch, stats)

    with pytest.raises(RuntimeError, match="8건 실패"):
        ingest.run(ctx)
    assert ctx.stats["extract"]["fetch_failed"] == 7
    assert checkpoints and checkpoints[-1]["extract"]["targets"] == 10


def test_ingest_warns_but_succeeds_when_one_press_fetches_all_failed(monkeypatch):
    stats = _extract_stats({"A": {"fetch_failed": 3}, "B": {"ok": 9}, "C": {"fetch_failed": 1, "ok": 1}})
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)

    assert len(ctx.warnings) == 1 and ctx.warnings[0].startswith("A:")


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


def test_embed_job_passes_budget_and_limit_and_fails_when_nothing_could_be_saved(monkeypatch):
    from jobs.tasks import embed

    seen = {}

    def fake_embed(settings, **kwargs):
        seen.update(kwargs)
        kwargs["stats"].update({"targets": 3, "embedded": 0, "failed_batches": 1})
        return kwargs["stats"]

    import pipeline.stages as stages
    monkeypatch.setattr(stages, "embed_pending_articles", fake_embed)

    parser = build_parser()
    ctx = JobContext(job="embed", args=parser.parse_args(
        ["embed", "--limit", "50", "--time-budget-s", "600", "--batch-size", "4"]))
    checkpoints = []
    ctx._checkpoint = lambda: checkpoints.append(1)
    with pytest.raises(RuntimeError, match="0건 저장"):
        embed.run(ctx)
    assert seen["limit"] == 50 and seen["time_budget_s"] == 600 and seen["batch_size"] == 4
    # 배치마다 job_runs에 진행 상황을 쓰도록 체크포인트가 연결돼 있어야 한다
    seen["on_batch"]()
    assert checkpoints == [1]
    assert ctx.stats["embed"]["failed_batches"] == 1
