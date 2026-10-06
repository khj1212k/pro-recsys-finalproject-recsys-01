"""scripts/oci/idle_check.py - OCI Always Free 유휴 회수 기준을 우리 지표로 계산하는 로직 (ADR 0026)."""
import importlib.util
import json
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "oci" / "idle_check.py"
_spec = importlib.util.spec_from_file_location("oci_idle_check", _PATH)
idle_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(idle_check)


def test_percentile_uses_linear_interpolation_like_numpy_default():
    values = list(range(1, 101))
    assert idle_check.percentile(values, 95) == pytest.approx(95.05)
    assert idle_check.percentile([7.0], 95) == 7.0
    # 입력 순서와 무관
    assert idle_check.percentile([3, 1, 2], 50) == 2


def test_percentile_rejects_empty_input():
    with pytest.raises(ValueError):
        idle_check.percentile([], 95)


def test_short_bursts_do_not_lift_p95_above_threshold():
    # 매시 3분 미만(5% 미만)만 90%를 쓰고 나머지는 1% - p95는 여전히 낮다.
    hour = [90.0] * 2 + [1.0] * 58
    assert idle_check.percentile(hour * 24 * 7, 95) < 20
    # 매시 4분(5% 초과)을 쓰면 p95가 기준을 넘는다.
    hour = [90.0] * 4 + [1.0] * 56
    assert idle_check.percentile(hour * 24 * 7, 95) > 20


def test_network_utilization_is_rate_over_shape_bandwidth():
    # 625,000 B/s = 5 Mbps, 50 Mbps 셰이프 대역폭의 10%
    assert idle_check.network_utilization_pct(625_000, 50) == pytest.approx(10.0)
    assert idle_check.network_utilization_pct(0, 50) == 0.0


def test_micro_is_idle_when_cpu_and_network_are_both_low():
    v = idle_check.idle_verdict(cpu_p95=5.0, net_p95=0.1, mem_p95=None)
    assert v["idle"] is True
    assert v["at_risk"] is True
    assert v["blocking"] == []
    # E2.1.Micro에는 메모리 기준이 적용되지 않는다
    assert "memory" not in v["criteria"]


def test_cpu_above_threshold_blocks_idle():
    v = idle_check.idle_verdict(cpu_p95=30.0, net_p95=0.1, mem_p95=None)
    assert v["idle"] is False
    assert v["blocking"] == ["cpu"]
    assert v["at_risk"] is False


def test_value_just_above_threshold_is_not_idle_but_at_risk():
    v = idle_check.idle_verdict(cpu_p95=22.0, net_p95=0.1, mem_p95=None, threshold=20.0, margin=5.0)
    assert v["idle"] is False
    assert v["at_risk"] is True


def test_a1_memory_above_threshold_alone_blocks_idle():
    # 공식 기준은 "모두 참이면 유휴" - A1은 메모리만 20% 이상이어도 유휴가 아니다.
    v = idle_check.idle_verdict(cpu_p95=5.0, net_p95=0.1, mem_p95=35.0)
    assert v["idle"] is False
    assert v["blocking"] == ["memory"]


def test_a1_with_all_three_low_is_idle():
    v = idle_check.idle_verdict(cpu_p95=5.0, net_p95=0.1, mem_p95=10.0)
    assert v["idle"] is True


def test_parse_datapoints_reads_oci_cli_summarize_output():
    raw = json.dumps({"data": [{"aggregated-datapoints": [
        {"timestamp": "2026-09-26T02:15:00+00:00", "value": 99.5},
        {"timestamp": "2026-09-26T02:16:00+00:00", "value": 3.1},
    ]}]})
    assert idle_check.parse_datapoints(raw) == {
        "2026-09-26T02:15:00+00:00": 99.5,
        "2026-09-26T02:16:00+00:00": 3.1,
    }
    # 지표가 아직 없으면 CLI가 빈 data를 돌려준다
    assert idle_check.parse_datapoints(json.dumps({"data": []})) == {}
    assert idle_check.parse_datapoints("") == {}


def test_evaluate_combines_metrics_and_uses_max_direction_for_network():
    series = {
        "CpuUtilization": {"t1": 1.0, "t2": 90.0, "t3": 1.0},
        "NetworksBytesIn": {"t1": 0.0, "t2": 625_000.0, "t3": 0.0},
        "NetworksBytesOut": {"t1": 1_250_000.0, "t2": 0.0, "t3": 0.0},
    }
    calls = []

    def fake_fetch(metric, statistic):
        calls.append((metric, statistic))
        return series[metric]

    report = idle_check.evaluate(fake_fetch, bandwidth_mbps=50, include_memory=False)
    assert ("NetworksBytesIn", "rate") in calls and ("CpuUtilization", "mean") in calls
    assert not any(metric == "MemoryUtilization" for metric, _ in calls)
    assert report["samples"]["cpu"] == 3
    # 분당 max(in, out): t1 = 1.25 MB/s = 10 Mbps = 20%, t2 = 10%, t3 = 0%
    assert report["net_p95"] == pytest.approx(idle_check.percentile([20.0, 10.0, 0.0], 95))
    assert report["cpu_p95"] == pytest.approx(idle_check.percentile([1.0, 90.0, 1.0], 95))


def test_evaluate_refuses_to_judge_without_cpu_samples():
    def empty_fetch(metric, statistic):
        return {}

    with pytest.raises(RuntimeError):
        idle_check.evaluate(empty_fetch, bandwidth_mbps=50, include_memory=False)


@pytest.mark.parametrize(
    "verdict, expected",
    [
        ({"idle": False, "at_risk": False}, 0),
        ({"idle": False, "at_risk": True}, 2),
        ({"idle": True, "at_risk": True}, 3),
    ],
)
def test_exit_code_signals_risk_level(verdict, expected):
    assert idle_check.exit_code(verdict) == expected
