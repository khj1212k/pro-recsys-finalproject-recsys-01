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
    import crawler.policy_briefing as policy_module
    monkeypatch.setattr(rss_module, "collect_rss", fake_collect_rss)
    monkeypatch.setattr(policy_module, "collect_policy_briefing", lambda: {"skipped": "no service key"})

    parser = build_parser()
    ctx = JobContext(job="ingest", args=parser.parse_args(["ingest", "--stages", "rss"]))
    with pytest.raises(RuntimeError, match="모든 RSS 피드"):
        ingest.run(ctx)
    # 실패해도 여기까지의 수집 통계는 job_runs에 남도록 ctx.stats에 들어가 있어야 한다
    assert ctx.stats["rss"]["failed_feeds"] == 2
    assert ctx.warnings == []  # 실패 메시지 하나로 알린다 - 피드마다 경고를 덧붙이지 않는다


def _ingest_rss_only(monkeypatch, policy_briefing, rss=None):
    """rss 단계만 도는 ingest. RSS는 rss(기본: 정상), 정책브리핑 수집은 policy_briefing(함수)로 바꿔 끼운다."""
    from jobs.tasks import ingest
    import crawler.rss_collector as rss_module
    import crawler.policy_briefing as policy_module

    rss = rss or {"inserted": 3, "skipped": 1, "failed_feeds": 0, "per_feed": {"a": {"inserted": 3}}}
    monkeypatch.setattr(rss_module, "collect_rss", lambda hours: rss)
    monkeypatch.setattr(policy_module, "collect_policy_briefing", policy_briefing)
    monkeypatch.setattr(ingest, "news_raw_snapshot", lambda: {"total": 0})
    checkpoints = []
    ctx = JobContext(job="ingest", args=build_parser().parse_args(["ingest", "--stages", "rss"]))
    ctx._checkpoint = lambda: checkpoints.append(dict(ctx.stats))
    return ingest, ctx, checkpoints


def test_ingest_rss_stage_also_collects_policy_briefing(monkeypatch):
    """스케줄러는 `--stages rss,extract`로 수집한다. 정책브리핑(API 본문, 추출 불필요)도
    이 rss 단계에서 돌아야 compose 런타임에서 실제로 수집된다."""
    calls = []

    def fake_policy():
        calls.append(1)
        return {"fetched": 4, "kept": 2, "inserted": 2, "skipped": 0}

    ingest, ctx, checkpoints = _ingest_rss_only(monkeypatch, fake_policy)

    ingest.run(ctx)

    assert calls == [1]
    assert ctx.stats["rss"]["inserted"] == 3
    assert ctx.stats["policy_briefing"]["inserted"] == 2
    assert "duration_s" in ctx.stats["policy_briefing"]
    assert checkpoints and "policy_briefing" in checkpoints[-1]
    assert ctx.warnings == []


def test_ingest_policy_briefing_failure_keeps_rss_result_and_only_warns(monkeypatch):
    import crawler.policy_briefing as policy_module

    def boom():
        raise policy_module.PolicyBriefingAPIError("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR")

    ingest, ctx, _ = _ingest_rss_only(monkeypatch, boom)

    ingest.run(ctx)  # 이 출처의 실패로 RSS 수집 잡 전체를 실패시키지 않는다

    assert ctx.stats["rss"]["inserted"] == 3
    assert ctx.stats["policy_briefing"]["error"].startswith("PolicyBriefingAPIError")
    assert len(ctx.warnings) == 1 and "정책브리핑" in ctx.warnings[0]


def test_ingest_without_service_key_records_skip_without_warning(monkeypatch):
    ingest, ctx, _ = _ingest_rss_only(monkeypatch, lambda: {"skipped": "no service key"})

    ingest.run(ctx)

    assert ctx.stats["policy_briefing"]["skipped"] == "no service key"
    assert ctx.warnings == []


def test_ingest_warns_for_each_failed_rss_feed_but_succeeds(monkeypatch):
    """2026-10-17 Tier 0 run 946~952의 모양: 동아일보 RSS가 RemoteDisconnected로 실패해 신규 기사 0건이고, 추출 대상에
    동아일보가 없어 추출 경고도 없이 succeeded로 끝났다 - 수집량이 0인데 알림이 없었다. RSS 단계에서 못 읽은 피드도
    피드마다 경고한다(ADR 0026 결정 9). 피드 하나의 실패로 실행을 실패시키지는 않는다."""
    rss = {"inserted": 4, "skipped": 0, "failed_feeds": 2, "per_feed": {
        "동아일보": {"press": "동아일보", "inserted": 0,
                    "error": "RemoteDisconnected: Remote end closed connection without response"},
        "경향신문": {"press": "경향신문", "inserted": 4, "error": None},
        "한국경제": {"press": "한국경제", "inserted": 0,
                    "error": "feed unreadable (HTTP 403): <unknown>:1:0: undefined entity"},
    }}
    ingest, ctx, _ = _ingest_rss_only(monkeypatch, lambda: {"skipped": "no service key"}, rss=rss)

    ingest.run(ctx)

    assert ctx.warnings == [
        "동아일보: RSS 수집 실패 (RemoteDisconnected) - 이번 회차 신규 기사 0건",
        "한국경제: RSS 수집 실패 (feed unreadable (HTTP 403)) - 이번 회차 신규 기사 0건",
    ]


def _ingest_extract_only(monkeypatch, extract_stats, extra_argv=()):
    from jobs.tasks import ingest
    import crawler.content_extractor as extractor_pkg

    calls = []

    class FakeExtractor:
        def extract_parallel_with_stats(self, workers=None, time_budget_s=None, stats=None, checkpoint=None):
            calls.append({"workers": workers, "time_budget_s": time_budget_s})
            stats.update(extract_stats)  # 진짜 추출기처럼 넘겨받은 dict를 채워 돌려준다
            return stats

    monkeypatch.setattr(extractor_pkg, "ContentExtractor", FakeExtractor)
    monkeypatch.setattr(ingest, "news_raw_snapshot", lambda: {"total": 0})
    checkpoints = []
    ctx = JobContext(job="ingest", args=build_parser().parse_args(["ingest", "--stages", "extract", *extra_argv]))
    ctx._checkpoint = lambda: checkpoints.append(dict(ctx.stats))
    ctx.extract_calls = calls
    return ingest, ctx, checkpoints


def _extract_stats(per_press, **extra):
    totals = {k: 0 for k in ("ok", "dropped", "empty", "duplicate", "fetch_failed", "error", "deferred")}
    for counts in per_press.values():
        for k, v in counts.items():
            totals[k] += v
    return {"targets": sum(totals.values()), **totals, "per_press": per_press, **extra}


def test_ingest_fails_when_most_article_fetches_failed(monkeypatch):
    """네트워크가 끊기거나 차단되면 RSS는 되는데 본문은 전부 fetch_failed가 된다 - 그래도
    succeeded로 끝나면 아무도 모른다. 두 언론사 이상이 실패하면 한 사이트 문제로 보지 않는다."""
    stats = _extract_stats({"A": {"ok": 2, "fetch_failed": 5}, "B": {"fetch_failed": 2, "error": 1}})
    ingest, ctx, checkpoints = _ingest_extract_only(monkeypatch, stats)

    with pytest.raises(RuntimeError, match="10건 중 8건 실패.*언론사 2곳"):
        ingest.run(ctx)
    assert ctx.stats["extract"]["fetch_failed"] == 7
    assert checkpoints and checkpoints[-1]["extract"]["targets"] == 10
    assert [w.split(":")[0] for w in ctx.warnings] == ["A", "B"]  # 실패해도 언론사별 경고는 남긴다


def test_ingest_warns_but_succeeds_when_one_press_fetches_all_failed(monkeypatch):
    stats = _extract_stats({"A": {"fetch_failed": 3}, "B": {"ok": 9}, "C": {"fetch_failed": 1, "ok": 1}})
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)

    assert len(ctx.warnings) == 1 and ctx.warnings[0].startswith("A:")


def test_one_blocked_press_no_longer_fails_the_whole_run(monkeypatch):
    """2026-10-16 20:05 UTC Tier 0 회차(run 934)의 모양: 대상 52건 중 34건 실패(65%) - 모두 동아일보.
    예전 규칙은 실행 전체를 실패시켰다. 이제 경고(원인 포함)로 알리고 실행은 성공한다(ADR 0026 결정 9)."""
    stats = _extract_stats(
        {"동아일보": {"ok": 6, "fetch_failed": 34}, "매일경제": {"ok": 5, "dropped": 2},
         "경향신문": {"ok": 4, "dropped": 1}},
        fetch_fail_causes={"동아일보": {"disconnect": 26, "read_timeout": 8}},
    )
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)

    assert ctx.warnings == ["동아일보: 기사 40건 중 34건 본문 다운로드 실패 (원인: disconnect 26, read_timeout 8)"
                            " - 차단/사이트 개편 확인"]


def test_failures_spread_beyond_the_worst_press_still_fail_the_run(monkeypatch):
    """가장 많이 실패한 언론사를 빼도 절반 이상 실패면 한 사이트 문제가 아니다(예: VM 네트워크 문제)."""
    stats = _extract_stats({"A": {"fetch_failed": 10}, "B": {"ok": 2, "fetch_failed": 2},
                            "C": {"ok": 1, "error": 1}})
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    # B(2/4)는 받은 기사의 절반이 실패라 건강하지 않은 언론사다 - 두 곳이면 그것만으로 실패한다
    with pytest.raises(RuntimeError, match="언론사 2곳"):
        ingest.run(ctx)

    stats = _extract_stats({"A": {"fetch_failed": 10}, "B": {"ok": 1, "fetch_failed": 1},
                            "C": {"ok": 1, "error": 1}, "D": {"fetch_failed": 1, "ok": 1}})
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)
    # B·C·D는 각각 2건이라 언론사 경고 기준(3건)에 못 미친다 - A를 빼도 6건 중 3건(50%) 실패
    with pytest.raises(RuntimeError, match="A를 빼도 6건 중 3건 실패"):
        ingest.run(ctx)


def test_press_breaker_and_deferred_articles_are_reported_but_not_counted_as_failures(monkeypatch):
    """차단기로 미룬 기사는 받지 않았으므로 실패 분모에 넣지 않고, 경고에 미룬 건수를 적는다."""
    stats = _extract_stats(
        {"동아일보": {"fetch_failed": 3, "deferred": 44}, "세계일보": {"ok": 8}},
        fetch_fail_causes={"동아일보": {"disconnect": 3}},
        press_breaker={"동아일보": {"failures": 3, "deferred": 44}},
        deferred_reasons={"press_breaker": 44},
    )
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)

    assert ctx.warnings == ["동아일보: 기사 3건 중 3건 본문 다운로드 실패 (원인: disconnect 3), 연속 실패로 남은 44건은 "
                            "다음 실행으로 미룸 - 차단/사이트 개편 확인"]


def test_time_budget_exhaustion_warns(monkeypatch):
    stats = _extract_stats({"세계일보": {"ok": 30, "deferred": 12}}, time_budget_s=2700.0,
                           time_budget_exhausted=True, deferred_reasons={"time_budget": 12})
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats, ["--extract-deadline-s", "2700"])
    monkeypatch.setattr(ingest, "_clock", lambda: 0.0)

    ingest.run(ctx)

    assert ctx.extract_calls == [{"workers": None, "time_budget_s": 2700.0}]
    assert ctx.stats["extract"]["deadline_s"] == 2700.0
    assert ctx.warnings == ["본문 추출 시간 예산 2700초(ingest 시작 뒤 2700초 마감)를 다 써서 12건을 다음 실행으로 미룸"]


def _ingest_rss_then_extract(monkeypatch, rss_seconds, argv):
    """RSS 단계가 rss_seconds초 걸린 것처럼 시계를 돌리고, 추출 단계가 받은 시간 예산을 돌려준다."""
    from jobs.tasks import ingest
    import crawler.content_extractor as extractor_pkg
    import crawler.policy_briefing as policy_module
    import crawler.rss_collector as rss_module

    now = [1000.0]

    def slow_rss(hours):
        now[0] += rss_seconds
        return {"inserted": 1, "skipped": 0, "failed_feeds": 0, "per_feed": {"a": {"inserted": 1}}}

    budgets = []

    class FakeExtractor:
        def extract_parallel_with_stats(self, workers=None, time_budget_s=None, stats=None, checkpoint=None):
            budgets.append(time_budget_s)
            stats.update(_extract_stats({"a": {"ok": 1}}, time_budget_s=time_budget_s))
            return stats

    monkeypatch.setattr(ingest, "_clock", lambda: now[0])
    monkeypatch.setattr(rss_module, "collect_rss", slow_rss)
    monkeypatch.setattr(policy_module, "collect_policy_briefing", lambda: {"skipped": "no service key"})
    monkeypatch.setattr(extractor_pkg, "ContentExtractor", FakeExtractor)
    monkeypatch.setattr(ingest, "news_raw_snapshot", lambda: {"total": 0})
    ctx = JobContext(job="ingest", args=build_parser().parse_args(["ingest", "--stages", "rss,extract", *argv]))
    ingest.run(ctx)
    return budgets


def test_extract_deadline_is_measured_from_ingest_start_so_a_slow_rss_stage_shortens_extraction(monkeypatch):
    """다음 정시 회차와 겹치지 않게 하는 것은 잡이 끝나는 시각이다. 추출 예산을 추출 시작부터 재면 RSS 단계가
    늦을수록(관측 최대 321초, 상한 없음) 잡 끝이 그만큼 밀린다 - 마감은 ingest 시작부터 잰다(ADR 0026 결정 9)."""
    assert _ingest_rss_then_extract(monkeypatch, 500, ["--extract-deadline-s", "2700"]) == [2200.0]
    # RSS가 마감을 거의 다 써도 추출은 최소 60초 돈다(앞서 실패한 기사만 계속 미뤄지지 않게)
    assert _ingest_rss_then_extract(monkeypatch, 2690, ["--extract-deadline-s", "2700"]) == [60.0]
    # 마감을 주지 않으면(수동 실행) 제한 없음
    assert _ingest_rss_then_extract(monkeypatch, 500, []) == [None]


def _ingest_with_extractor_that(monkeypatch, behave, store=None):
    """추출기 대역이 behave(stats, checkpoint)로 넘겨받은 dict를 채우고(필요하면 중간 저장을 부르고) 끝낸다. run_job으로 돌려 job_runs에 남는 것을 본다."""
    from jobs.runtime import run_job
    from jobs.tasks import ingest
    import crawler.content_extractor as extractor_pkg
    from tests.test_jobs_runtime import FakeStore

    class FakeExtractor:
        def extract_parallel_with_stats(self, workers=None, time_budget_s=None, stats=None, checkpoint=None):
            behave(stats, checkpoint)
            return stats

    monkeypatch.setattr(extractor_pkg, "ContentExtractor", FakeExtractor)
    monkeypatch.setattr(ingest, "news_raw_snapshot", lambda: {"total": 0})
    store = store or FakeStore()
    notified = []
    args = build_parser().parse_args(["ingest", "--stages", "extract"])
    result = run_job("ingest", ingest.run, args=args, store_factory=lambda: store, notify=notified.append, git_sha="abc1234")
    return result, store


ROWS = [[1, "2026-10-26T09:00:00Z", "2026-10-25T09:50:00Z"], [2, "2026-10-26T09:30:00Z", "2026-10-25T09:51:00Z"]]


def test_ingest_hands_its_own_stats_dict_to_the_extractor_so_an_interrupted_run_keeps_the_original_pubdates(monkeypatch):
    """바꾼 행의 원래 발행 시각(created_at_fixed_rows)은 DB 어디에도 없다 - 추출 도중 배포 등으로 SIGTERM을 받아도 job_runs.stats에 남아야 한다."""
    from jobs.runtime import JobTerminated

    def behave(stats, checkpoint):
        stats.update(_extract_stats({"국민일보": {"ok": 2}}, created_at_fixed_rows=[list(r) for r in ROWS]))
        raise JobTerminated(15)

    result, store = _ingest_with_extractor_that(monkeypatch, behave)

    assert result.status == "failed" and "SIGTERM" in result.error
    assert store.rows[result.run_id]["stats"]["extract"]["created_at_fixed_rows"] == ROWS
    assert store.rows[result.run_id]["status"] == "failed"


def test_ingest_keeps_the_partial_extract_stats_when_the_extraction_raises(monkeypatch):
    def behave(stats, checkpoint):
        stats.update(_extract_stats({"국민일보": {"ok": 1}}, created_at_fixed_rows=[list(ROWS[0])]))
        raise RuntimeError("db gone")

    result, store = _ingest_with_extractor_that(monkeypatch, behave)

    assert result.status == "failed"
    assert store.rows[result.run_id]["stats"]["extract"]["created_at_fixed_rows"] == [ROWS[0]]


def test_the_extractors_checkpoint_writes_the_running_stats_to_job_runs(monkeypatch):
    """OOM/SIGKILL로 죽으면 끝의 저장이 없다 - 추출기가 부르는 중간 저장이 그때까지의 목록을 job_runs에 쓴다."""
    from tests.test_jobs_runtime import FakeStore

    store, mid_run = FakeStore(), []

    def behave(stats, checkpoint):
        stats.update(_extract_stats({"국민일보": {"ok": 2}}, created_at_fixed_rows=[list(r) for r in ROWS]))
        checkpoint()
        mid_run.append(dict(store.rows[1]))  # 추출이 아직 끝나지 않았을 때 job_runs에 있는 것

    result, _ = _ingest_with_extractor_that(monkeypatch, behave, store)

    assert result.status == "succeeded"
    assert mid_run[0]["status"] == "running" and mid_run[0]["stats"]["extract"]["created_at_fixed_rows"] == ROWS


def test_ingest_extract_stats_keep_their_keys_and_get_the_duration(monkeypatch):
    def behave(stats, checkpoint):
        stats.update(_extract_stats({"국민일보": {"ok": 2}}))

    result, store = _ingest_with_extractor_that(monkeypatch, behave)

    extract = store.rows[result.run_id]["stats"]["extract"]
    assert extract["targets"] == 2 and extract["ok"] == 2 and extract["duration_s"] >= 0 and extract["deadline_s"] is None


def test_extract_deadline_flag_must_be_positive():
    parser = build_parser()
    assert parser.parse_args(["ingest"]).extract_deadline_s is None
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest", "--extract-deadline-s", "0"])


def test_scheduled_ingest_lines_bound_extraction_before_the_next_hourly_slot():
    """매시 :05 ingest가 다음 회차를 건너뛰게 하지 않도록 crontab은 ingest 시작부터 잰 추출 마감을 준다(ADR 0026 결정 9).
    마감 + 이미 보낸 기사 하나의 요청 상한(2 x 30초 + 2초)이 다음 회차(:05)보다 충분히 앞서야 한다(:55 안).
    RSS 단계는 마감 안에 들어가므로 따로 더하지 않는다 - 대신 RSS 요청의 소켓 대기 1번에 상한이 있어야 한다."""
    from pathlib import Path

    from config.settings import Settings

    root = Path(__file__).resolve().parents[1]
    parser = build_parser()
    for name in ("crontab", "crontab.micro"):
        lines = [l for l in (root / "docker" / name).read_text(encoding="utf-8").splitlines()
                 if l and not l.startswith("#") and " ingest " in l]
        assert len(lines) == 1, name
        fields = lines[0].split()
        assert fields[:5] == ["5", "*", "*", "*", "*"]
        args = parser.parse_args(fields[8:])
        assert args.extract_deadline_s is not None, name
        in_flight = Settings.EXTRACT_FETCH_ATTEMPTS * Settings.EXTRACT_REQUEST_DEADLINE_S + Settings.EXTRACT_RETRY_BACKOFF_S
        assert args.extract_deadline_s + in_flight <= 50 * 60, name
    assert 0 < Settings.RSS_SOCKET_TIMEOUT_S <= 120


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
        "news_raw_24h": {"total": 0, "ok": 0, "dropped": 0, "empty": 0, "fetch_failed": 0, "error": 0, "duplicate": 0,
                         "embedded": 0, "per_press": {}},
        "totals": {"news_raw": 10, "embedded": 8, "newsletters_24h": 0},
    }
    assert "실행된 잡 없음" in format_report(report)


def test_daily_report_lists_recommendation_responses_by_source_largest_first():
    from jobs.tasks.daily_report import format_report

    report = {
        "job_runs": {"popularity": {"success": 24}},
        "news_raw_24h": {"total": 0, "ok": 0, "dropped": 0, "empty": 0, "fetch_failed": 0, "error": 0, "duplicate": 0,
                         "embedded": 0, "per_press": {}},
        "totals": {"news_raw": 10, "embedded": 8, "newsletters_24h": 0},
        "recsys_24h": {"by_source": {
            "popular": {"responses": 3, "items": 60},
            "realtime": {"responses": 90, "items": 1800},
            "cold_start_popular": {"responses": 7, "items": 140},
        }},
    }

    text = format_report(report)

    assert "추천 응답 100건 (노출 로그 기준: realtime 90, cold_start_popular 7, popular 3)" in text


def test_daily_report_says_so_when_no_recommendation_was_logged():
    from jobs.tasks.daily_report import format_report

    report = {
        "job_runs": {"popularity": {"success": 24}},
        "news_raw_24h": {"total": 0, "ok": 0, "dropped": 0, "empty": 0, "fetch_failed": 0, "error": 0, "duplicate": 0,
                         "embedded": 0, "per_press": {}},
        "totals": {"news_raw": 10, "embedded": 8, "newsletters_24h": 0},
        "recsys_24h": {"by_source": {}},
    }

    assert "추천 응답 0건 (노출 로그 기준)" in format_report(report)


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


def test_train_job_is_retired_and_runs_nothing(monkeypatch):
    """팀 레시피 학습 잡은 은퇴했다(ADR 0033): 불려도 자식 프로세스를 띄우지 않고 건너뛴 것으로 끝난다."""
    import subprocess

    from jobs.tasks import train

    def no_subprocess(*args, **kwargs):
        raise AssertionError("the retired train job must not start a process")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    monkeypatch.setattr(subprocess, "Popen", no_subprocess)
    args = build_parser().parse_args(["train", "--no-inference"])  # 예전 명령줄도 사용법 오류가 아니다

    with pytest.raises(JobSkipped) as exc:
        train.run(JobContext(job="train", args=args))

    assert exc.value.reason == "team_recipe_retired"
    assert exc.value.stats == {"replacement": "scripts/register_model.py (ADR 0033)"}


def test_user_embed_job_is_gone_and_the_profile_rebuild_job_is_registered():
    """장기 프로필은 클릭마다 갱신되는 증분 상태다(ADR 0033): 하루 한 번 벡터를 다시 계산하던 잡은 없다."""
    assert "user_embed" not in JOBS
    assert JOBS["rebuild_user_state"] == "jobs.tasks.rebuild_user_state"
    args = build_parser().parse_args(["rebuild_user_state", "--check", "--user-id", "3", "--user-id", "9"])
    assert args.check is True and args.user_id == [3, 9]
    assert build_parser().parse_args(["rebuild_user_state"]).user_id is None
    with pytest.raises(SystemExit) as exc:
        main(["user_embed"])
    assert exc.value.code == 2


def test_rebuild_job_reports_a_mismatch_found_by_check_as_a_warning(monkeypatch):
    import sys
    import types

    from jobs.tasks import rebuild_user_state

    seen = {}

    def fake_rebuild_all(engine, user_ids=None, check=False):
        seen.update(engine=engine, user_ids=user_ids, check=check)
        return {"users": 3, "unchanged": 1, "rebuilt": 0, "mismatch": 2, "mode": "check",
                "changed_user_ids_sample": [4, 7]}

    monkeypatch.setattr(rebuild_user_state, "rebuild_all", fake_rebuild_all)
    monkeypatch.setitem(sys.modules, "app.database", types.SimpleNamespace(engine="ENGINE"))
    ctx = JobContext(job="rebuild_user_state", args=build_parser().parse_args(["rebuild_user_state", "--check"]))

    result = rebuild_user_state.run(ctx)

    assert seen == {"engine": "ENGINE", "user_ids": None, "check": True}
    assert result["user_state"]["mismatch"] == 2
    assert len(ctx.warnings) == 1 and "2명" in ctx.warnings[0]


# ----------------------------------------------------------------------------- 추출 이상 감지 (ADR 0026 결정 9, 2026-10-25 절)
#
# 국민일보 2026-10-19: 페이지 템플릿이 바뀌어 trafilatura가 모든 기사에서 같은 특집 글을 뽑았고, 본문 해시 중복 제거가
# 나머지를 duplicate로 지웠다. 다운로드는 성공했으므로 fetch 실패 규칙에는 걸리지 않았고 ingest는 매시 succeeded로
# 끝났다. 같은 실행 안에서 한 언론사의 duplicate 비율이 높으면 경고한다.


def test_same_body_repeated_in_one_run_warns_and_marks_the_press_in_the_stats_but_does_not_fail(monkeypatch):
    """2026-10-24 22:05 UTC 회차의 모양: 국민일보 13건이 모두 같은 본문 해시의 duplicate였다."""
    stats = _extract_stats({"국민일보": {"duplicate": 12, "ok": 3}, "세계일보": {"ok": 9, "duplicate": 1}})
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)  # 실행을 실패시키지 않는다 - 다운로드·저장은 정상이고 행은 복구 SQL로 되돌릴 수 있다

    assert ctx.warnings == [
        "국민일보: 본문을 받은 기사 15건 중 12건이 duplicate - 같은 본문 반복 - 추출 이상 의심 "
        "(본문 컨테이너·페이지 개편 확인)"
    ]
    assert ctx.stats["extract"]["suspect_press"] == {
        "국민일보": {"reason": "same_body_repeated", "label": "같은 본문 반복 - 추출 이상 의심",
                   "parsed": 15, "duplicate": 12},
    }


@pytest.mark.parametrize("counts,flagged", [
    ({"duplicate": 4}, False),                      # 5건 미만
    ({"duplicate": 4, "ok": 0, "fetch_failed": 9}, False),  # 받은 기사가 4건뿐 - 다운로드 실패는 분모에 넣지 않는다
    ({"duplicate": 4, "ok": 6}, False),             # 40%
    ({"duplicate": 5, "ok": 5}, True),              # 정확히 절반 - '절반 이상'
    ({"duplicate": 3, "ok": 2}, True),              # 5건 중 3건
    ({"duplicate": 4, "ok": 1, "fetch_failed": 20, "deferred": 30}, True),  # 받은 5건 중 4건. 실패·미룸은 분모 밖
    ({"duplicate": 1, "ok": 20}, False),            # 평소 하루 0~14건의 자연 중복
    ({"duplicate": 2, "dropped": 2, "empty": 1}, False),  # 40%
])
def test_same_body_repeat_threshold_is_five_parsed_articles_and_half_of_them(monkeypatch, counts, flagged):
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, _extract_stats({"국민일보": counts}))

    ingest.run(ctx)

    assert bool(ctx.stats["extract"].get("suspect_press")) is flagged
    assert any("같은 본문 반복" in w for w in ctx.warnings) is flagged


def test_the_first_run_of_the_2026_10_19_breakage_was_below_the_threshold():
    """이 감지가 닿는 범위를 못 박아 둔다: 개편 직후 첫 회차(05:05 UTC)는 ok 84 + duplicate 15라 15%로 기준 밖이었다 -
    특집 글이 아직 기사마다 본문과 섞여(길이 2만 자) 해시가 서로 달랐다. 해시가 하나로 모인 뒤(같은 날 밤부터)에야 걸린다."""
    from jobs.tasks.ingest import _same_body_repeats

    assert _same_body_repeats({"국민일보": {"ok": 84, "duplicate": 15}}) == {}
    assert "국민일보" in _same_body_repeats({"국민일보": {"duplicate": 13}})


def test_same_body_repeat_in_several_presses_still_does_not_fail_the_run(monkeypatch):
    """다운로드 실패와 달리 이 신호는 '건강하지 않은 언론사' 수에 넣지 않는다 - 같은 실행에서 두 언론사가 동시에 걸려도
    잡을 실패시키지 않고 경고만 한다(추출은 끝났고 되돌릴 수 있다)."""
    stats = _extract_stats({"국민일보": {"duplicate": 8}, "세계일보": {"duplicate": 6, "ok": 1}, "경향신문": {"ok": 4}})
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)

    assert sorted(ctx.stats["extract"]["suspect_press"]) == ["국민일보", "세계일보"]
    assert len(ctx.warnings) == 2


def test_missing_body_container_warns_per_press_with_counts(monkeypatch):
    stats = _extract_stats(
        {"국민일보": {"ok": 8}, "세계일보": {"ok": 4}},
        body_container={"국민일보": {"container": 5, "container_missing": 3}},
    )
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)

    assert ctx.warnings == ["국민일보: 본문 컨테이너를 못 찾아 전체 페이지로 추출한 기사 3건 (컨테이너로 추출 5건) - 페이지 개편 확인"]


def test_no_container_warning_when_every_page_had_its_container(monkeypatch):
    stats = _extract_stats({"국민일보": {"ok": 8}}, body_container={"국민일보": {"container": 8}})
    ingest, ctx, _ = _ingest_extract_only(monkeypatch, stats)

    ingest.run(ctx)

    assert ctx.warnings == []


def test_feed_failures_are_grouped_per_press_when_a_press_has_several_feeds(monkeypatch):
    """국민일보 여섯·전자신문 셋처럼 피드가 여럿인 언론사는 언론사마다 한 줄로 경고한다(PR #54 리뷰)."""
    rss = {"inserted": 3, "skipped": 0, "failed_feeds": 4, "per_feed": {
        "국민일보": {"press": "국민일보", "inserted": 0, "error": "RemoteDisconnected: closed"},
        "국민일보_정치": {"press": "국민일보", "inserted": 0, "error": "skipped: www.kmib.co.kr unreadable earlier in this run"},
        "국민일보_경제": {"press": "국민일보", "inserted": 0, "error": "skipped: www.kmib.co.kr unreadable earlier in this run"},
        "전자신문_IT": {"press": "전자신문", "inserted": 0, "error": "feed unreadable (HTTP 503): x"},
        "전자신문_AI": {"press": "전자신문", "inserted": 2, "error": None},
        "경향신문": {"press": "경향신문", "inserted": 1, "error": None},
    }}
    ingest, ctx, _ = _ingest_rss_only(monkeypatch, lambda: {"skipped": "no service key"}, rss=rss)

    ingest.run(ctx)

    assert ctx.warnings == [
        "국민일보: RSS 피드 3개 모두 실패 (RemoteDisconnected, skipped) - 이번 회차 신규 기사 0건",
        "전자신문: RSS 피드 2개 중 1개 실패 (feed unreadable (HTTP 503)) - 전자신문_IT",
    ]


# ----------------------------------------------------------------------------- 동아일보 RSS 요약 (ADR 0026 2026-10-25 갱신 2)


def _summary_feed(inserted, stored, dropped=0, duplicate=0, error=None):
    return {"press": "동아일보", "entries": 50, "inserted": inserted, "skipped": 50 - inserted, "error": error,
            "summary": {"stored": stored, "dropped": dropped, "duplicate": duplicate}}


def test_summary_feed_that_stored_nothing_warns_but_does_not_fail_the_run(monkeypatch):
    """읽기는 성공했는데 새 행 5건 이상이 모두 필터·중복으로 닫히면 피드가 요약을 못 주고 있다 - 수집량이 0인데 알림이 없던
    국민일보 사례(추출은 성공)와 같은 모양의 침묵을 막는다. 잡은 실패시키지 않는다."""
    rss = {"inserted": 7, "skipped": 43, "failed_feeds": 0, "per_feed": {
        "동아일보": _summary_feed(7, stored=0, dropped=6, duplicate=1),
        "경향신문": {"press": "경향신문", "inserted": 3, "error": None},
    }}
    ingest, ctx, _ = _ingest_rss_only(monkeypatch, lambda: {"skipped": "no service key"}, rss=rss)

    ingest.run(ctx)

    assert ctx.warnings == ["동아일보: RSS 요약으로 저장된 기사가 없다 - 새 기사 7건이 모두 필터 6건·중복 1건으로 닫혔다 "
                            "(피드의 요약 형식 확인)"]


@pytest.mark.parametrize("feed, warned", [
    (_summary_feed(7, stored=1, dropped=6), False),          # 하나라도 저장되면 정상(일부가 비는 것은 daily_report가 본다)
    (_summary_feed(4, stored=0, dropped=4), False),          # 새 행 5건 미만은 표본이 작다
    (_summary_feed(5, stored=0, dropped=5), True),           # 정확히 5건
    (_summary_feed(0, stored=0), False),                     # 새 기사 없음(읽기 성공)
    (_summary_feed(8, stored=0, dropped=8, error="x"), False),  # 읽기 실패는 warn_failed_feeds의 몫
    ({"press": "경향신문", "inserted": 9, "error": None}, False),  # 요약 피드가 아니다(summary 통계 없음)
    (None, False),
])
def test_empty_summary_warning_threshold(feed, warned):
    from jobs.tasks import ingest

    ctx = JobContext(job="ingest", args=argparse.Namespace())
    ingest.warn_empty_summaries(ctx, {"per_feed": {"동아일보": feed}})

    assert bool(ctx.warnings) is warned
    assert ingest.SUMMARY_EMPTY_MIN_NEW == 5


def test_extract_health_never_sees_summary_rows_so_the_press_is_not_unhealthy_for_having_no_body():
    """요약 행은 본문 추출기를 거치지 않는다 - 동아일보는 추출 통계(per_press)에 나오지 않아 '본문 0'이 실패로 세어지지 않는다."""
    from jobs.tasks import ingest

    ctx = JobContext(job="ingest", args=argparse.Namespace())
    ingest.check_extract_health(ctx, {"per_press": {"경향신문": {"ok": 6}}, "fetch_fail_causes": {}})

    assert ctx.warnings == []


class _SnapshotConn:
    def __init__(self, rows):
        self.rows, self.sql = rows, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self.rows


def test_news_raw_snapshot_counts_summary_rows_apart_from_bodies(monkeypatch):
    """동아일보는 'ok' 본문이 아니라 RSS 요약을 본문 칸에 둔다 - with_content에 섞지 않고 with_summary로 따로 센다."""
    import db.connection
    from jobs.tasks import ingest

    # (언론사, 전체, 본문 있는 ok, 요약 있는 rss_summary, 본문 비움, 임베딩)
    conn = _SnapshotConn([("경향신문", 100, 90, 0, 10, 80), ("동아일보", 300, 20, 250, 30, 200)])
    monkeypatch.setattr(db.connection, "get_db_connection", lambda: conn)

    snapshot = ingest.news_raw_snapshot()

    assert snapshot["per_press"]["동아일보"] == {
        "total": 300, "with_content": 20, "with_summary": 250, "body_purged": 30, "embedded": 200}
    assert snapshot["per_press"]["경향신문"]["with_summary"] == 0
    assert (snapshot["total"], snapshot["with_content"], snapshot["with_summary"], snapshot["body_purged"],
            snapshot["embedded"]) == (400, 110, 250, 40, 280)
    sql = " ".join(conn.sql.split())
    # 열 순서가 위 튜플과 같다(SQL이 바뀌면 이 순서도 함께 본다)
    assert sql.index("= 'ok'") < sql.index("= 'rss_summary'") < sql.index("raw_news_content_purged_at IS NOT NULL") \
        < sql.index("embedding_result IS NOT NULL")
    assert "'rss_summary' AND N.raw_news_content_purged_at IS NULL" in sql  # 비운 요약은 세지 않는다
