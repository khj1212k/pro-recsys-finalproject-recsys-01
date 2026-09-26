#!/usr/bin/env python3
"""OCI Always Free 유휴 회수 기준을 우리 인스턴스 지표로 미리 계산한다 (ADR 0026).

공식 기준(docs.oracle.com, Always Free Resources): 7일 동안 다음이 **모두** 참이면 유휴로 보고
회수할 수 있다.
  - CPU 사용률 95번째 백분위 < 20%
  - 네트워크 사용률 < 20%
  - 메모리 사용률 < 20% (A1 셰이프만)

문서는 집계 해상도와 네트워크 사용률의 분모를 밝히지 않는다. 그래서 이 스크립트는 근사치다:
CPU·메모리는 oci_computeagent 1분 평균의 p95, 네트워크는 분당 max(수신, 송신) 속도를 셰이프
대역폭(E2.1.Micro 인터넷 50 Mbps)으로 나눈 값의 p95. 판정에 쓰지 말고 "회수 위험 조기 경보"로 쓴다.

    python3 scripts/oci/idle_check.py --instance-id <ocid> --compartment-id <ocid> [--memory]

종료 코드: 0 안전, 2 기준+여유(margin) 안쪽(위험), 3 기준상 유휴, 1 조회 실패.
OCI CLI(`oci`)가 설정돼 있어야 한다. 표준 라이브러리만 쓴다(호스트 파이썬으로 실행 가능).
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional

NAMESPACE = "oci_computeagent"
Series = Dict[str, float]  # timestamp -> value
Fetch = Callable[[str, str], Series]  # (metric, statistic) -> series


def percentile(values: Iterable[float], q: float) -> float:
    """선형 보간 백분위(numpy.percentile 기본값과 같은 정의)."""
    xs = sorted(float(v) for v in values)
    if not xs:
        raise ValueError("빈 값으로 백분위를 계산할 수 없습니다")
    pos = (len(xs) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def network_utilization_pct(bytes_per_s: float, bandwidth_mbps: float) -> float:
    return bytes_per_s * 8 / (bandwidth_mbps * 1_000_000) * 100


def idle_verdict(cpu_p95: float, net_p95: float, mem_p95: Optional[float],
                 threshold: float = 20.0, margin: float = 5.0) -> dict:
    """모든 적용 기준이 threshold 미만이면 idle. threshold+margin 미만이면 at_risk.

    mem_p95가 None이면(A1이 아닌 셰이프) 메모리 기준은 적용하지 않는다.
    """
    criteria = {"cpu": cpu_p95, "network": net_p95}
    if mem_p95 is not None:
        criteria["memory"] = mem_p95
    blocking = [name for name, value in criteria.items() if value >= threshold]
    return {
        "idle": not blocking,
        "at_risk": all(value < threshold + margin for value in criteria.values()),
        "blocking": blocking,
        "criteria": criteria,
        "threshold": threshold,
        "margin": margin,
    }


def exit_code(verdict: dict) -> int:
    if verdict["idle"]:
        return 3
    if verdict["at_risk"]:
        return 2
    return 0


def parse_datapoints(raw: str) -> Series:
    """`oci monitoring metric-data summarize-metrics-data` 출력(JSON)에서 timestamp->value."""
    if not raw.strip():
        return {}
    data = json.loads(raw).get("data") or []
    points: Series = {}
    for item in data:
        for dp in item.get("aggregated-datapoints") or []:
            points[dp["timestamp"]] = float(dp["value"])
    return points


def evaluate(fetch: Fetch, bandwidth_mbps: float, include_memory: bool,
             threshold: float = 20.0, margin: float = 5.0) -> dict:
    cpu = fetch("CpuUtilization", "mean")
    if not cpu:
        raise RuntimeError("CpuUtilization 지표가 없습니다 (에이전트 미동작 또는 인스턴스 ID 확인)")
    net_in = fetch("NetworksBytesIn", "rate")
    net_out = fetch("NetworksBytesOut", "rate")
    net = [
        network_utilization_pct(max(net_in.get(ts, 0.0), net_out.get(ts, 0.0)), bandwidth_mbps)
        for ts in sorted(set(net_in) | set(net_out))
    ]
    mem = fetch("MemoryUtilization", "mean") if include_memory else {}

    cpu_p95 = percentile(cpu.values(), 95)
    net_p95 = percentile(net, 95) if net else 0.0
    mem_p95 = percentile(mem.values(), 95) if include_memory and mem else None
    timestamps = sorted(cpu)
    return {
        "window": {"first": timestamps[0], "last": timestamps[-1]},
        "samples": {"cpu": len(cpu), "network": len(net), "memory": len(mem)},
        "cpu_p95": round(cpu_p95, 2),
        "net_p95": round(net_p95, 4),
        "mem_p95": None if mem_p95 is None else round(mem_p95, 2),
        "cpu_minutes_at_or_above_threshold_pct": round(
            100 * sum(v >= threshold for v in cpu.values()) / len(cpu), 2),
        "verdict": idle_verdict(cpu_p95, net_p95, mem_p95, threshold, margin),
    }


def oci_fetcher(instance_id: str, compartment_id: str, days: float) -> Fetch:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=days)

    def fetch(metric: str, statistic: str) -> Series:
        series: Series = {}
        chunk_start = start
        while chunk_start < end:  # 하루씩 나눠 조회(1분 해상도 1,440점/요청)
            chunk_end = min(chunk_start + timedelta(days=1), end)
            cmd = [
                "oci", "monitoring", "metric-data", "summarize-metrics-data",
                "--compartment-id", compartment_id,
                "--namespace", NAMESPACE,
                "--query-text", f'{metric}[1m]{{resourceId = "{instance_id}"}}.{statistic}()',
                "--start-time", chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "--end-time", chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ]
            out = subprocess.run(cmd, check=True, capture_output=True, text=True,
                                 env={**os.environ, "SUPPRESS_LABEL_WARNING": "True"})
            series.update(parse_datapoints(out.stdout))
            chunk_start = chunk_end
        return series

    return fetch


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--instance-id", required=True)
    p.add_argument("--compartment-id", required=True, help="인스턴스가 있는 컴파트먼트(루트면 테넌시 OCID)")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--threshold", type=float, default=20.0)
    p.add_argument("--margin", type=float, default=5.0)
    p.add_argument("--bandwidth-mbps", type=float, default=50.0,
                   help="네트워크 사용률 분모. E2.1.Micro 인터넷 대역폭 50 Mbps(공식 문서)")
    p.add_argument("--memory", action="store_true", help="A1 셰이프: 메모리 기준도 적용")
    args = p.parse_args(argv)

    try:
        report = evaluate(oci_fetcher(args.instance_id, args.compartment_id, args.days),
                          args.bandwidth_mbps, args.memory, args.threshold, args.margin)
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        print(json.dumps({"error": detail.strip()[:500]}, ensure_ascii=False))
        return 1
    report["requested_days"] = args.days
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code(report["verdict"])


if __name__ == "__main__":
    sys.exit(main())
