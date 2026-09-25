import threading
import time
from collections import OrderedDict
from typing import Callable, Dict, Generic, Hashable, Iterable, Optional, Tuple, TypeVar

V = TypeVar("V")

_MISSING = object()


class TTLCache(Generic[V]):
    """스레드 안전한 프로세스 내 TTL+LRU 캐시.

    추천 결과 캐시는 (user_id, last_click_id)를 키로 쓴다. 클릭이 들어오면
    last_click_id가 바뀌어 다음 요청이 자연히 새 키를 조회하므로, 여러 API 워커가
    있어도 별도 무효화 신호(Redis pub/sub 등) 없이 클릭 즉시 새로 계산된다.
    """

    def __init__(
        self,
        ttl_s: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = ttl_s
        self._max = max_entries
        self._clock = clock
        self._data: "OrderedDict[Hashable, Tuple[float, V]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Optional[V]:
        if self._ttl <= 0:
            return None
        now = self._clock()
        with self._lock:
            entry = self._data.get(key, _MISSING)
            if entry is _MISSING:
                return None
            expires_at, value = entry
            if expires_at <= now:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def get_many(self, keys: Iterable[Hashable]) -> Dict[Hashable, V]:
        found = {}
        for k in keys:
            v = self.get(k)
            if v is not None:
                found[k] = v
        return found

    def put(self, key: Hashable, value: V) -> None:
        if self._ttl <= 0 or self._max <= 0:
            return
        expires_at = self._clock() + self._ttl
        with self._lock:
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
