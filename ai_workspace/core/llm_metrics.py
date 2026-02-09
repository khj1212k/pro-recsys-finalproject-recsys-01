"""
LLM Metrics Collector
- Tracks API calls, tokens, latency per batch
- Thread-safe singleton pattern for global access
"""
import threading
import time
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
        """Get summary statistics"""
        if not self.calls:
            return {
                "total_calls": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "avg_latency_seconds": 0,
                "by_purpose": {}
            }
        
        total_input = sum(c.input_tokens for c in self.calls)
        total_output = sum(c.output_tokens for c in self.calls)
        total_latency = sum(c.latency_seconds for c in self.calls)
        
        # By purpose breakdown
        by_purpose = defaultdict(lambda: {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "avg_latency": 0
        })
        
        for call in self.calls:
            p = by_purpose[call.purpose]
            p["calls"] += 1
            p["input_tokens"] += call.input_tokens
            p["output_tokens"] += call.output_tokens
            p["avg_latency"] = (p["avg_latency"] * (p["calls"] - 1) + call.latency_seconds) / p["calls"]
        
        return {
            "total_calls": len(self.calls),
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
        logger.info(f"  Total API Calls: {summary['total_calls']}")
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
