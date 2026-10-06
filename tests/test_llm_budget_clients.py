# 지출 상한이 실제 클라이언트 경로 전부에 걸려 있는지 확인한다 - docs/adr/0035.
#
# 경로 셋: (1) 역할(generator/judge/tone)이 모두 쓰는 OpenAICompatLLMClient, (2) HyperCLOVA 어댑터가
# 감싸는 레거시 NaverHyperCLOVAClient, (3) 레거시 OpenAIClient. 전송 계층은 전부 페이크다 - 거부된
# 뒤에 호출되면 페이크가 바로 실패한다. 실제 API 호출은 없다.
import json
import os
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai
import pytest
import requests

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import (  # noqa: E402
    FakeOpenAIClient,
    make_response,
    make_status_error,
    make_timeout_error,
)

import core.llm.adapters as adapters_module  # noqa: E402
import core.llm.registry as registry  # noqa: E402
import core.llm_client as legacy_module  # noqa: E402
from config.settings import Settings  # noqa: E402
from core.llm import budget  # noqa: E402
from core.llm.adapters import HyperCLOVALLMClient, OpenAICompatLLMClient  # noqa: E402
from core.llm.budget import LLMBudgetExceeded, LLMUnpricedModel  # noqa: E402
from core.llm.schemas import ClusterEval  # noqa: E402
from core.llm_client import NaverHyperCLOVAClient, OpenAIClient  # noqa: E402
from core.llm_metrics import LLMMetricsCollector  # noqa: E402

MESSAGES = [{"role": "user", "content": "x" * 936}]
GEN = "gemini-3.5-flash-lite"    # $0.30 / $2.50
JUDGE = "gemini-3.1-flash-lite"  # $0.25 / $1.50
SCHEMA_BYTES = len(json.dumps(ClusterEval.model_json_schema(), ensure_ascii=False).encode("utf-8"))


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for name in ("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "LLM_BUDGET_FALLBACK_OUTPUT_PER_1M",
                 "LLM_BUDGET_BYTES_PER_TOKEN", "LLM_CIRCUIT_BREAKER_THRESHOLD", "LLM_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_SPEND_LEDGER_FILE", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(tmp_path / "LLM_KILL_SWITCH"))
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(legacy_module.time, "sleep", lambda *_a, **_k: None)
    budget.reset_run_state()
    LLMMetricsCollector().reset()
    yield
    budget.reset_run_state()


def _caps(monkeypatch, run="100", day="100", total="100"):
    monkeypatch.setenv("LLM_BUDGET_RUN_USD", run)
    monkeypatch.setenv("LLM_BUDGET_DAY_USD", day)
    monkeypatch.setenv("LLM_BUDGET_TOTAL_USD", total)


def _events(tmp_path):
    path = tmp_path / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _usage(prompt=400, completion=120, total=None, reasoning=None, cached=None):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=total,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def _parsed():
    return ClusterEval(decision="PASS", confidence=0.9)


def _client(responses, model=GEN, provider="gemini", **kwargs):
    fake = FakeOpenAIClient(responses=responses)
    return OpenAICompatLLMClient(provider=provider, model=model, client=fake, **kwargs), fake


def _worst_case_nusd(max_tokens, price_in=300, price_out=2_500, schema=True):
    est_input = 936 + 4 + 16 + 64 + (SCHEMA_BYTES if schema else 0)
    return est_input * price_in + max_tokens * price_out


# ---------------------------------------------------------------------------
# OpenAI 호환 어댑터 (모든 역할의 경로)
# ---------------------------------------------------------------------------

def test_refused_call_never_reaches_the_transport(monkeypatch, tmp_path):
    _caps(monkeypatch, run="0")
    client, fake = _client([])  # 호출되면 FakeChatCompletions가 AssertionError를 낸다

    with pytest.raises(LLMBudgetExceeded):
        client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=2048)

    assert fake.chat.completions.call_count == 0
    assert [e["ev"] for e in _events(tmp_path)] == ["refuse"]


def test_reservation_is_on_disk_before_the_request_goes_out(tmp_path):
    seen = []

    def on_call(_mode, _kwargs):
        seen.append([e["ev"] for e in _events(tmp_path)])

    fake = FakeOpenAIClient(responses=[make_response("{}", parsed=_parsed(), usage=_usage())], on_call=on_call)
    client = OpenAICompatLLMClient(provider="gemini", model=GEN, client=fake)

    client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=2048)

    assert seen == [["reserve"]]
    assert [e["ev"] for e in _events(tmp_path)] == ["reserve", "commit"]


def test_successful_call_reserves_the_worst_case_and_commits_the_reported_usage(tmp_path):
    client, _ = _client([make_response("{}", parsed=_parsed(),
                                       usage=_usage(prompt=400, completion=120, total=700, reasoning=180, cached=64))])

    result = client.complete(MESSAGES, schema=ClusterEval, purpose="newsletter_eval", max_tokens=2048)

    reserve, commit = _events(tmp_path)
    assert reserve["nusd"] == _worst_case_nusd(2048)
    assert (reserve["role"], reserve["purpose"], reserve["model"]) == ("judge", "newsletter_eval", GEN)
    # thinking 180토큰은 completion(120)에 없고 total(700)에만 있다 -> 출력 300토큰으로 과금
    assert commit["nusd"] == 400 * 300 + 300 * 2_500
    assert (commit["basis"], commit["outcome"]) == ("actual", "ok")
    assert (commit["thinking_tokens"], commit["cached_tokens"], commit["total_tokens"]) == (180, 64, 700)
    assert commit["billable_output_tokens"] == 300
    assert (result.usage.thinking_tokens, result.usage.cached_tokens, result.usage.total_tokens) == (180, 64, 700)


def test_every_role_goes_through_the_guard(monkeypatch, tmp_path):
    """레지스트리가 주는 실제 role 클라이언트(generator/judge/tone)가 전부 같은 원장에 예약한다."""
    for name in ("GEN_PROVIDER", "GEN_MODEL", "JUDGE_PROVIDER", "JUDGE_MODEL", "TONE_PROVIDER", "TONE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    registry.reset_registry()
    try:
        for role, purpose in (("generator", "newsletter_content_gen"), ("judge", "cluster_eval"),
                              ("tone", "tone_convert")):
            client = registry.get_client(role)
            client._client = FakeOpenAIClient(responses=[make_response("{}", parsed=_parsed(), usage=_usage())])
            client.complete(MESSAGES, schema=ClusterEval, purpose=purpose, max_tokens=64)
    finally:
        registry.reset_registry()

    commits = [e for e in _events(tmp_path) if e["ev"] == "commit"]
    assert [(e["role"], e["model"]) for e in commits] == [("generator", GEN), ("judge", JUDGE), ("tone", GEN)]
    assert commits[1]["nusd"] == 400 * 250 + 120 * 1_500  # judge 모델 단가로 계산


def test_every_role_is_refused_once_the_run_cap_is_spent(monkeypatch):
    for name in ("GEN_PROVIDER", "GEN_MODEL", "JUDGE_PROVIDER", "JUDGE_MODEL", "TONE_PROVIDER", "TONE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    _caps(monkeypatch, run="0")
    registry.reset_registry()
    try:
        for role in ("generator", "judge", "tone"):
            client = registry.get_client(role)
            client._client = FakeOpenAIClient(responses=[])
            with pytest.raises(LLMBudgetExceeded):
                client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=64)
            assert client._client.chat.completions.call_count == 0
    finally:
        registry.reset_registry()


def test_unpriced_model_is_refused_before_the_transport(tmp_path):
    client, fake = _client([], model="some-future-model")

    with pytest.raises(LLMUnpricedModel):
        client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert fake.chat.completions.call_count == 0


def test_response_that_fails_schema_validation_is_charged_its_reported_usage(tmp_path):
    """스키마를 못 지킨 응답도 과금된다. 재시도는 새 예약을 잡는다."""
    client, _ = _client([
        make_response("{}", parsed=None, usage=_usage(prompt=400, completion=50)),
        make_response("{}", parsed=_parsed(), usage=_usage(prompt=400, completion=120)),
    ])

    result = client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=2048)

    events = _events(tmp_path)
    assert [e["ev"] for e in events] == ["reserve", "commit", "reserve", "commit"]
    assert (events[1]["outcome"], events[1]["basis"], events[1]["nusd"]) == ("schema_invalid", "actual", 400 * 300 + 50 * 2_500)
    assert (events[3]["outcome"], events[3]["nusd"]) == ("ok", 400 * 300 + 120 * 2_500)
    assert result.schema_failures == 1


def test_json_mode_response_that_fails_validation_is_charged_its_reported_usage(tmp_path):
    client, _ = _client(
        [make_response('{"decision": "MAYBE"}', usage=_usage(prompt=400, completion=30)),
         make_response('{"decision": "PASS", "confidence": 0.9}', usage=_usage(prompt=400, completion=30))],
        supports_json_schema=False,
    )

    client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=2048)

    commits = [e for e in _events(tmp_path) if e["ev"] == "commit"]
    assert [(c["outcome"], c["basis"]) for c in commits] == [("schema_invalid", "actual"), ("ok", "actual")]


def test_length_finish_reason_is_charged_from_the_completion_it_carries(tmp_path):
    error = openai.LengthFinishReasonError(completion=SimpleNamespace(usage=_usage(prompt=400, completion=2048)))
    client, _ = _client([error])

    result = client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=2048)

    commit = _events(tmp_path)[-1]
    assert result.error == "length"
    assert (commit["outcome"], commit["basis"], commit["nusd"]) == ("length", "actual", 400 * 300 + 2048 * 2_500)


def test_timeout_is_charged_at_the_worst_case_and_the_retry_reserves_again(tmp_path):
    client, _ = _client([make_timeout_error(), make_response("{}", parsed=_parsed(), usage=_usage())])

    client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=2048)

    events = _events(tmp_path)
    assert [e["ev"] for e in events] == ["reserve", "commit", "reserve", "commit"]
    assert (events[1]["outcome"], events[1]["basis"], events[1]["nusd"]) == ("timeout", "reserved", _worst_case_nusd(2048))
    assert events[3]["basis"] == "actual"
    assert budget.spend_snapshot()["run_usd"] == pytest.approx((_worst_case_nusd(2048) + 400 * 300 + 120 * 2_500) / 1e9)


def test_gemini_http_errors_cost_nothing_but_other_providers_are_charged(tmp_path):
    gemini, _ = _client([make_status_error(503), make_response("{}", parsed=_parsed(), usage=_usage())])
    other, _ = _client([make_status_error(429), make_response("{}", parsed=_parsed(), usage=_usage())],
                       provider="openai", model="gpt-4o-mini")

    gemini.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=64)
    other.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=64)

    commits = [e for e in _events(tmp_path) if e["ev"] == "commit"]
    assert [(c["provider"], c["outcome"], c["basis"]) for c in commits] == [
        ("gemini", "http_503", "not_billed"), ("gemini", "ok", "actual"),
        ("openai", "http_429", "reserved"), ("openai", "ok", "actual"),
    ]
    assert commits[0]["nusd"] == 0 and commits[2]["nusd"] == commits[2]["reserved_nusd"] > 0


def test_cap_reached_between_retries_stops_before_the_next_request(monkeypatch, tmp_path):
    """타임아웃 한 번이 최악 비용으로 잡혀 상한을 채우면, 재시도는 나가지 않는다."""
    one_call = Decimal(_worst_case_nusd(2048)) / Decimal(10**9)
    _caps(monkeypatch, run=str(one_call * Decimal("1.5")))
    client, fake = _client([make_timeout_error(), make_response("{}", parsed=_parsed(), usage=_usage())])

    with pytest.raises(LLMBudgetExceeded) as exc:
        client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=2048)

    assert fake.chat.completions.call_count == 1
    assert exc.value.details["scope"] == "run"
    assert [e["ev"] for e in _events(tmp_path)] == ["reserve", "commit", "refuse"]


def test_day_cap_engages_the_kill_switch_so_a_fresh_process_gets_the_kill_switch_path(monkeypatch, tmp_path):
    _caps(monkeypatch, day="0")
    first, first_fake = _client([])
    with pytest.raises(LLMBudgetExceeded):
        first.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")
    assert (tmp_path / "LLM_KILL_SWITCH").exists()

    # 같은 프로세스의 다른 스레드: 킬 스위치 결과("kill_switch" -> 로컬 폴백)가 아니라 같은 중단을 받는다
    second, second_fake = _client([])
    with pytest.raises(LLMBudgetExceeded):
        second.complete(MESSAGES, schema=ClusterEval, purpose="tone_convert")

    # 다른 프로세스(런 상태 없음): 킬 스위치 파일만 보고 막힌다 - 네트워크 호출 없음
    budget.reset_run_state()
    third, third_fake = _client([])
    result = third.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert result.error == "kill_switch" and result.attempts == 0
    assert first_fake.chat.completions.call_count == second_fake.chat.completions.call_count == 0
    assert third_fake.chat.completions.call_count == 0


class _MeteredTransport:
    """스레드에서 동시에 불려도 되는 페이크: 호출마다 고정 usage를 돌려주고 호출 수를 센다."""

    def __init__(self, prompt_tokens, completion_tokens, delay_s=0.002):
        self._lock = threading.Lock()
        self._usage = (prompt_tokens, completion_tokens)
        self._delay_s = delay_s
        self.calls = 0
        self.chat = self
        self.completions = self

    def parse(self, **_kwargs):
        with self._lock:
            self.calls += 1
        time.sleep(self._delay_s)  # 예약이 겹치도록 응답을 조금 늦춘다
        return make_response("{}", parsed=_parsed(), usage=_usage(*self._usage))

    create = parse


def test_worker_threads_sharing_a_run_never_spend_past_the_cap(monkeypatch, tmp_path):
    """워커 4개가 같은 런 상한 아래에서 호출을 반복한다: 실제 지출도, 최악의 경우(예약 전부)도 상한 이하다."""
    monkeypatch.setattr(adapters_module.time, "sleep", time.sleep)  # 다른 스레드에 순서를 넘기게
    reserve_nusd = _worst_case_nusd(2048)
    actual_nusd = 600 * 300 + 400 * 2_500
    cap_nusd = reserve_nusd * 6
    _caps(monkeypatch, run=str(Decimal(cap_nusd) / Decimal(10**9)))
    transport = _MeteredTransport(prompt_tokens=600, completion_tokens=400)
    start = threading.Barrier(4)
    stops, others = [], []

    def worker():
        client = OpenAICompatLLMClient(provider="gemini", model=GEN, client=transport)
        start.wait()
        try:
            for _ in range(40):
                client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval", max_tokens=2048)
        except LLMBudgetExceeded as stop:
            stops.append(stop)
        except BaseException as other:  # noqa: BLE001
            others.append(other)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = _events(tmp_path)
    reserves = [e for e in events if e["ev"] == "reserve"]
    commits = [e for e in events if e["ev"] == "commit"]
    assert others == []
    assert len(stops) == 4, "상한이 160번의 시도를 다 받아줄 만큼 크면 이 테스트는 아무것도 보지 못한다"
    assert transport.calls == len(reserves) == len(commits)  # 예약 없이 나간 요청이 없다
    assert transport.calls * actual_nusd <= cap_nusd
    # 어느 시점에도 "정산액 + 열린 예약"이 상한을 넘지 않았다: 원장 줄을 순서대로 다시 계산한다
    committed, open_ = 0, {}
    for e in events:
        if e["ev"] == "reserve":
            open_[e["id"]] = e["nusd"]
        elif e["ev"] == "commit":
            committed += e["nusd"]
            del open_[e["id"]]
        assert committed + sum(open_.values()) <= cap_nusd
    assert transport.calls >= 6  # 예약이 실제 비용으로 풀리면서 최악 비용 6건어치보다 많이 처리된다


# ---------------------------------------------------------------------------
# 레거시 클라이언트
# ---------------------------------------------------------------------------

def _naver_ok(prompt=400, completion=120):
    response = MagicMock(status_code=200, headers={})
    response.json.return_value = {
        "status": {"code": "20000"},
        "result": {"message": {"content": "ok"}, "usage": {"promptTokens": prompt, "completionTokens": completion}},
    }
    return response


@pytest.fixture
def naver(monkeypatch):
    monkeypatch.setenv("NCP_CLOVASTUDIO_API_KEY", "nv-dummy")
    monkeypatch.setenv("NAVER_LLM_MIN_INTERVAL", "0")
    return NaverHyperCLOVAClient(model="HCX-005")


def test_legacy_hyperclova_has_no_price_so_it_is_refused_before_the_request(naver):
    with patch("core.llm_client.requests.post") as post:
        with pytest.raises(LLMUnpricedModel):
            naver.chat_completion(MESSAGES, purpose="cluster_eval")

    post.assert_not_called()


def test_legacy_hyperclova_reserves_and_commits_with_an_explicit_price(naver, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "5")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "20")

    with patch("core.llm_client.requests.post", return_value=_naver_ok()) as post:
        assert naver.chat_completion(MESSAGES, max_tokens=1000, purpose="newsletter_content_gen") == "ok"

    reserve, commit = _events(tmp_path)
    assert post.call_count == 1
    assert (reserve["provider"], reserve["model"], reserve["price_source"]) == ("naver", "HCX-005", "fallback_override")
    assert reserve["nusd"] == _worst_case_nusd(1000, price_in=5_000, price_out=20_000, schema=False)
    assert (commit["basis"], commit["nusd"]) == ("actual", 400 * 5_000 + 120 * 20_000)


def test_legacy_hyperclova_stops_before_the_request_when_the_cap_is_spent(naver, monkeypatch):
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "5")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "20")
    _caps(monkeypatch, total="0")

    with patch("core.llm_client.requests.post") as post:
        with pytest.raises(LLMBudgetExceeded) as exc:
            naver.chat_completion(MESSAGES, purpose="cluster_eval")

    post.assert_not_called()
    assert exc.value.details["scope"] == "total"


def test_legacy_hyperclova_failed_attempts_are_charged_at_the_worst_case(naver, monkeypatch, tmp_path):
    """이 프로바이더의 오류 응답 과금 여부는 확인한 적이 없다 - 429도 타임아웃도 예약액 그대로 잡는다."""
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "5")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "20")
    rate_limited = MagicMock(status_code=429, headers={})

    with patch("core.llm_client.requests.post",
               side_effect=[rate_limited, requests.exceptions.Timeout(), _naver_ok()]):
        assert naver.chat_completion(MESSAGES, max_tokens=1000, purpose="cluster_eval") == "ok"

    commits = [e for e in _events(tmp_path) if e["ev"] == "commit"]
    assert [(c["outcome"], c["basis"]) for c in commits] == [
        ("http_429", "reserved"), ("timeout", "reserved"), ("ok", "actual"),
    ]


def test_hyperclova_adapter_path_is_covered_by_the_same_guard(naver, monkeypatch):
    """role을 naver로 돌렸을 때의 경로: 어댑터 -> 레거시 클라이언트. 상한이 차면 요청이 나가지 않는다."""
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "5")
    monkeypatch.setenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "20")
    _caps(monkeypatch, run="0")
    adapter = HyperCLOVALLMClient(legacy_client=naver)

    with patch("core.llm_client.requests.post") as post:
        with pytest.raises(LLMBudgetExceeded):
            adapter.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")
        with pytest.raises(LLMBudgetExceeded):  # 멈춘 런: 어댑터 입구에서 바로 같은 중단
            adapter.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    post.assert_not_called()


@pytest.fixture
def legacy_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-dummy")
    monkeypatch.setenv("OPENAI_LLM_MIN_INTERVAL", "0")
    client = OpenAIClient(model="gpt-4o-mini")  # $0.15 / $0.60
    client.client = MagicMock()
    return client


def test_legacy_openai_stops_before_the_request_when_the_cap_is_spent(legacy_openai, monkeypatch):
    _caps(monkeypatch, run="0")

    with pytest.raises(LLMBudgetExceeded):
        legacy_openai.chat_completion(MESSAGES, purpose="cluster_eval")

    legacy_openai.client.chat.completions.create.assert_not_called()


def test_legacy_openai_commits_actual_usage_and_charges_failed_attempts(legacy_openai, tmp_path):
    ok = MagicMock()
    ok.choices = [MagicMock(message=MagicMock(content="ok"))]
    ok.usage = SimpleNamespace(prompt_tokens=400, completion_tokens=120, total_tokens=520)
    legacy_openai.client.chat.completions.create.side_effect = [RuntimeError("503 overloaded"), ok]

    assert legacy_openai.chat_completion(MESSAGES, max_tokens=1000, purpose="tone_convert") == "ok"

    events = _events(tmp_path)
    assert [e["ev"] for e in events] == ["reserve", "commit", "reserve", "commit"]
    assert (events[1]["outcome"], events[1]["basis"]) == ("unsettled", "reserved")
    assert (events[3]["basis"], events[3]["nusd"]) == ("actual", 400 * 150 + 120 * 600)
    assert events[0]["role"] == "tone"
