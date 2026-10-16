# 런 서킷브레이커(core/llm/budget.py) 테스트 - docs/adr/0035.
#
# 연속 인프라 실패(5xx·타임아웃·연결 오류)가 임계에 닿으면 재시도를 더 태우지 않고 런을 멈춘다.
# HTTP 402(선불 잔액 소진)는 같은 런 안에서 회복되지 않으므로 한 번에 멈춘다.
# 앞부분은 가드만, 뒷부분은 실제 어댑터의 재시도 루프에 페이크 전송 계층을 붙여 본다.
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from tests.llm_fakes import (  # noqa: E402
    FakeOpenAIClient,
    make_connection_error,
    make_response,
    make_status_error,
    make_timeout_error,
)

import core.llm.adapters as adapters_module  # noqa: E402
from config.settings import Settings  # noqa: E402
from core.llm import budget  # noqa: E402
from core.llm.adapters import OpenAICompatLLMClient  # noqa: E402
from core.llm.budget import LLMCircuitOpen, LLMRunStop  # noqa: E402
from core.llm.client import LLMUsage  # noqa: E402
from core.llm.schemas import ClusterEval  # noqa: E402
from core.llm_metrics import LLMMetricsCollector  # noqa: E402

MESSAGES = [{"role": "user", "content": "x"}]
MODEL = "gemini-3.5-flash-lite"


@pytest.fixture(autouse=True)
def _breaker_env(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_CIRCUIT_BREAKER_THRESHOLD", raising=False)  # 기본 5
    monkeypatch.delenv("LLM_KILL_SWITCH", raising=False)
    monkeypatch.setattr(Settings, "LLM_KILL_SWITCH_FILE", str(tmp_path / "LLM_KILL_SWITCH"))
    monkeypatch.setattr(adapters_module.time, "sleep", lambda *_a, **_k: None)
    budget.reset_run_state()
    LLMMetricsCollector().reset()
    yield
    budget.reset_run_state()


def _attempt():
    return budget.get_budget_guard().begin_attempt(
        provider="gemini", model=MODEL, purpose="cluster_eval", messages=MESSAGES, schema=None, max_tokens=16,
    )


def _fail(outcome):
    _attempt().settle(outcome)


def _parsed():
    return ClusterEval(decision="PASS", confidence=0.9)


def _client(responses):
    fake = FakeOpenAIClient(responses=responses)
    return OpenAICompatLLMClient(provider="gemini", model=MODEL, client=fake), fake


# ---------------------------------------------------------------------------
# 가드 수준
# ---------------------------------------------------------------------------

def test_fifth_consecutive_infra_failure_opens_the_circuit():
    for outcome in ("http_503", "timeout", "connection", "http_500"):
        _fail(outcome)

    with pytest.raises(LLMCircuitOpen) as exc:
        _fail("timeout")

    assert exc.value.code == "llm_circuit_open"
    assert exc.value.details["consecutive_failures"] == 5
    assert exc.value.details["last_outcome"] == "timeout"
    assert isinstance(exc.value, LLMRunStop)


def test_default_threshold_exceeds_the_worker_pool_so_one_blip_cannot_trip_it():
    """워커 4개가 같은 순간에 한 번씩 실패해도(일시 장애) 열리지 않는다 - 누군가는 백오프 뒤 다시 실패해야 한다."""
    from pipeline.stages import MAX_NEWSLETTER_WORKERS

    cfg = budget.BudgetConfig.from_env()

    assert cfg.breaker_threshold > MAX_NEWSLETTER_WORKERS


def test_a_response_from_the_provider_resets_the_count():
    for _ in range(4):
        _fail("http_503")
    _attempt().settle("ok", LLMUsage(input_tokens=1, output_tokens=1))

    for _ in range(4):
        _fail("http_503")  # 다시 4번까지는 열리지 않는다

    assert budget.run_stop() is None


@pytest.mark.parametrize("outcome", ["schema_invalid", "length", "content_filter"])
def test_model_side_failures_count_as_a_healthy_provider(outcome):
    for _ in range(4):
        _fail("timeout")
    _attempt().settle(outcome)
    for _ in range(4):
        _fail("timeout")

    assert budget.run_stop() is None


@pytest.mark.parametrize("outcome", ["http_429", "http_400", "http_401", "http_404", "error", "unsettled"])
def test_rate_limits_and_client_errors_neither_count_nor_reset(outcome):
    for _ in range(4):
        _fail("http_503")
    for _ in range(10):
        _fail(outcome)
    assert budget.run_stop() is None

    with pytest.raises(LLMCircuitOpen):
        _fail("http_503")


def test_payment_required_opens_the_circuit_on_the_first_occurrence():
    with pytest.raises(LLMCircuitOpen) as exc:
        _fail("http_402")

    assert exc.value.details["last_outcome"] == "http_402"
    assert exc.value.details["http_status"] == 402


def test_open_circuit_refuses_later_calls_before_any_reservation():
    with pytest.raises(LLMCircuitOpen):
        _fail("http_402")
    ledger = Path(os.environ["LLM_SPEND_LEDGER_FILE"])  # tests/conftest.py가 테스트마다 주는 임시 원장
    before = ledger.read_text(encoding="utf-8")

    with pytest.raises(LLMCircuitOpen):
        _attempt()

    assert ledger.read_text(encoding="utf-8") == before


def test_threshold_is_configurable_and_zero_disables_counting(monkeypatch):
    monkeypatch.setenv("LLM_CIRCUIT_BREAKER_THRESHOLD", "2")
    _fail("http_503")
    with pytest.raises(LLMCircuitOpen):
        _fail("http_503")

    budget.begin_run("next")
    monkeypatch.setenv("LLM_CIRCUIT_BREAKER_THRESHOLD", "0")
    for _ in range(20):
        _fail("http_503")
    assert budget.run_stop() is None
    with pytest.raises(LLMCircuitOpen):
        _fail("http_402")  # 잔액 소진은 임계와 무관하게 멈춘다


def test_begin_run_closes_the_circuit():
    for _ in range(4):
        _fail("timeout")
    budget.begin_run("next")
    for _ in range(4):
        _fail("timeout")

    assert budget.run_stop() is None


def test_opening_the_circuit_does_not_engage_the_kill_switch(tmp_path):
    """장애는 지출 문제가 아니다 - 다른 프로세스까지 막을 이유가 없고, 나중에 다시 돌리면 된다."""
    with pytest.raises(LLMCircuitOpen):
        _fail("http_402")

    assert not (tmp_path / "LLM_KILL_SWITCH").exists()


# ---------------------------------------------------------------------------
# 어댑터의 재시도 루프
# ---------------------------------------------------------------------------

def test_adapter_stops_retrying_at_the_threshold_instead_of_exhausting_max_retries():
    assert Settings.MAX_LLM_CALL_RETRIES > 5
    client, fake = _client([make_status_error(503) for _ in range(Settings.MAX_LLM_CALL_RETRIES)])

    with pytest.raises(LLMCircuitOpen):
        client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert fake.chat.completions.call_count == 5


def test_adapter_counts_failures_across_calls_and_models_in_the_same_run():
    """다른 워커의 호출이 이미 3번 실패한 상태라면, 이 호출은 2번 더 실패한 시점에 런이 멈춘다."""
    for outcome in ("timeout", "http_503", "connection"):
        _fail(outcome)  # 같은 런의 다른 호출(다른 모델이어도 같다)이 남긴 실패
    fake = FakeOpenAIClient(responses=[make_status_error(503) for _ in range(6)])
    other_model = OpenAICompatLLMClient(provider="gemini", model="gemini-3.1-flash-lite", client=fake)

    with pytest.raises(LLMCircuitOpen) as exc:
        other_model.complete(MESSAGES, schema=ClusterEval, purpose="newsletter_eval")

    assert fake.chat.completions.call_count == 2
    assert exc.value.details["consecutive_failures"] == 5


class _AlwaysOverloaded:
    """스레드에서 동시에 불려도 되는 페이크 전송 계층: 항상 503, 호출 수만 센다."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0
        self.chat = self
        self.completions = self

    def _fail(self, **_kwargs):
        with self._lock:
            self.calls += 1
        raise make_status_error(503)

    parse = create = _fail


def test_provider_outage_wastes_at_most_threshold_plus_in_flight_calls_across_worker_threads():
    """장애 주입: 워커 4개가 계속 503을 받으면, 임계(5) + 그 순간 이미 나가 있던 요청(최대 3)에서 전부 멈춘다.

    막지 않으면 4 x MAX_LLM_CALL_RETRIES(10) = 40번을 호출한다.
    """
    transport = _AlwaysOverloaded()
    clients = [OpenAICompatLLMClient(provider="gemini", model=MODEL, client=transport) for _ in range(4)]
    start = threading.Barrier(4)
    stops, others = [], []

    def worker(client):
        start.wait()
        try:
            client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")
        except LLMCircuitOpen as stop:
            stops.append(stop)
        except BaseException as other:  # noqa: BLE001 - 어떤 종류든 실패로 드러나야 한다
            others.append(other)

    threads = [threading.Thread(target=worker, args=(c,)) for c in clients]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert others == []
    assert len(stops) == 4
    assert 5 <= transport.calls <= 5 + 3


def test_adapter_makes_no_network_call_once_the_circuit_is_open():
    first, _ = _client([make_status_error(402, "prepay required")])
    with pytest.raises(LLMCircuitOpen):
        first.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")
    second, fake = _client([])  # 호출되면 FakeChatCompletions가 AssertionError를 낸다

    with pytest.raises(LLMCircuitOpen) as exc:
        second.complete(MESSAGES, schema=ClusterEval, purpose="newsletter_eval")

    assert fake.chat.completions.call_count == 0
    assert exc.value.details["http_status"] == 402


def test_adapter_records_the_stop_in_metrics():
    client, _ = _client([make_status_error(402, "prepay required")])
    collector = LLMMetricsCollector()

    with pytest.raises(LLMCircuitOpen):
        client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert [c.error_type for c in collector.calls if c.error_type] == ["llm_circuit_open"]


def test_three_transient_failures_then_success_still_succeeds():
    """기존 동작 보존: 일시 장애 3번 뒤 성공하는 호출은 런을 멈추지 않는다."""
    client, _ = _client([make_status_error(503), make_timeout_error(), make_connection_error(),
                         make_response("{}", parsed=_parsed())])

    result = client.complete(MESSAGES, schema=ClusterEval, purpose="cluster_eval")

    assert result.parsed is not None and result.attempts == 4
    assert budget.run_stop() is None
