# LLM 지출 원장 (docs/adr/0035)
#
# 프로바이더에 요청을 보내기 전에 "이 요청이 쓸 수 있는 최악 비용"을 예약하고, 응답을 받은 뒤
# 실제 비용으로 정산한다. 두 단계를 추가 전용 JSONL 파일에 한 줄씩 남긴다.
#
#   reserve  요청 직전. 이 줄이 디스크에 내려간 뒤에만 네트워크 요청이 나간다.
#   commit   요청이 끝난 뒤. 실제 사용량으로 계산한 비용(또는 알 수 없으면 예약액)을 적는다.
#   expire   정산 없이 TTL을 넘긴 예약. 예약액을 그대로 지출로 확정한다.
#   refuse   상한 때문에 거부한 요청(지출 없음, 감사용).
#   reset_day  사람이 하루 창을 비운 기록.
#
# 상한 검사는 항상 "정산된 지출 + 아직 열려 있는 예약 + 이번 요청 > 상한"이다. 예약과 정산
# 사이에 프로세스가 죽으면 정산 줄이 없으므로 그 예약은 열린 채 최악 비용으로 계속 잡힌다
# (그 요청이 과금됐는지 알 수 없기 때문이다). 한 번 잡힌 금액은 reset_day 말고는 줄지 않는다.
#
# 금액은 nUSD(10억분의 1달러) 정수다. 토큰당 단가가 1e-7달러 단위라 float 합으로 상한
# 경계를 비교하면 실행마다 결과가 달라질 수 있다.
#
# 잠금: 같은 디렉터리의 "<원장>.lock"에 flock을 건다. flock은 열린 파일 기술자 단위라 한
# 프로세스의 스레드끼리도, 여러 프로세스끼리도 서로 기다린다. 프로세스가 죽으면 커널이 푼다.
# 상태는 파일에서만 만든다: 쓰고 나서도 방금 쓴 줄을 다시 읽어 반영한다.

import errno
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Union

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX가 아닌 환경에서는 원장을 쓸 수 없다(호출 거부)
    fcntl = None

NUSD_PER_USD = 1_000_000_000

# 넓은 범위부터 검사한다 - 여러 상한을 한꺼번에 넘으면 가장 넓은 범위를 사유로 보고한다
# (전체·일 상한은 킬 스위치를 켜는 근거가 된다).
SCOPES = ("total", "day", "run")


def usd(nusd: int) -> float:
    return nusd / NUSD_PER_USD


class LedgerError(RuntimeError):
    """원장을 읽거나 쓸 수 없다. 지출을 확인할 수 없으므로 호출부는 LLM 호출을 거부해야 한다."""


@dataclass(frozen=True)
class Caps:
    run_nusd: int
    day_nusd: int
    total_nusd: int

    def for_scope(self, scope: str) -> int:
        return {"run": self.run_nusd, "day": self.day_nusd, "total": self.total_nusd}[scope]


@dataclass(frozen=True)
class CallKey:
    """지출을 어디에 귀속시킬지. day는 호출부가 정한 시간대의 날짜(YYYY-MM-DD)다."""

    run_id: str
    day: str
    provider: str
    model: str
    role: str
    purpose: str


@dataclass(frozen=True)
class Reservation:
    id: str
    ts: float
    key: CallKey
    nusd: int


@dataclass(frozen=True)
class Refusal:
    scope: str
    cap_nusd: int
    committed_nusd: int
    open_nusd: int
    requested_nusd: int

    @property
    def used_nusd(self) -> int:
        return self.committed_nusd + self.open_nusd


@dataclass(frozen=True)
class Totals:
    committed: Dict[str, int]
    open: Dict[str, int]
    open_count: int
    corrupt_lines: int

    def used(self, scope: str) -> int:
        return self.committed[scope] + self.open[scope]


class _State:
    def __init__(self):
        self.open: Dict[str, Dict[str, Any]] = {}  # 예약 id -> reserve 이벤트
        self.settled: Dict[str, int] = {}  # 예약 id -> 확정된 금액
        self.committed_total = 0
        self.committed_by_day: Dict[str, int] = {}
        self.committed_by_run: Dict[str, int] = {}
        self.corrupt_lines = 0

    def charge(self, nusd: int, day: str, run_id: str) -> None:
        self.committed_total += nusd
        self.committed_by_day[day] = self.committed_by_day.get(day, 0) + nusd
        self.committed_by_run[run_id] = self.committed_by_run.get(run_id, 0) + nusd

    def apply(self, ev: Dict[str, Any]) -> None:
        kind = ev.get("ev")
        if kind == "reserve":
            if ev["id"] not in self.settled:
                self.open[ev["id"]] = ev
        elif kind in ("commit", "expire"):
            self._settle(ev)
        elif kind == "reset_day":
            self.committed_by_day[ev["day"]] = 0
        # refuse 등 나머지는 감사용 - 합계에 영향 없음

    def _settle(self, ev: Dict[str, Any]) -> None:
        rid, nusd = ev["id"], int(ev["nusd"])
        if rid in self.settled:
            # 이미 확정된 예약(만료 뒤 늦게 도착한 정산, 중복 정산): 낮추지 않고 더 클 때만 올린다
            extra = nusd - self.settled[rid]
            if extra > 0:
                self.charge(extra, ev["day"], ev["run_id"])
                self.settled[rid] = nusd
            return
        reserve = self.open.pop(rid, None) or ev  # 예약 줄이 없어도 정산 줄만으로 센다
        self.charge(nusd, reserve["day"], reserve["run_id"])
        self.settled[rid] = nusd

    def open_sum(self, scope: str, key_value: Optional[str]) -> int:
        if scope == "total":
            return sum(int(e["nusd"]) for e in self.open.values())
        field = "day" if scope == "day" else "run_id"
        return sum(int(e["nusd"]) for e in self.open.values() if e.get(field) == key_value)

    def committed(self, scope: str, key_value: Optional[str]) -> int:
        if scope == "total":
            return self.committed_total
        table = self.committed_by_day if scope == "day" else self.committed_by_run
        return table.get(key_value, 0)


class SpendLedger:
    def __init__(
        self,
        path: Union[str, os.PathLike],
        *,
        reservation_ttl_s: float = 900.0,
        clock=time.time,
        lock_timeout_s: float = 30.0,
    ):
        self.path = str(path)
        self.lock_path = self.path + ".lock"
        self.reservation_ttl_s = float(reservation_ttl_s)
        self.lock_timeout_s = float(lock_timeout_s)
        self._clock = clock
        self._mutex = threading.RLock()
        self._state = _State()
        self._offset = 0
        self._inode: Optional[int] = None
        self._dangling = False  # 파일 끝에 개행 없이 끊긴 줄이 있는가

    # ------------------------------------------------------------------ 쓰기 경로

    def reserve(self, *, caps: Caps, key: CallKey, nusd: int,
                extra: Optional[Dict[str, Any]] = None) -> Union[Reservation, Refusal]:
        """상한 안이면 예약을 기록하고 Reservation을, 넘으면 거부를 기록하고 Refusal을 돌려준다.

        Reservation을 받았다면 reserve 줄은 이미 디스크에 있다 - 그 뒤에만 요청을 보낸다.
        """
        nusd = int(nusd)
        if nusd < 0:
            raise ValueError("예약 금액은 음수일 수 없습니다")
        with self._locked():
            self._refresh()
            now = float(self._clock())
            self._expire_stale(now)
            scope_keys = {"total": None, "day": key.day, "run": key.run_id}
            base = {
                "ts": now, "day": key.day, "run_id": key.run_id, "pid": os.getpid(),
                "provider": key.provider, "model": key.model, "role": key.role, "purpose": key.purpose,
            }
            for scope in SCOPES:
                cap = caps.for_scope(scope)
                committed = self._state.committed(scope, scope_keys[scope])
                open_nusd = self._state.open_sum(scope, scope_keys[scope])
                if committed + open_nusd + nusd > cap:
                    self._append({
                        "ev": "refuse", **base, "scope": scope, "cap_nusd": cap,
                        "committed_nusd": committed, "open_nusd": open_nusd, "requested_nusd": nusd,
                    })
                    return Refusal(scope=scope, cap_nusd=cap, committed_nusd=committed,
                                   open_nusd=open_nusd, requested_nusd=nusd)
            reservation = Reservation(id=uuid.uuid4().hex, ts=now, key=key, nusd=nusd)
            self._append({"ev": "reserve", "id": reservation.id, **base, "nusd": nusd, **(extra or {})})
            return reservation

    def settle(self, reservation: Reservation, *, nusd: int, outcome: str, basis: str,
               extra: Optional[Dict[str, Any]] = None) -> None:
        """예약을 실제 비용으로 확정한다. 정산 줄은 예약 줄 없이도 집계되게 귀속 정보를 다시 싣는다."""
        key = reservation.key
        with self._locked():
            self._refresh()
            self._append({
                "ev": "commit", "id": reservation.id, "ts": float(self._clock()),
                "day": key.day, "run_id": key.run_id, "provider": key.provider, "model": key.model,
                "role": key.role, "purpose": key.purpose,
                "nusd": int(nusd), "reserved_nusd": reservation.nusd, "outcome": outcome, "basis": basis,
                **(extra or {}),
            })

    def reset_day(self, day: str, note: str = "") -> int:
        """그날의 일 상한 창을 비우고, 비운 금액을 돌려준다. 전체 합계와 열린 예약은 그대로다."""
        with self._locked():
            self._refresh()
            cleared = self._state.committed("day", day)
            self._append({"ev": "reset_day", "ts": float(self._clock()), "day": day,
                          "cleared_nusd": cleared, "note": note, "pid": os.getpid()})
            return cleared

    def probe(self) -> None:
        """원장에 쓸 수 있는지 확인한다(잠금 획득, 추가 모드로 열기). 줄은 쓰지 않는다.

        쓸 수 없으면 LedgerError. 잡이 클러스터링 같은 준비 작업을 하기 전에 부른다 - 읽기 전용
        마운트에서는 첫 LLM 호출이 어차피 거부된다.
        """
        with self._locked():
            self._refresh()
            os.close(os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))

    # ------------------------------------------------------------------ 읽기 경로

    def totals(self, *, run_id: str, day: str) -> Totals:
        """현재 합계. 파일을 쓰지도, 잠금 파일을 만들지도 않는다(읽기 전용 마운트에서도 된다)."""
        with self._mutex:
            self._refresh()
            keys = {"total": None, "day": day, "run": run_id}
            return Totals(
                committed={s: self._state.committed(s, keys[s]) for s in SCOPES},
                open={s: self._state.open_sum(s, keys[s]) for s in SCOPES},
                open_count=len(self._state.open),
                corrupt_lines=self._state.corrupt_lines,
            )

    def read_events(self) -> Iterator[Dict[str, Any]]:
        """원장의 모든 줄을 순서대로(해석 못 한 줄은 건너뛰고) 돌려준다 - 요약 CLI용."""
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return
        except OSError as e:
            raise LedgerError(f"지출 원장을 읽을 수 없습니다: {self.path} ({e})") from e
        for line in data.split(b"\n"):
            ev = _parse(line)
            if ev is not None:
                yield ev

    # ------------------------------------------------------------------ 내부

    @contextmanager
    def _locked(self):
        if fcntl is None:
            raise LedgerError("이 플랫폼에는 flock이 없어 지출 원장을 쓸 수 없습니다")
        with self._mutex:
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as e:
                raise LedgerError(f"지출 원장 잠금 파일을 열 수 없습니다: {self.lock_path} ({e})") from e
            try:
                self._flock(fd)
                try:
                    yield
                except OSError as e:
                    raise LedgerError(f"지출 원장을 읽거나 쓸 수 없습니다: {self.path} ({e})") from e
            finally:
                os.close(fd)  # 닫으면 flock도 풀린다

    def _flock(self, fd: int) -> None:
        deadline = time.monotonic() + self.lock_timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise LedgerError(f"지출 원장 잠금 실패: {self.lock_path} ({e})") from e
            if time.monotonic() >= deadline:
                raise LedgerError(
                    f"지출 원장 잠금을 {self.lock_timeout_s:.0f}초 안에 얻지 못했습니다: {self.lock_path}"
                )
            time.sleep(0.002)

    def _refresh(self) -> None:
        """파일에 새로 붙은 줄을 읽어 상태에 반영한다. 파일이 바뀌었거나 줄었으면 처음부터 다시 읽는다."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._state, self._offset, self._inode, self._dangling = _State(), 0, None, False
            return
        except OSError as e:
            raise LedgerError(f"지출 원장을 읽을 수 없습니다: {self.path} ({e})") from e
        if st.st_ino != self._inode or st.st_size < self._offset:
            self._state, self._offset, self._inode = _State(), 0, st.st_ino
        if st.st_size == self._offset:
            self._dangling = False
            return
        try:
            with open(self.path, "rb") as f:
                f.seek(self._offset)
                data = f.read()
        except OSError as e:
            raise LedgerError(f"지출 원장을 읽을 수 없습니다: {self.path} ({e})") from e
        end = data.rfind(b"\n")
        self._dangling = end != len(data) - 1
        if end < 0:
            return
        for line in data[:end].split(b"\n"):
            if not line.strip():
                continue
            ev = _parse(line)
            if ev is None:
                self._state.corrupt_lines += 1
                continue
            try:
                self._state.apply(ev)
            except (KeyError, TypeError, ValueError):
                self._state.corrupt_lines += 1
        self._offset += end + 1

    def _expire_stale(self, now: float) -> None:
        stale = [e for e in self._state.open.values() if now - float(e.get("ts", now)) > self.reservation_ttl_s]
        for e in stale:
            self._append({
                "ev": "expire", "id": e["id"], "ts": now, "day": e["day"], "run_id": e["run_id"],
                "provider": e.get("provider"), "model": e.get("model"), "role": e.get("role"),
                "purpose": e.get("purpose"), "nusd": int(e["nusd"]), "reserved_nusd": int(e["nusd"]),
                "outcome": "expired_as_spent", "basis": "reserved",
                "age_s": round(now - float(e.get("ts", now)), 3),
            })

    def _append(self, ev: Dict[str, Any]) -> None:
        """한 줄을 붙이고 디스크에 내린 뒤, 방금 쓴 줄을 파일에서 다시 읽어 상태에 반영한다."""
        data = (json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if self._dangling:
            data = b"\n" + data  # 끊긴 줄에 이어 붙지 않게 줄을 먼저 끝낸다
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        self._refresh()


def _parse(line: bytes) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return ev if isinstance(ev, dict) else None
