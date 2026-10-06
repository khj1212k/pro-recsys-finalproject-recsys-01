"""shadow 스코어러와 로그용 피처를 요청 경로 밖에서 계산하는 실행기 (ADR 0033, ADR 0025의 남은 일).

지키려는 것: **느리거나 실패하는 shadow가 요청을 시간 초과 폴백으로 만들지 못한다.** 그래서
- 요청을 처리하는 작업 스레드는 shadow를 기다리지 않는다. 작업을 이 실행기의 전용 스레드에 넘기고(submit)
  바로 돌아온다. 결과는 응답을 보낸 뒤 로그를 쓸 때 찾아간다(DeferredScores.wait).
- shadow 작업에는 자기 시간 예산이 있다(budget_s, 넘긴 시점부터). 로그를 쓰는 쪽은 그 예산이 남은 만큼과
  자기 상한(wait의 max_wait_s) 중 짧은 쪽만큼만 기다리고, 넘으면 그 작업을 버린다(shadow.timeout. 상한에 걸린
  것이면 shadow.log_wait_exceeded도 센다): 대기 중이던 작업은 시작되지 않고, 돌던 작업은 단계 사이에서 스스로
  멈춘다(expired()). 파이썬 스레드는 밖에서 끊을 수 없으므로 한 단계 안에 갇힌 작업은 전용 스레드 하나를
  붙잡은 채 남는다 - 요청 스레드가 아니다.
- 기다리는 쪽은 공짜가 아니다. 로그 쓰기는 응답을 보낸 뒤의 BackgroundTask이고, 동기 함수라 요청 핸들러와
  같은 스레드 풀(anyio, 기본 40개)의 자리를 쓴다. 기다리는 동안 그 자리 하나가 묶인다. 그래서 기다림의 상한을
  작업의 예산과 따로 둔다(RECSYS_SHADOW_LOG_WAIT_MS, 기본 100ms): 상한 x 밀린 작업 수만큼만 자리가 묶인다.
- 밀린 작업 수에 상한이 있다(max_pending). 넘으면 받지 않는다(shadow.rejected).
- 시간 초과가 breaker_after번 이어지면 cooldown_s 동안 받지 않는다(shadow.rejected). 갇힌 작업 뒤에 줄을 선
  작업마다 로그 쓰기가 예산만큼 기다리는 일이 되풀이되지 않게 하려는 것이다.
"""
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import numpy as np

from app.recsys.metrics import RecsysCounters
from app.recsys.throttle import ThrottledExceptionLog

logger = logging.getLogger(__name__)


@dataclass
class DeferredResult:
    """요청 경로 밖에서 계산한 것: 로그용 피처 행렬(후보 순서)과 shadow 모델 버전 -> 점수."""

    features: Optional[np.ndarray] = None
    feature_schema_version: Optional[int] = None
    extra_scores: Dict[str, np.ndarray] = field(default_factory=dict)


# 작업은 expired()를 인자로 받는다: True면 버려진 작업이니 단계 사이에서 멈춘다.
Job = Callable[[Callable[[], bool]], DeferredResult]


class DeferredScores:
    """넘긴 작업 하나의 손잡이. 결정론 목록과 함께 캐시에 들어가므로 여러 요청의 로그 쓰기가 같이 본다."""

    def __init__(self, runner: "ShadowRunner", deadline: float):
        self._runner = runner
        self._deadline = deadline
        self._abandoned = threading.Event()
        self._future: Optional[Future] = None

    def expired(self) -> bool:
        return self._abandoned.is_set() or self._runner.clock() >= self._deadline

    def done(self) -> bool:
        return self._future is not None and self._future.done()

    def wait(self, max_wait_s: Optional[float] = None) -> Optional[DeferredResult]:
        """결과를 돌려준다. 아직이면 이 작업의 남은 예산과 max_wait_s 중 짧은 쪽만큼만 기다리고, 그래도 없으면
        버리고 None이다. 한 번 버린 작업은 다시 기다리지 않는다.

        max_wait_s는 기다리는 쪽의 상한이다(None이면 남은 예산이 전부). 기다리는 쪽이 요청을 받는 스레드 풀의
        자리를 쓰는 경우(응답 뒤의 로그 쓰기)에 준다. 상한에 걸려 버린 작업도 시간 초과로 세어 차단 판단에
        들어간다 - 작업이 계속 상한보다 늦으면 받지 않게 되고, 로그 쓰기는 기다릴 것이 없어진다."""
        if self._future is None or self._abandoned.is_set():
            return None
        remaining = max(0.0, self._deadline - self._runner.clock())
        capped = max_wait_s is not None and max_wait_s < remaining
        try:
            return self._future.result(timeout=max(0.0, max_wait_s) if capped else remaining)
        except FutureTimeout:
            self._abandon(by_wait_cap=capped)
        except Exception:
            pass  # 작업이 예외로 끝났다: 실행기가 이미 세고 기록했다
        return None

    def _abandon(self, by_wait_cap: bool = False) -> None:
        if self._abandoned.is_set():
            return
        self._abandoned.set()
        self._future.cancel()  # 아직 시작하지 않았으면 시작되지 않는다
        self._runner.note_timeout(by_wait_cap=by_wait_cap)


class ShadowRunner:
    def __init__(
        self,
        workers: int = 1,
        max_pending: int = 16,
        budget_s: float = 0.5,
        breaker_after: int = 3,
        cooldown_s: float = 30.0,
        counters: Optional[RecsysCounters] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.budget_s = budget_s
        self.max_pending = max_pending
        self.breaker_after = breaker_after
        self.cooldown_s = cooldown_s
        self.counters = counters or RecsysCounters()
        self.clock = clock
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="recsys-shadow")
        self._lock = threading.Lock()
        self._pending = 0
        self._timeouts_in_a_row = 0
        self._closed_until = 0.0
        self._errors = ThrottledExceptionLog(clock=clock, logger=logger)

    def submit(self, job: Job) -> Optional[DeferredScores]:
        """작업을 전용 스레드에 넘긴다. 기다리지 않는다. 받지 않으면 None이다(밀림 상한, 차단 중, 종료됨)."""
        with self._lock:
            if self._pending >= self.max_pending or self.clock() < self._closed_until:
                self.counters.inc("shadow.rejected")
                return None
            self._pending += 1
        handle = DeferredScores(self, self.clock() + self.budget_s)
        try:
            future = self._executor.submit(self._run, job, handle)
        except RuntimeError:  # 종료된 실행기
            with self._lock:
                self._pending -= 1
            self.counters.inc("shadow.rejected")
            return None
        handle._future = future
        future.add_done_callback(self._finished)
        self.counters.inc("shadow.submitted")
        return handle

    def _run(self, job: Job, handle: DeferredScores) -> DeferredResult:
        if handle.expired():
            # 줄에서 기다리다 예산이 지났다: 시작하지 않는다. 밀리고 있다는 신호이므로 차단 판단에 같이 센다.
            self.counters.inc("shadow.expired")
            self._count_toward_breaker()
            return DeferredResult()
        try:
            result = job(handle.expired)
        except Exception:
            self._errors.exception("shadow.job", "deferred shadow/feature job failed; responses are unaffected")
            self.counters.inc("shadow.job_error")
            raise
        if not handle.expired():
            with self._lock:
                self._timeouts_in_a_row = 0
        return result

    def _finished(self, _future: Future) -> None:
        with self._lock:
            self._pending -= 1

    def note_timeout(self, by_wait_cap: bool = False) -> None:
        """로그를 쓰는 쪽이 결과를 받지 못해 작업을 버렸다. by_wait_cap: 작업의 예산은 남았는데 기다리는 쪽의
        상한이 먼저 찼다."""
        self.counters.inc("shadow.timeout")
        if by_wait_cap:
            self.counters.inc("shadow.log_wait_exceeded")
        self._count_toward_breaker()

    def _count_toward_breaker(self) -> None:
        with self._lock:
            self._timeouts_in_a_row += 1
            if self._timeouts_in_a_row >= self.breaker_after:
                self._closed_until = self.clock() + self.cooldown_s
                self._timeouts_in_a_row = 0
                self.counters.inc("shadow.breaker_open")

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
