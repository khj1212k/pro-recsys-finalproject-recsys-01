"""
LLM Metrics Collector
- Tracks API calls, tokens, latency per batch
- Thread-safe singleton pattern for global access
"""
import threading
import time
import os
import json
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


@dataclass
class LLMCallRecord:
    """Single LLM API call record"""
    purpose: str  # cluster_eval, newsletter_gen, newsletter_eval, tone_convert
    input_tokens: int
    output_tokens: int
    latency_seconds: float
    timestamp: float
    success: bool = True


class LLMMetricsCollector:
    """
    Global LLM metrics collector for a single batch run.
    Thread-safe singleton.
    """
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.reset()
    
    def reset(self):
        """Reset all metrics for new batch"""
        with self._lock:
            self.calls: List[LLMCallRecord] = []
            self.batch_start_time: Optional[float] = None
            self.batch_end_time: Optional[float] = None
            self._purpose_counts: Dict[str, int] = defaultdict(int)
    
    def start_batch(self):
        """Mark batch start time"""
        self.reset()
        self.batch_start_time = time.time()
        logger.info("📊 LLM Metrics collection started")
    
    def end_batch(self):
        """Mark batch end time"""
        self.batch_end_time = time.time()
        logger.info("📊 LLM Metrics collection ended")
    
    def record_call(
        self,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        latency_seconds: float,
        success: bool = True
    ):
        """Record a single LLM API call"""
        record = LLMCallRecord(
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_seconds=latency_seconds,
            timestamp=time.time(),
            success=success
        )
        with self._lock:
            self.calls.append(record)
            self._purpose_counts[purpose] += 1
    
    def get_summary(self) -> Dict:
        """Get summary statistics.

        self.calls는 성공한 호출뿐 아니라 재시도/실패한 호출도 포함한다(record_call이
        success=False로도 호출됨) - 그래야 실제로 API에 몇 번 요청했는지(재시도 비용
        포함)를 총계가 반영한다.
        """
        if not self.calls:
            return {
                "total_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "avg_latency_seconds": 0,
                "by_purpose": {}
            }

        total_input = sum(c.input_tokens for c in self.calls)
        total_output = sum(c.output_tokens for c in self.calls)
        total_latency = sum(c.latency_seconds for c in self.calls)
        failed_calls = [c for c in self.calls if not c.success]

        # By purpose breakdown
        by_purpose = defaultdict(lambda: {
            "calls": 0,
            "failed_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "avg_latency": 0
        })

        for call in self.calls:
            p = by_purpose[call.purpose]
            p["calls"] += 1
            if not call.success:
                p["failed_calls"] += 1
            p["input_tokens"] += call.input_tokens
            p["output_tokens"] += call.output_tokens
            p["avg_latency"] = (p["avg_latency"] * (p["calls"] - 1) + call.latency_seconds) / p["calls"]

        return {
            "total_calls": len(self.calls),
            "successful_calls": len(self.calls) - len(failed_calls),
            "failed_calls": len(failed_calls),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "avg_input_tokens": total_input / len(self.calls),
            "avg_output_tokens": total_output / len(self.calls),
            "avg_latency_seconds": total_latency / len(self.calls),
            "total_latency_seconds": total_latency,
            "by_purpose": dict(by_purpose),
            "batch_duration_seconds": (self.batch_end_time or time.time()) - (self.batch_start_time or time.time())
        }
    
    def print_summary(self):
        """Print formatted summary to logger"""
        summary = self.get_summary()
        
        logger.info("\n" + "=" * 60)
        logger.info("📊 LLM Usage Metrics Summary")
        logger.info("=" * 60)
        logger.info(f"  Total API Calls: {summary['total_calls']} "
                  f"(success: {summary.get('successful_calls', summary['total_calls'])}, "
                  f"failed/retried: {summary.get('failed_calls', 0)})")
        logger.info(f"  Total Input Tokens: {summary['total_input_tokens']:,}")
        logger.info(f"  Total Output Tokens: {summary['total_output_tokens']:,}")
        logger.info(f"  Avg Input Tokens/Call: {summary.get('avg_input_tokens', 0):.0f}")
        logger.info(f"  Avg Output Tokens/Call: {summary.get('avg_output_tokens', 0):.0f}")
        logger.info(f"  Avg Latency: {summary['avg_latency_seconds']:.2f}s/call")
        logger.info(f"  Total LLM Time: {summary.get('total_latency_seconds', 0):.1f}s")
        
        if summary["by_purpose"]:
            logger.info("\n  By Purpose:")
            for purpose, stats in summary["by_purpose"].items():
                logger.info(f"    {purpose}: {stats['calls']} calls, "
                          f"{stats['input_tokens']:,} in / {stats['output_tokens']:,} out, "
                          f"{stats['avg_latency']:.2f}s avg")
        
        logger.info("=" * 60 + "\n")

    def save_summary(self, path: str) -> str:
        """수집된 메트릭 요약을 JSON 파일로 저장한다.

        기존에는 print_summary()로 로그에만 남고 배치 종료 후 휘발됐다. 파일로
        영속화해 비용/지연시간 추이를 배치 간 비교하거나 리포트로 재사용할 수 있게 한다.
        """
        summary = self.get_summary()
        summary["saved_at"] = time.time()

        out_dir = os.path.dirname(path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        logger.info(f"💾 LLM 메트릭 저장 완료: {path}")
        return path

    def estimate_cost(self, price_per_1k_input: float, price_per_1k_output: float) -> float:
        """토큰 사용량 기준 대략적인 비용을 추정한다.

        price_per_1k_input/output은 호출부에서 실제 사용 모델의 단가(1,000 토큰당)를
        전달해야 한다 - 모델별 정확한 단가는 자주 바뀌므로 이 함수에 하드코딩하지 않는다.
        """
        summary = self.get_summary()
        input_cost = (summary["total_input_tokens"] / 1000.0) * price_per_1k_input
        output_cost = (summary["total_output_tokens"] / 1000.0) * price_per_1k_output
        return round(input_cost + output_cost, 6)


# Global instance
_metrics_collector: Optional[LLMMetricsCollector] = None


def get_metrics_collector() -> LLMMetricsCollector:
    """Get or create the global metrics collector"""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = LLMMetricsCollector()
    return _metrics_collector


def reset_metrics():
    """Reset the global metrics collector"""
    get_metrics_collector().reset()
