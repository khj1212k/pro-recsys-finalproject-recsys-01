# LLM 지출 원장 (docs/adr/0035)
#
# 프로바이더에 요청을 보내기 전에 "이 요청이 쓸 수 있는 최악 비용"을 예약하고, 응답을 받은 뒤
# 실제 비용으로 정산한다. 두 단계를 추가 전용 JSONL 파일에 한 줄씩 남긴다.
#
#   init     파일의 첫 줄. 사람이 `spend_cli init`으로 만들 때만 생긴다(원장 id).
#   reserve  요청 직전. 이 줄이 디스크에 내려간 뒤에만 네트워크 요청이 나간다.
#   commit   요청이 끝난 뒤. 실제 사용량으로 계산한 비용(또는 알 수 없으면 예약액)을 적는다.
#   expire   정산 없이 TTL을 넘긴 예약. 예약액을 그대로 지출로 확정한다.
#   refuse   상한 때문에 거부한 요청(지출 없음, 감사용).
#   reset_day  사람이 하루 창을 비운 기록.
#   torn     쓰다 끊긴 줄 하나를 다음 쓰기가 인정한 기록(그 줄의 바이트 오프셋).
#   repair   해석할 수 없는 줄들을 사람이 인정한 기록(`spend_cli repair --yes`).
#   adopt    사라지거나 줄어든 원장의 누계를 사람이 이어받은 기록(`spend_cli adopt --yes`).
#
# 상한 검사는 항상 "정산된 지출 + 아직 열려 있는 예약 + 이번 요청 > 상한"이다. 예약과 정산
# 사이에 프로세스가 죽으면 정산 줄이 없으므로 그 예약은 열린 채 최악 비용으로 계속 잡힌다
# (그 요청이 과금됐는지 알 수 없기 때문이다). 한 번 잡힌 금액은 reset_day 말고는 줄지 않는다.
#
# 닫힌 쪽으로 실패한다: 원장이 "지금까지 쓴 돈"을 증명하지 못하면 호출을 거부한다(LedgerError).
#   - 파일이 없거나 init 헤더가 없다           -> 자동으로 만들지 않는다. 0부터 다시 세지 않는다.
#   - 해석할 수 없는 줄이 있다                 -> 끝의 끊긴 한 줄(쓰다 죽은 흔적)만 예외다.
#   - 원장 밖에 둔 누계 기록(워터마크)보다 작다 -> 파일이 바뀌었거나 줄었거나 누계가 내려갔다.
#   - 방금 쓴 줄이 파일에 반영되지 않았다        -> 쓰기를 삼키는 경로(/dev/null 등).
# 워터마크는 원장과 다른 디렉터리(사용자 상태 디렉터리)에 둔다. 원장은 gitignore된 .ops/에 있어서
# `git clean -fdx` 한 번에 사라질 수 있다 - 같은 디렉터리에 두면 함께 사라진다.
#
# 금액은 nUSD(10억분의 1달러) 정수다. 토큰당 단가가 1e-7달러 단위라 float 합으로 상한
# 경계를 비교하면 실행마다 결과가 달라질 수 있다.
#
# 잠금: 원장 파일 자체에 flock을 건다(별도 잠금 파일이 없다 - 지워서 잠금을 둘로 가를 수 없다).
# flock은 열린 파일 기술자 단위라 한 프로세스의 스레드끼리도, 여러 프로세스끼리도 서로 기다린다.
# 프로세스가 죽으면 커널이 푼다. 잠금을 얻은 뒤 경로가 여전히 그 파일을 가리키는지 다시 확인한다.
# 상태는 파일에서만 만든다: 쓰고 나서도 방금 쓴 줄을 다시 읽어 반영한다.

import errno
import hashlib
import json
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX가 아닌 환경에서는 원장을 쓸 수 없다(호출 거부)
    fcntl = None

NUSD_PER_USD = 1_000_000_000
FORMAT_VERSION = 1

# 넓은 범위부터 검사한다 - 여러 상한을 한꺼번에 넘으면 가장 넓은 범위를 사유로 보고한다
# (전체·일 상한은 킬 스위치를 켜는 근거가 된다).
SCOPES = ("total", "day", "run")

CLI = "python -m core.llm.spend_cli"

# 무결성 문제 코드 (LedgerIntegrityError.code)
MISSING = "missing"                # 원장 파일이 없다
UNINITIALIZED = "uninitialized"    # 첫 줄이 init 헤더가 아니다(빈 파일, 잘린 파일, 다른 파일)
CORRUPT = "corrupt"                # 인정되지 않은 해석 불가 줄이 있다
NO_WATERMARK = "no_watermark"      # 원장 밖의 누계 기록이 없다
BAD_WATERMARK = "bad_watermark"    # 누계 기록을 읽을 수 없다
REPLACED = "replaced"              # 누계 기록이 가리키는 원장이 아니다
SHRUNK = "shrunk"                  # 파일이 기록된 크기보다 작다
BEHIND = "behind"                  # 정산 누계가 기록된 최고값보다 작다


def usd(nusd: int) -> float:
    return nusd / NUSD_PER_USD


class LedgerError(RuntimeError):
    """원장을 읽거나 쓸 수 없다. 지출을 확인할 수 없으므로 호출부는 LLM 호출을 거부해야 한다."""


class LedgerIntegrityError(LedgerError):
    """원장이 지금까지의 지출을 증명하지 못한다. 사람이 CLI(init / repair / adopt)로 풀어야 한다."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def default_state_dir() -> str:
    """워터마크를 두는 디렉터리. LLM_SPEND_STATE_DIR > $XDG_STATE_HOME > ~/.local/state.

    저장소 안(.ops/)이 아니라 사용자 상태 디렉터리다 - 원장과 한꺼번에 지워지지 않게.
    """
    explicit = os.getenv("LLM_SPEND_STATE_DIR", "").strip()
    if explicit:
        return explicit
    base = os.getenv("XDG_STATE_HOME", "").strip()
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "newsletter-recsys", "llm-spend")


def watermark_path_for(ledger_path: Union[str, os.PathLike], state_dir: Optional[Union[str, os.PathLike]] = None) -> str:
    """이 원장의 워터마크 파일 경로. 원장의 실제 경로(심볼릭 링크를 푼 것)마다 하나다."""
    state_dir = os.fspath(state_dir) if state_dir else default_state_dir()
    if not os.path.isabs(state_dir):
        raise LedgerError(f"LLM_SPEND_STATE_DIR은 절대 경로여야 합니다: {state_dir!r}")
    real_ledger = os.path.realpath(os.fspath(ledger_path))
    real_state = os.path.realpath(state_dir)
    ledger_dir = os.path.dirname(real_ledger)
    if real_state == ledger_dir or real_state.startswith(ledger_dir.rstrip(os.sep) + os.sep):
        raise LedgerError(
            f"LLM_SPEND_STATE_DIR({state_dir})이 원장 디렉터리({ledger_dir}) 안에 있습니다 - "
            "원장과 함께 지워지면 누계를 지킬 수 없습니다. 다른 위치를 지정하세요"
        )
    digest = hashlib.sha256(real_ledger.encode("utf-8")).hexdigest()[:24]
    return os.path.join(state_dir, f"ledger-{digest}.json")


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
    corrupt_lines: int                      # 해석 못 한 줄 전체(인정된 것 포함)
    unacknowledged_corrupt_lines: int = 0   # 그중 아직 인정되지 않은 것(있으면 호출이 거부된다)
    carried_nusd: int = 0                   # adopt로 이어받은 누계(정산 합계에 들어 있다)
    problem: Optional[str] = None           # 무결성 문제 설명. None이면 정상

    def used(self, scope: str) -> int:
        return self.committed[scope] + self.open[scope]


def _nusd(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"금액은 0 이상의 정수여야 합니다: {value!r}")
    return value


def _amount_or_zero(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(ev: Dict[str, Any], name: str) -> str:
    value = ev[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}: 비어 있지 않은 문자열이어야 합니다")
    return value


class _State:
    def __init__(self):
        self.ledger_id: Optional[str] = None
        self.open: Dict[str, Dict[str, Any]] = {}  # 예약 id -> reserve 이벤트
        self.settled: Dict[str, int] = {}  # 예약 id -> 확정된 금액
        self.committed_total = 0
        self.committed_by_day: Dict[str, int] = {}
        self.committed_by_run: Dict[str, int] = {}
        self.carried_total = 0
        self.last_day: Optional[str] = None  # 마지막으로 지출이 귀속된 날(워터마크의 일 누계용)
        self.corrupt_lines = 0
        # 해석 못 한 줄: 바이트 오프셋 -> 길이. torn/repair 줄이 인정하면 빠진다. 남아 있으면 호출 거부.
        self.unacked: Dict[int, int] = {}

    def charge(self, nusd: int, day: str, run_id: str) -> None:
        self.committed_total += nusd
        self.committed_by_day[day] = self.committed_by_day.get(day, 0) + nusd
        self.committed_by_run[run_id] = self.committed_by_run.get(run_id, 0) + nusd
        self.last_day = day

    def apply(self, ev: Dict[str, Any], offset: int) -> None:
        """한 줄을 반영한다. 필요한 필드가 없거나 형식이 다르면 예외 - 호출부가 해석 불가 줄로 센다."""
        kind = ev.get("ev")
        if kind == "init":
            if offset != 0 or self.ledger_id is not None:
                raise ValueError("init 줄은 파일의 첫 줄에만 올 수 있습니다")
            self.ledger_id = _text(ev, "ledger_id")
        elif kind == "reserve":
            rid = _text(ev, "id")
            _nusd(ev["nusd"])
            _text(ev, "day")
            _text(ev, "run_id")
            if rid not in self.settled:
                self.open[rid] = ev
        elif kind in ("commit", "expire"):
            self._settle(ev)
        elif kind == "reset_day":
            self.committed_by_day[_text(ev, "day")] = 0
        elif kind == "adopt":
            carried = _nusd(ev["carried_nusd"])
            carried_day = _nusd(ev.get("carried_day_nusd", 0))
            day = _text(ev, "day") if carried_day else ev.get("day")
            self.committed_total += carried
            self.carried_total += carried
            if carried_day:
                self.committed_by_day[day] = self.committed_by_day.get(day, 0) + carried_day
            if isinstance(day, str) and day:
                self.last_day = day
        elif kind in ("torn", "repair"):
            for at in ev["offsets"]:
                self.unacked.pop(_nusd(at), None)
        # refuse 등 나머지는 감사용 - 합계에 영향 없음

    def _settle(self, ev: Dict[str, Any]) -> None:
        rid, nusd = _text(ev, "id"), _nusd(ev["nusd"])
        if rid in self.settled:
            # 이미 확정된 예약(만료 뒤 늦게 도착한 정산, 중복 정산): 낮추지 않고 더 클 때만 올린다
            extra = nusd - self.settled[rid]
            if extra > 0:
                self.charge(extra, _text(ev, "day"), _text(ev, "run_id"))
                self.settled[rid] = nusd
            return
        reserve = self.open.get(rid) or ev  # 예약 줄이 없어도 정산 줄만으로 센다
        self.charge(nusd, _text(reserve, "day"), _text(reserve, "run_id"))
        self.open.pop(rid, None)
        self.settled[rid] = nusd

    def open_sum(self, scope: str, key_value: Optional[str]) -> int:
        if scope == "total":
            return sum(e["nusd"] for e in self.open.values())
        field = "day" if scope == "day" else "run_id"
        return sum(e["nusd"] for e in self.open.values() if e.get(field) == key_value)

    def committed(self, scope: str, key_value: Optional[str]) -> int:
        if scope == "total":
            return self.committed_total
        table = self.committed_by_day if scope == "day" else self.committed_by_run
        return table.get(key_value, 0)


_BAD = object()  # 워터마크 파일이 있지만 읽을 수 없다


class SpendLedger:
    def __init__(
        self,
        path: Union[str, os.PathLike],
        *,
        state_dir: Optional[Union[str, os.PathLike]] = None,
        reservation_ttl_s: float = 900.0,
        clock=time.time,
        lock_timeout_s: float = 30.0,
    ):
        self.path = os.path.abspath(os.fspath(path))
        self.watermark_path = watermark_path_for(self.path, state_dir)
        self.reservation_ttl_s = float(reservation_ttl_s)
        self.lock_timeout_s = float(lock_timeout_s)
        self._clock = clock
        self._mutex = threading.RLock()
        self._fd: Optional[int] = None  # 잠금을 쥔 동안의 원장 파일 기술자
        self._state = _State()
        self._offset = 0
        self._file_id: Optional[Tuple[int, int]] = None
        self._size = 0
        self._dangling = 0  # 파일 끝에 개행 없이 끊긴 줄의 바이트 수
        self._problem: Optional[Tuple[str, str]] = None

    # ------------------------------------------------------------------ 만들기와 사람의 복구

    def init(self, note: str = "") -> Dict[str, Any]:
        """새 원장을 만든다(첫 줄 = init 헤더). 가드는 이 메서드를 부르지 않는다 - 사람이 CLI로만 만든다.

        같은 경로의 이전 원장이 남긴 워터마크가 있으면 건드리지 않는다. 그러면 새 원장은 "바뀐 원장"으로
        거부되고, 사람이 adopt()로 이전 누계를 이어받아야 호출이 허용된다.
        """
        if fcntl is None:
            raise LedgerError("이 플랫폼에는 flock이 없어 지출 원장을 쓸 수 없습니다")
        with self._mutex:
            prior = self._read_watermark()
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raise LedgerError(
                    f"이미 파일이 있습니다: {self.path} (덮어쓰지 않습니다. 헤더가 없는 파일이면 다른 이름으로 "
                    "옮겨 둔 뒤 다시 init 하세요)"
                ) from None
            except OSError as e:
                raise LedgerError(f"지출 원장을 만들 수 없습니다: {self.path} ({e})") from e
            try:
                self._flock(fd, time.monotonic() + self.lock_timeout_s)
                self._fd = fd
                self._forget()
                ledger_id = uuid.uuid4().hex
                self._append({"ev": "init", "v": FORMAT_VERSION, "ledger_id": ledger_id,
                              "ts": float(self._clock()), "pid": os.getpid(), "note": note})
                if prior is None:
                    self._write_watermark()
                    self._refresh(strict=False)
            except OSError as e:
                raise LedgerError(f"지출 원장을 만들 수 없습니다: {self.path} ({e})") from e
            finally:
                self._fd = None
                os.close(fd)
            return {
                "ledger": self.path, "ledger_id": ledger_id, "watermark": self.watermark_path,
                "prior": prior if isinstance(prior, dict) else None, "prior_unreadable": prior is _BAD,
                "problem": self._problem[1] if self._problem else None,
            }

    def repair(self, note: str = "") -> Dict[str, Any]:
        """해석할 수 없는 줄들을 사람이 확인했다고 기록한다. 그 줄들의 금액은 알 수 없다 - 정산 누계가
        워터마크보다 작아졌으면 이어서 adopt()가 그 차이를 이어받아야 한다."""
        with self._locked():
            self._refresh(strict=False)
            if self._state.ledger_id is None:
                raise LedgerIntegrityError(*self._diagnosis())
            unacked = dict(sorted(self._state.unacked.items()))
            if unacked:
                self._append({"ev": "repair", "ts": float(self._clock()), "pid": os.getpid(),
                              "offsets": list(unacked), "lines": len(unacked), "bytes": sum(unacked.values()),
                              "note": note})
            return {"acknowledged": unacked, "problem": self._problem[1] if self._problem else None}

    def adopt(self, note: str = "", *, dry_run: bool = False) -> Dict[str, Any]:
        """지금 원장을 기준으로 삼는다. 워터마크가 기억하는 누계를 이 원장에 이어받는다.

        누계는 내려가지 않는다. 워터마크가 기억하는 사용액은 "정산 누계 + 그때 열려 있던 예약"이다 -
        원장과 함께 사라진 예약은 과금됐는지 알 수 없으므로 만료된 예약처럼 최악 비용으로 친다.
        - 같은 원장이 줄었거나 누계가 내려갔으면(shrunk, behind) 지금 원장의 사용액과의 차이를 더한다.
        - 다른 원장으로 바뀌었으면(replaced) 워터마크의 사용액 전부를 더한다 - 새 원장에 이미 적힌 지출은
          옛 원장의 사용액에 들어 있지 않다.
        - 워터마크가 없거나 읽을 수 없으면 이어받을 값을 모른다(0). 그 사실을 adopt 줄에 남긴다.
        마지막 날의 일 사용액도 같은 규칙으로 이어받는다. dry_run이면 무엇을 할지만 돌려준다.
        """
        if dry_run:
            with self._mutex:
                self._refresh(strict=False)
                return self._adoption_plan()
        with self._locked():
            self._refresh(strict=False)
            plan = self._adoption_plan()
            if plan["blocked"]:
                raise LedgerIntegrityError(*self._diagnosis())
            if plan["was"] is not None:
                self._append({
                    "ev": "adopt", "ts": float(self._clock()), "pid": os.getpid(),
                    "reason": plan["was"], "watermark": plan["watermark"],
                    "prev_ledger_id": plan["prev_ledger_id"], "prev_size": plan["prev_size"],
                    "prev_committed_total_nusd": plan["prev_committed_total_nusd"],
                    "prev_open_nusd": plan["prev_open_nusd"],
                    "carried_nusd": plan["carried_nusd"], "day": plan["day"],
                    "carried_day_nusd": plan["carried_day_nusd"], "note": note,
                })
                self._write_watermark()
                self._refresh(strict=False)
            return {**plan, "problem": self._problem[1] if self._problem else None}

    def _adoption_plan(self) -> Dict[str, Any]:
        state, problem = self._state, self._problem
        watermark = self._read_watermark()
        known = isinstance(watermark, dict)
        carried, day, carried_day = 0, None, 0
        prev_open = _amount_or_zero(watermark.get("open_nusd")) if known else None
        if known and problem is not None:
            same_ledger = watermark["ledger_id"] == state.ledger_id
            carried = watermark["committed_total_nusd"] + prev_open
            if same_ledger:
                carried = max(0, carried - state.committed_total - state.open_sum("total", None))
            day = watermark.get("day")
            if isinstance(day, str) and day:
                carried_day = (_amount_or_zero(watermark.get("day_committed_nusd"))
                               + _amount_or_zero(watermark.get("day_open_nusd")))
                if same_ledger:
                    carried_day = max(0, carried_day - state.committed_by_day.get(day, 0) - state.open_sum("day", day))
            else:
                day = None
        return {
            "was": problem[0] if problem else None, "was_message": problem[1] if problem else None,
            # 헤더가 없거나 인정되지 않은 해석 불가 줄이 있으면 adopt로 풀 수 없다(init / repair가 먼저다)
            "blocked": state.ledger_id is None or bool(state.unacked),
            "watermark": "ok" if known else ("unreadable" if watermark is _BAD else "missing"),
            "watermark_known": known,
            "prev_ledger_id": watermark["ledger_id"] if known else None,
            "prev_size": watermark["size"] if known else None,
            "prev_committed_total_nusd": watermark["committed_total_nusd"] if known else None,
            "prev_open_nusd": prev_open,
            "committed_total_nusd": state.committed_total,
            "open_nusd": state.open_sum("total", None),
            "carried_nusd": carried, "day": day, "carried_day_nusd": carried_day,
        }

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
            # 방금 쓴 예약이 파일에서 다시 읽혀 상한 계산에 들어왔는지 확인한다. 아니면 이 예약은 다음
            # 호출의 상한 검사에 잡히지 않는다 - 허용하지 않는다.
            if reservation.id not in self._state.open or self._problem is not None:
                raise LedgerError(
                    f"예약 줄이 지출 원장에 반영되지 않았습니다: {self.path}"
                    + (f" ({self._problem[1]})" if self._problem else "")
                )
            return reservation

    def settle(self, reservation: Reservation, *, nusd: int, outcome: str, basis: str,
               extra: Optional[Dict[str, Any]] = None) -> None:
        """예약을 실제 비용으로 확정한다. 정산 줄은 예약 줄 없이도 집계되게 귀속 정보를 다시 싣는다.

        원장에 무결성 문제가 생긴 뒤에도(파일이 있기만 하면) 정산 줄은 쓴다 - 나간 요청의 기록이다.
        """
        key = reservation.key
        with self._locked():
            self._refresh(strict=False)
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
        """다음 예약이 쓰일 수 있는 상태인지 확인한다(잠금 획득, 쓰기 모드로 열기, 무결성). 줄은 쓰지 않는다.

        아니면 LedgerError. 잡이 클러스터링 같은 준비 작업을 하기 전에 부른다 - 원장이 없거나 읽기 전용
        마운트이거나 무결성 문제가 있으면 첫 LLM 호출이 어차피 거부된다.
        """
        with self._locked():
            self._refresh()

    # ------------------------------------------------------------------ 읽기 경로

    def totals(self, *, run_id: str, day: str, strict: bool = True) -> Totals:
        """현재 합계. 파일을 쓰지도 잠그지도 않는다(읽기 전용 마운트에서도 된다).

        무결성 문제가 있으면 LedgerIntegrityError다. strict=False(요약 CLI)면 읽히는 만큼의 합계와
        문제 설명을 함께 돌려준다 - 그 합계로 호출을 허용하면 안 된다.
        """
        with self._mutex:
            self._refresh(strict=strict)
            keys = {"total": None, "day": day, "run": run_id}
            return Totals(
                committed={s: self._state.committed(s, keys[s]) for s in SCOPES},
                open={s: self._state.open_sum(s, keys[s]) for s in SCOPES},
                open_count=len(self._state.open),
                corrupt_lines=self._state.corrupt_lines,
                unacknowledged_corrupt_lines=len(self._state.unacked),
                carried_nusd=self._state.carried_total,
                problem=self._problem[1] if self._problem else None,
            )

    def inspect(self) -> Dict[str, Any]:
        """무결성 상태(요약·복구 CLI용). 쓰지 않는다."""
        with self._mutex:
            self._refresh(strict=False)
            watermark = self._read_watermark()
            return {
                "ok": self._problem is None,
                "code": self._problem[0] if self._problem else None,
                "problem": self._problem[1] if self._problem else None,
                "ledger_id": self._state.ledger_id,
                "committed_total_nusd": self._state.committed_total,
                "unacknowledged_corrupt_lines": dict(sorted(self._state.unacked.items())),
                "watermark_file": self.watermark_path,
                "watermark": watermark if isinstance(watermark, dict) else None,
            }

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

    # ------------------------------------------------------------------ 내부: 잠금

    @contextmanager
    def _locked(self):
        if fcntl is None:
            raise LedgerError("이 플랫폼에는 flock이 없어 지출 원장을 쓸 수 없습니다")
        with self._mutex:
            fd = self._open_locked()
            self._fd = fd
            try:
                yield
            except OSError as e:
                raise LedgerError(f"지출 원장을 읽거나 쓸 수 없습니다: {self.path} ({e})") from e
            finally:
                self._fd = None
                os.close(fd)  # 닫으면 flock도 풀린다

    def _open_locked(self) -> int:
        """원장 파일을 쓰기 모드로 열고 그 파일에 배타 잠금을 건다. 파일을 만들지 않는다.

        잠금을 기다리는 사이에 경로의 파일이 지워지거나 다른 파일로 바뀌었으면, 쥔 잠금은 아무도 보지
        않는 파일의 것이다 - 놓고 지금 경로의 파일로 다시 시도한다(없으면 거부).
        """
        deadline = time.monotonic() + self.lock_timeout_s
        while True:
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_APPEND)
            except FileNotFoundError:
                raise LedgerIntegrityError(MISSING, self._missing_message()) from None
            except OSError as e:
                raise LedgerError(f"지출 원장을 쓰기 모드로 열 수 없습니다: {self.path} ({e})") from e
            try:
                held = os.fstat(fd)
                if not stat.S_ISREG(held.st_mode):
                    raise LedgerError(f"지출 원장이 일반 파일이 아닙니다(쓴 줄이 남지 않습니다): {self.path}")
                self._flock(fd, deadline)
                try:
                    current = os.stat(self.path)
                except FileNotFoundError:
                    raise LedgerIntegrityError(MISSING, self._missing_message()) from None
                except OSError as e:
                    raise LedgerError(f"지출 원장을 읽을 수 없습니다: {self.path} ({e})") from e
                if (current.st_dev, current.st_ino) == (held.st_dev, held.st_ino):
                    return fd
            except BaseException:
                os.close(fd)
                raise
            os.close(fd)
            if time.monotonic() >= deadline:
                raise LedgerError(f"지출 원장 파일이 계속 바뀌어 잠금을 얻지 못했습니다: {self.path}")

    def _flock(self, fd: int, deadline: float) -> None:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise LedgerError(f"지출 원장 잠금 실패: {self.path} ({e})") from e
            if time.monotonic() >= deadline:
                raise LedgerError(
                    f"지출 원장 잠금을 {self.lock_timeout_s:.0f}초 안에 얻지 못했습니다: {self.path}"
                )
            time.sleep(0.002)

    # ------------------------------------------------------------------ 내부: 읽기와 무결성

    def _forget(self) -> None:
        self._state, self._offset, self._file_id, self._size, self._dangling = _State(), 0, None, 0, 0

    def _missing_message(self) -> str:
        return (f"지출 원장이 없습니다: {self.path}. 자동으로 만들지 않습니다(지워진 원장을 0부터 다시 세지 않게) - "
                f"`{CLI} init`으로 만드세요. 전에 쓰던 원장이 사라진 것이면 이어서 `{CLI} adopt --yes`로 누계를 이어받습니다")

    def _refresh(self, *, strict: bool = True) -> None:
        """파일에 새로 붙은 줄을 읽어 상태에 반영하고 무결성을 다시 판정한다.

        파일이 바뀌었거나 줄었으면 처음부터 다시 읽는다. strict면 무결성 문제를 LedgerIntegrityError로 낸다.
        """
        # 워터마크를 원장보다 먼저 읽는다: 잠금 없이 읽는 경로에서 그 사이에 다른 프로세스가 줄을 붙여도
        # 원장이 워터마크보다 앞설 뿐이다(뒤처진 것으로 잘못 보지 않는다).
        watermark = self._read_watermark()
        fd, own = self._fd, False
        if fd is None:
            try:
                fd = os.open(self.path, os.O_RDONLY)
            except FileNotFoundError:
                self._forget()
                self._problem = (MISSING, self._missing_message())
                if strict:
                    raise LedgerIntegrityError(*self._problem) from None
                return
            except OSError as e:
                raise LedgerError(f"지출 원장을 읽을 수 없습니다: {self.path} ({e})") from e
            own = True
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise LedgerError(f"지출 원장이 일반 파일이 아닙니다(쓴 줄이 남지 않습니다): {self.path}")
            file_id = (st.st_dev, st.st_ino)
            if file_id != self._file_id or st.st_size < self._offset:
                self._forget()
                self._file_id = file_id
            if st.st_size > self._offset:
                data = _pread_all(fd, self._offset, st.st_size - self._offset)
                start = self._offset
                self._consume(data)
                self._dangling = start + len(data) - self._offset
            else:
                self._dangling = 0
            self._size = self._offset + self._dangling
        except OSError as e:
            raise LedgerError(f"지출 원장을 읽을 수 없습니다: {self.path} ({e})") from e
        finally:
            if own:
                os.close(fd)
        self._problem = self._diagnose(watermark)
        if strict and self._problem is not None:
            raise LedgerIntegrityError(*self._problem)

    def _consume(self, data: bytes) -> None:
        end = data.rfind(b"\n")
        if end < 0:
            return
        position = self._offset
        for line in data[:end].split(b"\n"):
            at, position = position, position + len(line) + 1
            if not line.strip():
                continue
            ev = _parse(line)
            try:
                if ev is None:
                    raise ValueError("JSON 객체가 아닙니다")
                self._state.apply(ev, at)
            except (KeyError, TypeError, ValueError):
                self._state.corrupt_lines += 1
                self._state.unacked[at] = len(line)
        self._offset += end + 1

    def _diagnosis(self) -> Tuple[str, str]:
        return self._problem or (CORRUPT, f"지출 원장을 쓸 수 없는 상태입니다: {self.path}")

    def _diagnose(self, watermark: Any) -> Optional[Tuple[str, str]]:
        state = self._state
        if state.ledger_id is None:
            return (UNINITIALIZED,
                    f"지출 원장에 init 헤더가 없습니다(비었거나 잘렸거나 다른 파일입니다): {self.path}. 파일을 다른 "
                    f"이름으로 옮겨 두고 `{CLI} init`, 이어서 `{CLI} adopt --yes`로 누계를 이어받으세요")
        if state.unacked:
            first = min(state.unacked)
            return (CORRUPT,
                    f"지출 원장에 해석할 수 없는 줄이 {len(state.unacked)}개 있습니다(첫 줄의 바이트 오프셋 {first}): "
                    f"{self.path}. 그 줄들의 지출을 알 수 없어 호출을 거부합니다 - 확인한 뒤 `{CLI} repair --yes`로 인정하세요")
        if watermark is None:
            return (NO_WATERMARK,
                    f"원장 밖에 두는 누계 기록이 없습니다: {self.watermark_path}. 원장이 바뀌거나 줄었는지 확인할 수 "
                    f"없습니다 - `{CLI} adopt --yes`로 지금 원장을 기준으로 삼으세요")
        if watermark is _BAD:
            return (BAD_WATERMARK,
                    f"원장 밖에 두는 누계 기록을 읽을 수 없습니다: {self.watermark_path}. `{CLI} adopt --yes`로 지금 "
                    "원장을 기준으로 삼으세요(이어받을 누계는 알 수 없습니다)")
        prev = f"기록된 누계 ${usd(watermark['committed_total_nusd']):.6f}"
        if watermark["ledger_id"] != state.ledger_id:
            return (REPLACED,
                    f"지출 원장이 다른 파일로 바뀌었습니다(기록된 원장 {watermark['ledger_id'][:8]}, 지금 "
                    f"{state.ledger_id[:8]}; {prev}): {self.path}. `{CLI} adopt --yes`로 누계를 이어받으세요")
        if self._size < watermark["size"]:
            return (SHRUNK,
                    f"지출 원장이 줄었습니다({self._size} < 기록된 {watermark['size']}바이트; {prev}): {self.path}. "
                    f"`{CLI} adopt --yes`로 누계를 이어받으세요")
        if state.committed_total < watermark["committed_total_nusd"]:
            return (BEHIND,
                    f"지출 원장의 정산 누계 ${usd(state.committed_total):.6f}가 {prev}보다 작습니다: {self.path}. "
                    f"`{CLI} adopt --yes`로 누계를 이어받으세요")
        return None

    def _read_watermark(self) -> Any:
        """워터마크 dict, 없으면 None, 있지만 읽을 수 없으면 _BAD."""
        try:
            with open(self.watermark_path, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            return None
        except OSError:
            return _BAD
        try:
            watermark = json.loads(raw)
            if not isinstance(watermark, dict):
                return _BAD
            _text(watermark, "ledger_id")
            _nusd(watermark["size"])
            _nusd(watermark["committed_total_nusd"])
        except (KeyError, TypeError, ValueError):
            return _BAD
        return watermark

    def _write_watermark(self) -> None:
        """지금 원장의 id·크기·정산 누계를 원장 디렉터리 밖에 적는다(임시 파일에 쓰고 바꿔치기)."""
        state = self._state
        payload = {
            "v": FORMAT_VERSION, "ledger_path": self.path, "ledger_id": state.ledger_id, "size": self._size,
            "committed_total_nusd": state.committed_total,
            # 열려 있는 예약: 무결성 판정에는 쓰지 않는다(정산되면 줄어든다). 원장이 사라졌을 때 adopt가
            # 최악 비용으로 이어받는 데만 쓴다.
            "open_nusd": state.open_sum("total", None),
            "day": state.last_day,
            "day_committed_nusd": state.committed_by_day.get(state.last_day, 0) if state.last_day else 0,
            "day_open_nusd": state.open_sum("day", state.last_day) if state.last_day else 0,
            "updated_ts": float(self._clock()),
        }
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        os.makedirs(os.path.dirname(self.watermark_path), mode=0o700, exist_ok=True)
        tmp = f"{self.watermark_path}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.watermark_path)

    # ------------------------------------------------------------------ 내부: 쓰기

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
        """한 줄을 붙이고 디스크에 내린 뒤, 방금 쓴 줄을 파일에서 다시 읽어 상태에 반영한다.

        쓴 만큼 파일이 자라지 않았거나, 쓰는 사이에 경로가 다른 파일을 가리키게 됐거나, 다시 읽은 위치가
        파일 끝이 아니면 LedgerError다. 원장이 정상일 때만 워터마크를 지금 상태로 옮긴다.
        """
        fd = self._fd
        if fd is None:
            raise LedgerError("지출 원장 잠금 없이 쓰려 했습니다")
        lines: List[Dict[str, Any]] = [ev]
        prefix = b""
        if self._dangling:
            # 앞선 쓰기가 줄을 끝내지 못하고 죽었다. 그 줄을 끝내고, 그 줄이 "쓰다 끊긴 줄"임을 적는다
            # (끝이 아닌 곳의 해석 불가 줄은 인정 기록이 없으면 호출 거부 사유다).
            prefix = b"\n"
            lines.insert(0, {"ev": "torn", "ts": float(self._clock()), "pid": os.getpid(),
                             "offsets": [self._offset], "bytes": self._dangling})
        data = prefix + b"".join(
            (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8") for line in lines
        )
        before = os.fstat(fd).st_size
        _write_all(fd, data)
        os.fsync(fd)
        after = os.fstat(fd)
        if after.st_size != before + len(data):
            raise LedgerError(
                f"지출 원장에 쓴 줄이 파일에 남지 않았습니다({len(data)}바이트를 썼는데 크기가 {before} -> "
                f"{after.st_size}): {self.path}"
            )
        try:
            current = os.stat(self.path)
        except FileNotFoundError:
            raise LedgerIntegrityError(MISSING, f"쓰는 사이에 지출 원장이 사라졌습니다: {self.path}") from None
        if (current.st_dev, current.st_ino) != (after.st_dev, after.st_ino):
            raise LedgerIntegrityError(REPLACED, f"쓰는 사이에 지출 원장이 다른 파일로 바뀌었습니다: {self.path}")
        self._refresh(strict=False)
        if self._offset != after.st_size:
            raise LedgerError(f"지출 원장에 쓴 줄을 다시 읽지 못했습니다: {self.path}")
        if self._problem is None:
            self._write_watermark()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def _pread_all(fd: int, offset: int, size: int) -> bytes:
    chunks = []
    while size > 0:
        chunk = os.pread(fd, min(size, 1 << 20), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        size -= len(chunk)
    return b"".join(chunks)


def _parse(line: bytes) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return ev if isinstance(ev, dict) else None
