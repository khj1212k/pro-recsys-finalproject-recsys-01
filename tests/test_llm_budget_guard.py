# LLM 지출 상한 가드(core/llm/budget.py) 단위 테스트 - docs/adr/0035.
#
# 가드는 요청마다 최악 비용(입력 토큰 상한 추정 + max_tokens × 출력 단가)을 원장에 예약하고,
# 상한을 넘으면 네트워크 요청 전에 예외를 낸다. 여기서는 어댑터 없이 가드만 본다:
# 설정의 기본값과 fail-closed, 비용 산술, 단가 없는 모델, 킬 스위치 연동, 런 중단 래치.
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from config.settings import Settings, shared_ops_root  # noqa: E402
from core.llm import budget  # noqa: E402
from core.llm.budget import (  # noqa: E402
    BudgetConfig,
    BudgetConfigError,
    LLMBudgetExceeded,
    LLMBudgetRefusal,
    LLMBudgetUnavailable,
    LLMRunStop,
    LLMUnpricedModel,
    billable_tokens,
    estimate_prompt_tokens,
    role_for_purpose,
    token_cost_nusd,
)
from core.llm.client import LLMUsage  # noqa: E402
from core.llm.schemas import ClusterEval  # noqa: E402
from core.llm.spend_ledger import SpendLedger  # noqa: E402
from core.llm_metrics import LLMMetricsCollector  # noqa: E402

BUDGET_ENV = (
    "LLM_BUDGET_RUN_USD", "LLM_BUDGET_DAY_USD", "LLM_BUDGET_TOTAL_USD", "LLM_BUDGET_DAY_TZ",
    "LLM_BUDGET_RESERVATION_TTL_S", "LLM_BUDGET_BYTES_PER_TOKEN", "LLM_BUDGET_KRW_PER_USD",
    "LLM_BUDGET_FALLBACK_INPUT_PER_1M", "LLM_BUDGET_FALLBACK_OUTPUT_PER_1M",
    "LLM_CIRCUIT_BREAKER_THRESHOLD", "LLM_RUN_ID",
)
MESSAGES = [{"role": "user", "content": "x" * 936}]  # 936 + role 4 + 메시지 16 + 기본 64 = 1,020 토큰 상한
MODEL = "gemini-3.5-flash-lite"  # 단가표: 입력 $0.30, 출력 $2.50 / 1M 토큰


@pytest.fixture(autouse=True)
def _guard_env(monkeypatch, tmp_path, llm_ledger_init):
    """상한 관련 환경변수를 모두 지우고(= 코드 기본값) 원장과 킬 스위치만 임시 경로로 돌린다."""
    for name in BUDGET_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", llm_ledger_init(str(tmp_path / "ledger.jsonl")))
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(tmp_path / "LLM_KILL_SWITCH"))
    budget.reset_run_state()
    LLMMetricsCollector().reset()
    yield
    budget.reset_run_state()


def _begin(model=MODEL, provider="gemini", purpose="newsletter_content_gen", messages=MESSAGES,
           schema=None, max_tokens=1000):
    return budget.get_budget_guard().begin_attempt(
        provider=provider, model=model, purpose=purpose, messages=messages, schema=schema, max_tokens=max_tokens,
    )


def _ledger_events(tmp_path):
    """init 헤더를 뺀 원장 줄."""
    path = tmp_path / "ledger.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in events if e["ev"] != "init"]


def _snapshot():
    return budget.spend_snapshot()


# ---------------------------------------------------------------------------
# 설정: 기본값과 fail-closed
# ---------------------------------------------------------------------------

def test_missing_config_falls_back_to_small_default_caps():
    cfg = BudgetConfig.from_env()

    assert (cfg.caps.run_nusd, cfg.caps.day_nusd, cfg.caps.total_nusd) == (200_000_000, 300_000_000, 3_000_000_000)
    assert cfg.fallback_price is None  # 단가 없는 모델을 허용하는 덮어쓰기는 기본으로 꺼져 있다
    assert cfg.breaker_threshold == 5
    assert cfg.day_tz == "Asia/Seoul"


def test_default_caps_stay_below_the_prepaid_balance_they_protect():
    """기본 전체 상한은 선불 잔액(₩8,000)보다 충분히 작아야 한다 - 가정 환율 ₩1,400/$ 기준 절반 남짓."""
    cfg = BudgetConfig.from_env()

    total_krw = Decimal(cfg.caps.total_nusd) / Decimal(10**9) * cfg.krw_per_usd
    assert total_krw == Decimal("4200")
    assert cfg.caps.run_nusd <= cfg.caps.day_nusd <= cfg.caps.total_nusd


def test_env_overrides_are_read_at_call_time(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_RUN_USD", "0.05")
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "1")
    monkeypatch.setenv("LLM_BUDGET_TOTAL_USD", "4.29")

    cfg = BudgetConfig.from_env()

    assert (cfg.caps.run_nusd, cfg.caps.day_nusd, cfg.caps.total_nusd) == (50_000_000, 1_000_000_000, 4_290_000_000)


def test_default_ledger_path_comes_from_settings(monkeypatch):
    monkeypatch.delenv("LLM_SPEND_LEDGER_FILE", raising=False)

    cfg = BudgetConfig.from_env()

    assert cfg.ledger_path == Settings.LLM_SPEND_LEDGER_FILE_DEFAULT
    assert cfg.ledger_path.endswith(os.path.join(".ops", "llm_spend_ledger.jsonl"))


@pytest.mark.parametrize("name,value", [
    ("LLM_BUDGET_DAY_USD", "O.5"),       # 숫자 0 대신 알파벳 O
    ("LLM_BUDGET_RUN_USD", "-1"),
    ("LLM_BUDGET_TOTAL_USD", "nan"),
    ("LLM_BUDGET_TOTAL_USD", "inf"),
    ("LLM_BUDGET_BYTES_PER_TOKEN", "0"),
    ("LLM_BUDGET_DAY_TZ", "Mars/Olympus"),
    ("LLM_CIRCUIT_BREAKER_THRESHOLD", "many"),
    ("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "1.0"),  # 출력 단가 없이 입력만
])
def test_malformed_config_is_an_error_not_a_silent_default(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(BudgetConfigError):
        BudgetConfig.from_env()


def test_malformed_config_refuses_the_call_before_anything_is_reserved(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "O.5")

    with pytest.raises(LLMBudgetUnavailable) as exc:
        _begin()

    assert exc.value.code == "llm_budget_unavailable"
    assert "LLM_BUDGET_DAY_USD" in str(exc.value)
    assert _ledger_events(tmp_path) == []


def test_unwritable_ledger_refuses_the_call(monkeypatch, tmp_path):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", str(blocker / "ledger.jsonl"))

    with pytest.raises(LLMBudgetUnavailable):
        _begin()


def test_missing_ledger_refuses_the_call_and_is_never_created_by_the_guard(tmp_path):
    """원장은 사람이 `spend_cli init`으로만 만든다. 가드가 만들면 지워진 원장이 0부터 다시 시작한다."""
    os.remove(tmp_path / "ledger.jsonl")

    with pytest.raises(LLMBudgetUnavailable) as exc:
        _begin()

    assert "spend_cli init" in str(exc.value)
    assert not (tmp_path / "ledger.jsonl").exists()
    assert "spend_cli init" in budget.preflight_problem()
    assert "spend_cli init" in _snapshot()["error"]  # 믿을 수 없는 합계 대신 사유
    assert budget.exhausted_scope() is None  # 판단하지 않는다 - 사전 점검과 첫 호출이 거부한다


def test_total_cap_does_not_restart_when_the_ledger_and_kill_switch_are_deleted(monkeypatch, tmp_path):
    """`.ops/`가 통째로 지워진 상황(git clean -fdx): 원장과 킬 스위치 파일이 함께 사라져도 상한이 0부터
    다시 시작하지 않는다. 사람이 원장을 다시 만들고 이전 누계를 이어받으면, 여전히 그 상한에 닿아 있다."""
    _cap_env(monkeypatch, total=str(ONE_CALL_USD))
    _begin().settle("timeout")  # 예약액 그대로 정산 - 전체 상한이 찼다
    with pytest.raises(LLMBudgetExceeded):
        _begin()
    os.remove(tmp_path / "ledger.jsonl")
    os.remove(tmp_path / "LLM_KILL_SWITCH")

    budget.begin_run("after-clean")
    with pytest.raises(LLMBudgetUnavailable, match="spend_cli init"):
        _begin()
    assert not (tmp_path / "ledger.jsonl").exists()

    SpendLedger(tmp_path / "ledger.jsonl").init()
    budget.begin_run("after-init")
    with pytest.raises(LLMBudgetUnavailable, match="adopt --yes"):
        _begin()

    SpendLedger(tmp_path / "ledger.jsonl").adopt()
    budget.begin_run("after-adopt")
    with pytest.raises(LLMBudgetExceeded) as exc:
        _begin()
    assert exc.value.details["scope"] == "total"
    assert exc.value.details["spent_usd"] == pytest.approx(float(ONE_CALL_USD))


@pytest.mark.parametrize("damage", ["truncate", "corrupt"])
def test_damaged_ledger_refuses_the_call(tmp_path, damage):
    _begin().settle("ok", LLMUsage(input_tokens=400, output_tokens=120))
    path = tmp_path / "ledger.jsonl"
    if damage == "truncate":
        path.write_bytes(b"")
    else:
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(lines[0] + b"".join(b"#" + line[1:] for line in lines[1:]))

    budget.reset_run_state()  # 다음 프로세스: 메모리에 남은 합계 없이 파일만 읽는다
    with pytest.raises(LLMBudgetUnavailable):
        _begin()

    assert budget.preflight_problem() is not None
    assert "error" in _snapshot()


def test_call_without_an_output_token_limit_is_refused():
    """max_tokens가 없으면 최악 비용을 계산할 수 없다."""
    with pytest.raises(LLMBudgetUnavailable):
        _begin(max_tokens=None)


def test_shared_ops_root_points_a_linked_worktree_at_the_main_checkout(tmp_path):
    """워크트리마다 원장이 따로 생기면 전체 상한이 워크트리 수만큼 늘어난다 - 기본 원장은 메인 체크아웃에 둔다."""
    main = tmp_path / "repo"
    worktree = tmp_path / "worktrees" / "exp"
    admin = main / ".git" / "worktrees" / "exp"
    admin.mkdir(parents=True)
    worktree.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    (worktree / ".git").write_text(f"gitdir: {admin}\n")

    assert shared_ops_root(worktree) == main.resolve()


def test_shared_ops_root_is_the_repo_itself_outside_a_worktree(tmp_path):
    plain = tmp_path / "repo"
    (plain / ".git").mkdir(parents=True)
    no_git = tmp_path / "image-root"
    no_git.mkdir()

    assert shared_ops_root(plain) == plain
    assert shared_ops_root(no_git) == no_git


# ---------------------------------------------------------------------------
# 비용 산술
# ---------------------------------------------------------------------------

def test_prompt_estimate_is_the_utf8_byte_count_plus_fixed_overhead():
    """서브워드 토큰은 최소 1바이트를 덮으므로 UTF-8 바이트 수가 토큰 수의 상한이다(한글 1자 = 3바이트)."""
    ascii_tokens = estimate_prompt_tokens([{"role": "user", "content": "a" * 100}])
    hangul_tokens = estimate_prompt_tokens([{"role": "user", "content": "가" * 100}])

    assert ascii_tokens == 100 + 4 + 16 + 64
    assert hangul_tokens == 300 + 4 + 16 + 64


def test_prompt_estimate_includes_the_response_schema():
    without = estimate_prompt_tokens(MESSAGES)
    with_schema = estimate_prompt_tokens(MESSAGES, ClusterEval)

    schema_bytes = len(json.dumps(ClusterEval.model_json_schema(), ensure_ascii=False).encode("utf-8"))
    assert with_schema == without + schema_bytes


def test_bytes_per_token_setting_relaxes_the_estimate():
    assert estimate_prompt_tokens([{"role": "user", "content": "가" * 100}], bytes_per_token=3.0) == 102 + 16 + 64


def test_token_cost_rounds_up_to_a_whole_nano_dollar():
    assert token_cost_nusd(1_000_000, Decimal("0.30")) == 300_000_000
    assert token_cost_nusd(1, Decimal("0.30")) == 300
    assert token_cost_nusd(1, Decimal("0.0000001")) == 1  # 0.0001 nUSD도 올림
    assert token_cost_nusd(0, Decimal("2.50")) == 0


def test_reservation_is_estimated_input_plus_max_output_at_list_price(tmp_path):
    attempt = _begin(max_tokens=1000)

    # 입력 1,020 × 300 nUSD + 출력 1,000 × 2,500 nUSD
    assert attempt.reservation.nusd == 1_020 * 300 + 1_000 * 2_500
    reserve = _ledger_events(tmp_path)[0]
    assert reserve["ev"] == "reserve"
    assert (reserve["est_input_tokens"], reserve["max_output_tokens"]) == (1_020, 1_000)
    assert (reserve["price_in_per_1m"], reserve["price_out_per_1m"], reserve["price_source"]) == ("0.3", "2.5", "table")
    assert (reserve["role"], reserve["purpose"]) == ("generator", "newsletter_content_gen")


def test_settling_with_reported_usage_charges_the_actual_tokens(tmp_path):
    attempt = _begin(max_tokens=1000)

    attempt.settle("ok", LLMUsage(input_tokens=400, output_tokens=120))

    commit = _ledger_events(tmp_path)[-1]
    assert commit["ev"] == "commit" and commit["basis"] == "actual"
    assert commit["nusd"] == 400 * 300 + 120 * 2_500
    assert commit["reserved_nusd"] == attempt.reservation.nusd
    assert (commit["input_tokens"], commit["output_tokens"]) == (400, 120)
    assert _snapshot()["run_usd"] == pytest.approx((400 * 300 + 120 * 2_500) / 1e9)


def test_thinking_tokens_missing_from_completion_are_billed_at_the_output_price():
    """total이 prompt+completion보다 크면 그 차이(보고되지 않은 thinking)를 출력 단가로 센다."""
    usage = LLMUsage(input_tokens=400, output_tokens=120, total_tokens=700, thinking_tokens=180)

    assert billable_tokens(usage) == (400, 300)


def test_thinking_tokens_already_inside_completion_are_not_double_counted():
    usage = LLMUsage(input_tokens=400, output_tokens=300, total_tokens=700, thinking_tokens=180)

    assert billable_tokens(usage) == (400, 300)


def test_thinking_tokens_are_added_when_no_total_is_reported():
    """total이 없으면 completion에 thinking이 들어 있는지 알 수 없다 - 더해서 센다(과대 계상 쪽)."""
    usage = LLMUsage(input_tokens=400, output_tokens=120, thinking_tokens=180)

    assert billable_tokens(usage) == (400, 300)


def test_cached_tokens_are_recorded_but_charged_at_the_full_input_price(tmp_path):
    attempt = _begin(max_tokens=1000)

    attempt.settle("ok", LLMUsage(input_tokens=400, output_tokens=120, cached_tokens=300, thinking_tokens=0, total_tokens=520))

    commit = _ledger_events(tmp_path)[-1]
    assert commit["nusd"] == 400 * 300 + 120 * 2_500
    assert commit["cached_tokens"] == 300 and commit["thinking_tokens"] == 0


def test_response_without_usage_is_charged_the_whole_reservation(tmp_path):
    attempt = _begin(max_tokens=1000)

    attempt.settle("ok", LLMUsage())

    commit = _ledger_events(tmp_path)[-1]
    assert commit["basis"] == "reserved" and commit["nusd"] == attempt.reservation.nusd


def test_timeout_is_charged_the_whole_reservation_because_billing_is_unknown(tmp_path):
    attempt = _begin(max_tokens=1000)

    attempt.settle("timeout")

    commit = _ledger_events(tmp_path)[-1]
    assert (commit["outcome"], commit["basis"], commit["nusd"]) == ("timeout", "reserved", attempt.reservation.nusd)


def test_gemini_http_error_is_not_billed(tmp_path):
    """Gemini 청구 문서: 400/500 오류로 실패한 요청은 토큰이 과금되지 않는다(접근 2026-10-06)."""
    attempt = _begin(provider="gemini", max_tokens=1000)

    attempt.settle("http_503")

    commit = _ledger_events(tmp_path)[-1]
    assert (commit["basis"], commit["nusd"]) == ("not_billed", 0)


def test_http_error_from_a_provider_whose_billing_was_not_verified_is_charged(tmp_path):
    attempt = _begin(provider="openai", model="gpt-4o-mini", max_tokens=1000)

    attempt.settle("http_503")

    commit = _ledger_events(tmp_path)[-1]
    assert commit["basis"] == "reserved" and commit["nusd"] == attempt.reservation.nusd


def test_attempt_left_unsettled_is_closed_at_the_reserved_amount(tmp_path):
    attempt = _begin(max_tokens=1000)

    attempt.ensure_settled()
    attempt.ensure_settled()

    commits = [e for e in _ledger_events(tmp_path) if e["ev"] == "commit"]
    assert len(commits) == 1
    assert (commits[0]["outcome"], commits[0]["basis"]) == ("unsettled", "reserved")


def test_settle_is_idempotent(tmp_path):
    attempt = _begin(max_tokens=1000)

    attempt.settle("ok", LLMUsage(input_tokens=10, output_tokens=10))
    attempt.settle("timeout")

    assert [e["ev"] for e in _ledger_events(tmp_path)] == ["reserve", "commit"]


# ---------------------------------------------------------------------------
# 단가 없는 모델
# ---------------------------------------------------------------------------

def test_unknown_model_price_refuses_the_call(tmp_path):
    with pytest.raises(LLMUnpricedModel) as exc:
        _begin(model="model-not-in-the-price-table")

    assert exc.value.code == "llm_unpriced_model"
    assert isinstance(exc.value, LLMBudgetRefusal)
    assert [e["ev"] for e in _ledger_events(tmp_path)] == []


def test_unknown_model_is_allowed_only_with_an_explicit_fallback_price(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "5")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "20")

    attempt = _begin(model="model-not-in-the-price-table", max_tokens=1000)

    assert attempt.reservation.nusd == 1_020 * 5_000 + 1_000 * 20_000
    assert _ledger_events(tmp_path)[0]["price_source"] == "fallback_override"


def test_fallback_price_does_not_replace_a_known_table_price(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "5")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "20")

    attempt = _begin(max_tokens=1000)

    assert attempt.reservation.nusd == 1_020 * 300 + 1_000 * 2_500


# ---------------------------------------------------------------------------
# 상한 초과와 킬 스위치
# ---------------------------------------------------------------------------

def _cap_env(monkeypatch, run="100", day="100", total="100"):
    monkeypatch.setenv("LLM_BUDGET_RUN_USD", run)
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", day)
    monkeypatch.setenv("LLM_BUDGET_TOTAL_USD", total)


ONE_CALL_USD = Decimal(1_020 * 300 + 1_000 * 2_500) / Decimal(10**9)  # $0.002806


def test_run_cap_refusal_raises_before_reserving_and_leaves_the_kill_switch_off(monkeypatch, tmp_path):
    _cap_env(monkeypatch, run=str(ONE_CALL_USD * 2))
    _begin()
    _begin()

    with pytest.raises(LLMBudgetExceeded) as exc:
        _begin()

    assert exc.value.code == "llm_budget_exceeded"
    assert exc.value.details["scope"] == "run"
    assert exc.value.details["cap_usd"] == pytest.approx(float(ONE_CALL_USD * 2))
    assert exc.value.details["requested_usd"] == pytest.approx(float(ONE_CALL_USD))
    assert not (tmp_path / "LLM_KILL_SWITCH").exists()
    assert [e["ev"] for e in _ledger_events(tmp_path)] == ["reserve", "reserve", "refuse"]


@pytest.mark.parametrize("scope", ["day", "total"])
def test_day_and_total_cap_refusals_engage_the_kill_switch_file(monkeypatch, tmp_path, scope):
    _cap_env(monkeypatch, **{scope: str(ONE_CALL_USD)})
    _begin()

    with pytest.raises(LLMBudgetExceeded) as exc:
        _begin()

    assert exc.value.details["scope"] == scope
    kill_file = tmp_path / "LLM_KILL_SWITCH"
    payload = json.loads(kill_file.read_text(encoding="utf-8"))
    assert payload["engaged_by"] == "llm_spend_cap"
    assert payload["scope"] == scope
    assert payload["ledger"] == str(tmp_path / "ledger.jsonl")
    assert exc.value.details["kill_switch_file"] == str(kill_file)

    from core.llm.kill_switch import kill_switch_reason
    assert str(kill_file) in kill_switch_reason()  # 같은 파일을 보는 다른 프로세스도 이제 막힌다


def test_existing_kill_switch_file_is_not_overwritten(monkeypatch, tmp_path):
    kill_file = tmp_path / "LLM_KILL_SWITCH"
    kill_file.write_text("external budget watcher\n")
    _cap_env(monkeypatch, total="0")

    with pytest.raises(LLMBudgetExceeded):
        _begin()

    assert kill_file.read_text() == "external budget watcher\n"


def test_kill_switch_that_cannot_be_written_still_refuses_the_call(monkeypatch, tmp_path):
    blocker = tmp_path / "ro"
    blocker.write_text("x")
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(blocker / "LLM_KILL_SWITCH"))
    _cap_env(monkeypatch, total="0")

    with pytest.raises(LLMBudgetExceeded) as exc:
        _begin()

    assert exc.value.details["kill_switch_file"] is None


def test_reservation_headroom_returns_after_the_actual_cost_is_settled(monkeypatch):
    """상한이 최악 비용 1.5건어치여도, 첫 호출이 실제 비용으로 정산되면 다음 호출이 들어간다."""
    _cap_env(monkeypatch, run=str(ONE_CALL_USD * Decimal("1.5")))
    first = _begin()
    first.settle("ok", LLMUsage(input_tokens=10, output_tokens=10))  # 실제 $0.000028

    second = _begin()

    assert second.reservation.nusd == first.reservation.nusd
    assert _snapshot()["run_usd"] == pytest.approx((10 * 300 + 10 * 2_500) / 1e9 + float(ONE_CALL_USD))


def test_open_reservations_of_concurrent_calls_count_against_the_cap(monkeypatch):
    """같은 상한(1.5건어치)에서 첫 호출이 아직 진행 중이면 두 번째는 거부된다."""
    _cap_env(monkeypatch, run=str(ONE_CALL_USD * Decimal("1.5")))
    _begin()

    with pytest.raises(LLMBudgetExceeded) as exc:
        _begin()

    assert exc.value.details["spent_usd"] == 0
    assert exc.value.details["reserved_usd"] == pytest.approx(float(ONE_CALL_USD))


def test_refusal_is_recorded_in_the_metrics_collector(monkeypatch):
    _cap_env(monkeypatch, run="0")
    collector = LLMMetricsCollector()

    with pytest.raises(LLMBudgetExceeded):
        _begin(purpose="cluster_eval")

    record = collector.calls[-1]
    assert (record.purpose, record.success, record.error_type) == ("cluster_eval", False, "llm_budget_exceeded")
    assert (record.provider, record.model) == ("gemini", MODEL)


# ---------------------------------------------------------------------------
# 런 중단 래치
# ---------------------------------------------------------------------------

def test_stop_exceptions_are_not_ordinary_exceptions():
    """`except Exception`으로 LLM 실패를 로컬 폴백으로 바꾸는 코드가 중단 신호를 삼키지 못해야 한다."""
    assert issubclass(LLMRunStop, BaseException)
    assert not issubclass(LLMRunStop, Exception)
    assert issubclass(LLMBudgetExceeded, LLMRunStop)


def test_after_a_refusal_every_later_call_in_the_run_stops_without_touching_the_ledger(monkeypatch, tmp_path):
    _cap_env(monkeypatch, run="0")
    with pytest.raises(LLMBudgetExceeded):
        _begin()
    before = _ledger_events(tmp_path)
    _cap_env(monkeypatch, run="100")  # 상한을 올려도 이미 멈춘 런은 다시 시작해야 풀린다

    with pytest.raises(LLMBudgetExceeded) as exc:
        _begin()

    assert exc.value.details["scope"] == "run"
    assert _ledger_events(tmp_path) == before
    assert budget.run_stop().code == "llm_budget_exceeded"


def test_begin_run_clears_the_stop_and_scopes_the_run_cap(monkeypatch):
    _cap_env(monkeypatch, run=str(ONE_CALL_USD))
    first_run = budget.begin_run("stage5-1")
    _begin()
    with pytest.raises(LLMBudgetExceeded):
        _begin()

    second_run = budget.begin_run("stage5-2")
    attempt = _begin()

    assert first_run != second_run
    assert attempt.reservation.key.run_id == second_run
    assert budget.run_stop() is None


def test_run_id_from_env_is_shared_across_begin_run_calls(monkeypatch):
    """LLM_RUN_ID를 주면 여러 프로세스(재개 포함)가 한 런 상한을 나눠 쓴다."""
    monkeypatch.setenv("LLM_RUN_ID", "bakeoff-v1")

    assert budget.begin_run("stage5-1") == "bakeoff-v1"
    assert budget.current_run_id() == "bakeoff-v1"


def test_raise_if_run_stopped_reraises_the_same_stop(monkeypatch):
    budget.raise_if_run_stopped()  # 멈추지 않았으면 아무 일도 없다
    _cap_env(monkeypatch, run="0")
    with pytest.raises(LLMBudgetExceeded):
        _begin()

    with pytest.raises(LLMBudgetExceeded):
        budget.raise_if_run_stopped()


# ---------------------------------------------------------------------------
# 역할 귀속과 스냅숏
# ---------------------------------------------------------------------------

def test_every_pipeline_purpose_maps_to_a_role():
    assert role_for_purpose("newsletter_content_gen") == "generator"
    assert role_for_purpose("newsletter_meta_gen") == "generator"
    assert role_for_purpose("cluster_eval") == "judge"
    assert role_for_purpose("newsletter_eval") == "judge"
    assert role_for_purpose("tone_convert") == "tone"
    assert role_for_purpose("something_new") == "other"


def test_purposes_used_by_the_pipeline_are_all_in_the_role_map():
    """호출부가 새 purpose를 쓰기 시작하면 원장의 역할별 집계에서 'other'로 빠진다 - 여기서 잡는다."""
    import re

    root = Path(__file__).resolve().parents[1] / "ai_workspace"
    used = set()
    for sub in ("core", "workflow", "pipeline"):
        for path in (root / sub).rglob("*.py"):
            if path.parent.name == "llm" or path.name == "llm_client.py":
                continue  # 클라이언트 계층 자신의 기본값·주석은 호출부가 아니다
            used |= set(re.findall(r'purpose="([a-z_0-9]+)"', path.read_text(encoding="utf-8")))

    assert used, "purpose 리터럴을 하나도 찾지 못했다 - 검색 패턴을 확인할 것"
    assert {p for p in used if role_for_purpose(p) == "other"} == set()


def test_spend_snapshot_reports_usage_against_each_cap(monkeypatch, tmp_path):
    _cap_env(monkeypatch, run="0.01", day="0.02", total="0.03")
    run_id = budget.begin_run("stage5-7")
    _begin().settle("ok", LLMUsage(input_tokens=1000, output_tokens=100))
    _begin()

    snap = _snapshot()

    assert snap["run_id"] == run_id
    assert snap["ledger"] == str(tmp_path / "ledger.jsonl")
    assert snap["caps_usd"] == {"run": 0.01, "day": 0.02, "total": 0.03}
    settled = (1000 * 300 + 100 * 2_500) / 1e9
    assert snap["run_usd"] == pytest.approx(settled + float(ONE_CALL_USD))
    assert snap["day_usd"] == snap["total_usd"] == snap["run_usd"]
    assert snap["open_reservations"] == 1


def test_spend_snapshot_reports_a_broken_config_instead_of_raising(monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", "O.5")

    snap = _snapshot()

    assert "LLM_BUDGET_DAY_USD" in snap["error"]
