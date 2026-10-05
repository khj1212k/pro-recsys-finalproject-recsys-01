import threading
from collections import Counter
from typing import Dict


class RecsysCounters:
    """프로세스 내 카운터. GET /recsys/stats로 노출한다(워커별 값이며 합산은 하지 않음)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: Counter = Counter()

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counts[name] += n

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts[name]

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)
