# 지출 원장 CLI(python -m core.llm.spend_cli) 테스트 - docs/adr/0035.
#
# summary: 일·모델·역할별 사용량과 금액(USD, 가정 환율의 KRW), 상한 대비 사용액
# reset-day: 하루 창을 명시적으로 비운다(--yes 없이는 아무것도 바꾸지 않는다)
# estimate: 계획한 호출 수로 최악 비용을 미리 계산한다(원장을 쓰지 않는다)
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AI_WORKSPACE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace")
sys.path.insert(0, AI_WORKSPACE)

from config.settings import Settings  # noqa: E402
from core.llm import budget, spend_cli  # noqa: E402
from core.llm.client import LLMUsage  # noqa: E402
from core.llm.spend_ledger import Reservation, SpendLedger  # noqa: E402

GEN = "gemini-3.5-flash-lite"    # $0.30 / $2.50
JUDGE = "gemini-3.1-flash-lite"  # $0.25 / $1.50
MESSAGES = [{"role": "user", "content": "x" * 936}]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for name in ("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "LLM_BUDGET_KRW_PER_USD",
                 "LLM_BUDGET_DAY_TZ", "LLM_BUDGET_BYTES_PER_TOKEN", "LLM_RUN_ID",
                 "GEN_PROVIDER", "GEN_MODEL", "JUDGE_PROVIDER", "JUDGE_MODEL", "TONE_PROVIDER", "TONE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("LLM_BUDGET_RUN_USD", "0.20")
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "0.30")
    monkeypatch.setenv("LLM_BUDGET_TOTAL_USD", "3.00")
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(tmp_path / "LLM_KILL_SWITCH"))
    budget.reset_run_state()
    yield
    budget.reset_run_state()


def _call(model, purpose, usage=None, outcome="ok", provider="gemini", max_tokens=1000):
    attempt = budget.get_budget_guard().begin_attempt(
        provider=provider, model=model, purpose=purpose, messages=MESSAGES, schema=None, max_tokens=max_tokens)
    if outcome is not None:
        attempt.settle(outcome, usage)
    return attempt


def _seed():
    """generator 2건(실제 사용량), judge 1건(실제) + 1건(타임아웃 = 예약액), tone 1건, 열린 예약 1건."""
    budget.begin_run("stage5-1")
    _call(GEN, "newsletter_content_gen", LLMUsage(input_tokens=10_000, output_tokens=1_000, thinking_tokens=200,
                                                 cached_tokens=0, total_tokens=11_200))
    _call(GEN, "newsletter_meta_gen", LLMUsage(input_tokens=2_000, output_tokens=200))
    _call(JUDGE, "cluster_eval", LLMUsage(input_tokens=8_000, output_tokens=400))
    _call(JUDGE, "newsletter_eval", outcome="timeout")
    _call(GEN, "tone_convert", LLMUsage(input_tokens=1_500, output_tokens=900))
    _call(GEN, "newsletter_content_gen", outcome=None)


def _run(capsys, *argv):
    code = spend_cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------

def test_summary_groups_by_day_model_and_role_with_usd_and_krw(capsys):
    _seed()

    code, out, _ = _run(capsys, "summary", "--json")

    assert code == 0
    data = json.loads(out)
    rows = {(r["model"], r["role"]): r for r in data["rows"]}
    assert set(rows) == {(GEN, "generator"), (JUDGE, "judge"), (GEN, "tone")}
    assert len({r["day"] for r in data["rows"]}) == 1

    gen = rows[(GEN, "generator")]
    # 10,000 + 2,000 입력, 출력은 thinking 200 포함 1,200 + 200
    assert (gen["calls"], gen["input_tokens"], gen["billable_output_tokens"]) == (2, 12_000, 1_400)
    assert (gen["thinking_tokens"], gen["cached_tokens"]) == (200, 0)
    assert gen["usd"] == pytest.approx((12_000 * 300 + 1_400 * 2_500) / 1e9)
    assert gen["krw"] == pytest.approx(gen["usd"] * 1400)
    assert gen["basis"] == {"actual": 2}

    judge = rows[(JUDGE, "judge")]
    timeout_reservation = (936 + 4 + 16 + 64) * 250 + 1000 * 1_500
    assert judge["calls"] == 2 and judge["basis"] == {"actual": 1, "reserved": 1}
    assert judge["usd"] == pytest.approx((8_000 * 250 + 400 * 1_500 + timeout_reservation) / 1e9)

    assert data["totals"]["usd"] == pytest.approx(sum(r["usd"] for r in data["rows"]))
    assert data["open_reservations"]["count"] == 1
    assert data["open_reservations"]["usd"] == pytest.approx(((936 + 4 + 16 + 64) * 300 + 1000 * 2_500) / 1e9)


def test_summary_marks_the_exchange_rate_as_an_assumption(capsys, monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_KRW_PER_USD", "1450")
    _seed()

    code, out, _ = _run(capsys, "summary")

    assert code == 0
    assert "가정 환율" in out and "1,450" in out
    assert "실제 청구" in out  # 프로바이더 청구서가 기준이라는 문구
    data = json.loads(_run(capsys, "summary", "--json")[1])
    assert data["krw_per_usd"] == 1450 and data["krw_is_assumed_rate"] is True


def test_summary_text_lists_each_day_model_role_row_and_cap_usage(capsys):
    _seed()

    _, out, _ = _run(capsys, "summary")

    for token in (GEN, JUDGE, "generator", "judge", "tone", "$0.2000", "$0.3000", "$3.0000", "열린 예약 1건"):
        assert token in out
    assert str(Path(os.environ["LLM_SPEND_LEDGER_FILE"])) in out


def test_summary_reports_usage_against_caps_including_open_reservations(capsys):
    _seed()

    data = json.loads(_run(capsys, "summary", "--json")[1])

    settled = data["totals"]["usd"]
    open_usd = data["open_reservations"]["usd"]
    for scope in ("run", "day", "total"):
        assert data["caps"][scope]["used_usd"] == pytest.approx(settled + open_usd)
    assert data["caps"]["run"]["run_id"] == "stage5-1"  # 이 CLI 프로세스의 런이 아니라 원장에 마지막으로 기록된 런
    assert (data["caps"]["run"]["cap_usd"], data["caps"]["day"]["cap_usd"], data["caps"]["total"]["cap_usd"]) == (0.2, 0.3, 3.0)
    assert data["caps"]["day"]["remaining_usd"] == pytest.approx(0.3 - settled - open_usd)


def test_summary_counts_refusals_and_expired_reservations(capsys, tmp_path, monkeypatch):
    _seed()
    monkeypatch.setenv("LLM_BUDGET_RUN_USD", "0")
    with pytest.raises(budget.LLMBudgetExceeded):
        _call(GEN, "tone_convert")
    monkeypatch.setenv("LLM_BUDGET_RUN_USD", "0.20")
    monkeypatch.setenv("LLM_BUDGET_RESERVATION_TTL_S", "0.001")
    budget.begin_run("stage5-2")
    import time
    time.sleep(0.01)
    _call(GEN, "tone_convert", LLMUsage(input_tokens=10, output_tokens=10))  # 이 예약이 앞의 열린 예약을 만료시킨다

    data = json.loads(_run(capsys, "summary", "--json")[1])

    assert data["refusals"] == {"run": 1}
    assert data["expired_reservations"] == 1
    assert data["open_reservations"]["count"] == 0
    gen = {(r["model"], r["role"]): r for r in data["rows"]}[(GEN, "generator")]
    assert gen["basis"] == {"actual": 2, "reserved": 1}  # 만료된 예약이 예약액 그대로 잡혔다


def test_summary_of_a_missing_ledger_is_empty_not_an_error(capsys, tmp_path):
    code, out, _ = _run(capsys, "summary", "--ledger", str(tmp_path / "nope.jsonl"))

    assert code == 0
    assert "기록 없음" in out
    assert not (tmp_path / "nope.jsonl").exists()


def test_summary_can_be_limited_to_recent_days(capsys, tmp_path):
    ledger = SpendLedger(tmp_path / "ledger.jsonl")
    from core.llm.spend_ledger import CallKey, Caps

    caps = Caps(run_nusd=10**12, day_nusd=10**12, total_nusd=10**12)
    for day in ("2026-10-01", "2026-10-05", "2026-10-06"):
        res = ledger.reserve(caps=caps, key=CallKey("r", day, "gemini", GEN, "generator", "p"), nusd=1_000)
        assert isinstance(res, Reservation)
        ledger.settle(res, nusd=1_000, outcome="ok", basis="actual")

    data = json.loads(_run(capsys, "summary", "--json", "--days", "2")[1])

    assert [r["day"] for r in data["rows"]] == ["2026-10-05", "2026-10-06"]
    assert data["totals"]["usd"] == pytest.approx(2_000 / 1e9)        # 표에 보이는 기간의 합
    assert data["caps"]["total"]["used_usd"] == pytest.approx(3_000 / 1e9)  # 상한은 원장 전체 기준


# ---------------------------------------------------------------------------
# reset-day
# ---------------------------------------------------------------------------

def _today():
    return budget.BudgetConfig.from_env().day_of(__import__("time").time())


def test_reset_day_without_yes_changes_nothing(capsys, tmp_path):
    _seed()
    before = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8")

    code, out, _ = _run(capsys, "reset-day")

    assert code == 2
    assert "--yes" in out
    assert (tmp_path / "ledger.jsonl").read_text(encoding="utf-8") == before


def test_reset_day_clears_only_the_day_window(capsys, tmp_path, monkeypatch):
    _seed()
    before = json.loads(_run(capsys, "summary", "--json")[1])

    code, out, _ = _run(capsys, "reset-day", _today(), "--yes", "--note", "콘솔 청구액 $0.01 확인")

    assert code == 0 and "전체 상한" in out
    after = json.loads(_run(capsys, "summary", "--json")[1])
    open_usd = before["open_reservations"]["usd"]
    assert after["caps"]["day"]["used_usd"] == pytest.approx(open_usd)  # 진행 중 예약만 남는다
    assert after["caps"]["total"]["used_usd"] == pytest.approx(before["caps"]["total"]["used_usd"])
    assert after["totals"]["usd"] == pytest.approx(before["totals"]["usd"])  # 표의 지출 기록은 지워지지 않는다
    assert after["day_resets"] == [{"day": _today(), "cleared_usd": pytest.approx(before["totals"]["usd"]),
                                    "note": "콘솔 청구액 $0.01 확인"}]


def test_reset_day_rejects_a_malformed_date(capsys):
    code, _, err = _run(capsys, "reset-day", "10/06", "--yes")

    assert code == 2 and "YYYY-MM-DD" in err


def test_reset_day_can_clear_a_kill_switch_it_engaged_for_that_day(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "0")
    with pytest.raises(budget.LLMBudgetExceeded):
        _call(GEN, "tone_convert")
    kill_file = tmp_path / "LLM_KILL_SWITCH"
    assert kill_file.exists()

    code, out, _ = _run(capsys, "reset-day", _today(), "--yes", "--clear-kill-switch")

    assert code == 0 and not kill_file.exists()
    assert "킬 스위치" in out


@pytest.mark.parametrize("content", [
    "",                                                              # 외부 감시가 만든 빈 파일
    '{"engaged_by": "gcp-budget-guard"}\n',                          # 다른 주체
    '{"engaged_by": "llm_spend_cap", "scope": "total", "day": "%s"}\n',  # 전체 상한: 하루를 비워도 풀리지 않는다
    '{"engaged_by": "llm_spend_cap", "scope": "day", "day": "1999-01-01"}\n',  # 다른 날
])
def test_reset_day_never_clears_a_kill_switch_it_does_not_own(capsys, tmp_path, content):
    kill_file = tmp_path / "LLM_KILL_SWITCH"
    kill_file.write_text(content % _today() if "%s" in content else content, encoding="utf-8")

    code, out, _ = _run(capsys, "reset-day", _today(), "--yes", "--clear-kill-switch")

    assert code == 0
    assert kill_file.exists()
    assert "그대로" in out


# ---------------------------------------------------------------------------
# estimate
# ---------------------------------------------------------------------------

def test_estimate_multiplies_the_worst_case_per_call_by_the_planned_calls(capsys, tmp_path):
    code, out, _ = _run(capsys, "estimate", "--json",
                        "--plan", f"{GEN}:10:1000t:500", "--plan", f"{JUDGE}:4:2000t:100")

    assert code == 0
    data = json.loads(out)
    gen, judge = data["plans"]
    assert gen["worst_case_usd_per_call"] == pytest.approx((1000 * 300 + 500 * 2_500) / 1e9)
    assert gen["worst_case_usd"] == pytest.approx(10 * (1000 * 300 + 500 * 2_500) / 1e9)
    assert judge["worst_case_usd"] == pytest.approx(4 * (2000 * 250 + 100 * 1_500) / 1e9)
    assert data["worst_case_usd"] == pytest.approx(gen["worst_case_usd"] + judge["worst_case_usd"])
    assert data["worst_case_krw"] == pytest.approx(data["worst_case_usd"] * 1400)
    assert data["krw_is_assumed_rate"] is True
    assert not (tmp_path / "ledger.jsonl").exists()  # 추정은 원장을 쓰지 않는다


def test_estimate_from_characters_uses_the_same_byte_bound_as_the_guard(capsys):
    """문자 수로 주면 한글 기준 글자당 3바이트로 잡는다 - 가드가 실제로 예약할 금액과 같은 계산이다."""
    data = json.loads(_run(capsys, "estimate", "--json", "--plan", f"{GEN}:1:1000:1000")[1])

    plan = data["plans"][0]
    assert plan["est_input_tokens"] == 3000 + 2 * 16 + 64  # 시스템+사용자 메시지 2개 가정
    assert plan["worst_case_usd_per_call"] == pytest.approx((3096 * 300 + 1000 * 2_500) / 1e9)


def test_estimate_resolves_role_names_to_the_configured_models(capsys):
    data = json.loads(_run(capsys, "estimate", "--json", "--plan", "generator:1:100t:10",
                           "--plan", "judge:1:100t:10", "--plan", "tone:1:100t:10")[1])

    assert [p["model"] for p in data["plans"]] == [GEN, JUDGE, GEN]
    assert [p["role"] for p in data["plans"]] == ["generator", "judge", "tone"]


def test_estimate_counts_retries_when_asked(capsys):
    single = json.loads(_run(capsys, "estimate", "--json", "--plan", f"{GEN}:5:1000t:500")[1])
    retried = json.loads(_run(capsys, "estimate", "--json", "--plan", f"{GEN}:5:1000t:500",
                              "--attempts-per-call", "3")[1])

    assert retried["worst_case_usd"] == pytest.approx(3 * single["worst_case_usd"])


def test_estimate_says_whether_the_plan_fits_the_remaining_caps(capsys):
    fits = json.loads(_run(capsys, "estimate", "--json", "--plan", f"{GEN}:10:1000t:500")[1])     # $0.0155
    too_big = json.loads(_run(capsys, "estimate", "--json", "--plan", f"{GEN}:200:1000t:500")[1])  # $0.31

    assert fits["fits"] == {"run": True, "day": True, "total": True}
    assert too_big["fits"] == {"run": False, "day": False, "total": True}
    _, text, _ = _run(capsys, "estimate", "--plan", f"{GEN}:200:1000t:500")
    assert "넘습니다" in text and "run" in text and "day" in text


def test_estimate_accounts_for_what_is_already_spent_today(capsys):
    _seed()
    spent = json.loads(_run(capsys, "summary", "--json")[1])["caps"]["day"]["used_usd"]

    data = json.loads(_run(capsys, "estimate", "--json", "--plan", f"{GEN}:1:1000t:500")[1])

    assert data["remaining_usd"]["day"] == pytest.approx(0.30 - spent)
    assert data["remaining_usd"]["run"] == pytest.approx(0.20)  # 새 런은 런 상한을 처음부터 쓴다


def test_estimate_refuses_a_model_without_a_price(capsys):
    code, _, err = _run(capsys, "estimate", "--plan", "mystery-model:1:100t:10")

    assert code == 1 and "mystery-model" in err


@pytest.mark.parametrize("plan", ["gemini-3.5-flash-lite:10", "gemini-3.5-flash-lite:x:100:10", ":1:100:10",
                                  "gemini-3.5-flash-lite:1:100:0"])
def test_estimate_rejects_a_malformed_plan(capsys, plan):
    with pytest.raises(SystemExit) as exc:
        spend_cli.main(["estimate", "--plan", plan])

    assert exc.value.code == 2
    assert "MODEL:CALLS:PROMPT:MAX_OUTPUT_TOKENS" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def test_module_entry_point_runs(tmp_path):
    env = {**os.environ, "PYTHONPATH": AI_WORKSPACE, "LLM_SPEND_LEDGER_FILE": str(tmp_path / "ledger.jsonl")}
    proc = subprocess.run([sys.executable, "-m", "core.llm.spend_cli", "summary"], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path), timeout=120)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "기록 없음" in proc.stdout


def test_broken_config_is_reported_as_an_error_exit(capsys, monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "O.5")

    code, _, err = _run(capsys, "summary")

    assert code == 1 and "LLM_BUDGET_DAY_USD" in err
