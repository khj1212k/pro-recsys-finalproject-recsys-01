"""요청 시간 예산. 파이프라인 단계와 스코어러 묶음(ScorerStack)이 같이 본다."""
import time
from typing import Callable


class BudgetExceeded(Exception):
    def __init__(self, stage: str):
        super().__init__(f"time budget exhausted before {stage}")
        self.stage = stage


class Deadline:
    def __init__(self, budget_s: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.budget_s = budget_s
        self._end = clock() + budget_s

    def remaining(self) -> float:
        return max(0.0, self._end - self._clock())

    def remaining_fraction(self) -> float:
        """남은 예산의 비율(0~1). 예산이 0 이하면 0이다."""
        if self.budget_s <= 0:
            return 0.0
        return self.remaining() / self.budget_s

    def check(self, stage: str) -> None:
        if self._clock() >= self._end:
            raise BudgetExceeded(stage)
