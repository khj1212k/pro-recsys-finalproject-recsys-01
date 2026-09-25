import sys
import os
import json
from unittest.mock import MagicMock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ai_workspace"),
)

from core.llm_metrics import LLMMetricsCollector


def _fresh_collector():
    # 싱글턴이므로 매 테스트마다 강제로 리셋해서 격리
    collector = LLMMetricsCollector()
    collector.reset()
    return collector


def test_save_summary_writes_json_file(tmp_path):
    collector = _fresh_collector()
    collector.start_batch()
    collector.record_call("cluster_eval", input_tokens=100, output_tokens=50, latency_seconds=1.2)
    collector.record_call("newsletter_gen", input_tokens=300, output_tokens=200, latency_seconds=3.4)
    collector.end_batch()

    out_path = os.path.join(str(tmp_path), "reports", "llm_usage.json")
    saved_path = collector.save_summary(out_path)

    assert saved_path == out_path
    assert os.path.exists(out_path)

    with open(out_path, encoding="utf-8") as f:
        data = json.load(f)

    assert data["total_calls"] == 2
    assert data["total_input_tokens"] == 400
    assert data["total_output_tokens"] == 250
    assert "saved_at" in data


def test_estimate_cost_computes_weighted_token_price():
    collector = _fresh_collector()
    collector.start_batch()
    collector.record_call("cluster_eval", input_tokens=1000, output_tokens=500, latency_seconds=1.0)
    collector.end_batch()

    # 1000 input 토큰 * $0.001/1k + 500 output 토큰 * $0.002/1k = 0.001 + 0.001 = 0.002
    cost = collector.estimate_cost(price_per_1k_input=0.001, price_per_1k_output=0.002)

    assert cost == 0.002


def test_estimate_cost_zero_when_no_calls():
    collector = _fresh_collector()
    collector.start_batch()
    collector.end_batch()

    cost = collector.estimate_cost(price_per_1k_input=0.001, price_per_1k_output=0.002)

    assert cost == 0.0


def test_get_summary_counts_failed_calls_separately_from_successful():
    """FIX_LOG #20 확장: 실패/재시도한 호출도 record_call(success=False)로 기록되어
    total_calls에 포함되고, successful_calls/failed_calls로 구분 집계돼야 한다."""
    collector = _fresh_collector()
    collector.start_batch()
    collector.record_call("cluster_eval", input_tokens=0, output_tokens=0, latency_seconds=0.5, success=False)
    collector.record_call("cluster_eval", input_tokens=0, output_tokens=0, latency_seconds=0.5, success=False)
    collector.record_call("cluster_eval", input_tokens=100, output_tokens=50, latency_seconds=1.2, success=True)
    collector.end_batch()

    summary = collector.get_summary()

    assert summary["total_calls"] == 3
    assert summary["successful_calls"] == 1
    assert summary["failed_calls"] == 2
    # 실패 호출은 토큰이 0이므로 합계에는 성공 호출분만 반영됨
    assert summary["total_input_tokens"] == 100
    assert summary["by_purpose"]["cluster_eval"]["failed_calls"] == 2


def test_hyperclova_retry_records_failed_attempts_before_success(monkeypatch):
    """FIX_LOG #20 확장 (엔드투엔드): NaverHyperCLOVAClient가 429로 두 번 재시도한
    뒤 성공하면, 메트릭 컬렉터에 실패 2건 + 성공 1건, 총 3건이 기록돼야 한다."""
    import core.llm_client as llm_client_module
    from core.llm_client import NaverHyperCLOVAClient

    collector = _fresh_collector()
    collector.start_batch()
    monkeypatch.setattr(llm_client_module, "get_metrics_collector", lambda: collector)
    monkeypatch.setattr(llm_client_module.time, "sleep", lambda *_a, **_k: None)

    client = NaverHyperCLOVAClient(api_key="nv-test-key", model="HCX-003")

    rate_limited = MagicMock()
    rate_limited.status_code = 429
    rate_limited.headers = {}

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.raise_for_status = lambda: None
    success_response.json = lambda: {
        "status": {"code": "20000"},
        "result": {
            "message": {"content": "ok"},
            "usage": {"promptTokens": 10, "completionTokens": 5},
        },
    }

    responses = [rate_limited, rate_limited, success_response]
    monkeypatch.setattr(llm_client_module.requests, "post", lambda *a, **k: responses.pop(0))

    result = client.chat_completion([{"role": "user", "content": "hi"}])
    collector.end_batch()

    assert result == "ok"
    summary = collector.get_summary()
    assert summary["total_calls"] == 3
    assert summary["failed_calls"] == 2
    assert summary["successful_calls"] == 1
