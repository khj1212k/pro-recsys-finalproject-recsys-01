import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple


class ThrottledExceptionLog:
    """같은 자리의 실패가 이어질 때 traceback을 interval_s마다 한 번만 남긴다.

    DB 장애처럼 요청마다 같은 예외가 나는 동안 요청 수만큼 traceback이 쌓이면 로그가 원인을
    가린다. 그 사이에 건너뛴 건수는 다음 기록에 함께 적는다. 실패 건수 자체는 카운터
    (fallback.error 등)가 빠짐없이 센다."""

    def __init__(
        self,
        interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        logger: Optional[logging.Logger] = None,
    ):
        self._interval = interval_s
        self._clock = clock
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._state: Dict[str, Tuple[float, int]] = {}  # key -> (마지막 기록 시각, 건너뛴 건수)

    def exception(self, key: str, message: str, *args) -> None:
        """except 블록 안에서 부른다."""
        now = self._clock()
        with self._lock:
            last, skipped = self._state.get(key, (None, 0))
            if last is not None and now - last < self._interval:
                self._state[key] = (last, skipped + 1)
                return
            self._state[key] = (now, 0)
        if skipped:
            message += f" (+{skipped} similar failures since the last traceback)"
        self._logger.exception(message, *args)
