# LLM 지출 상한과 런 서킷브레이커 (docs/adr/0035)
#
# 프로바이더에 나가는 모든 요청(재시도는 시도마다)이 이 가드를 거친다:
#
#   guard = get_budget_guard()
#   spend = guard.begin_attempt(provider=..., model=..., purpose=..., messages=..., schema=..., max_tokens=...)
#   try:
#       ... 네트워크 요청 ...
#       spend.settle("ok", usage)
#   except ...:
#       spend.settle("timeout")        # 또는 "http_503", "schema_invalid" 등
#   finally:
#       spend.ensure_settled()         # 어느 분기도 정산하지 않았으면 예약액 그대로 지출로 잡는다
#
# begin_attempt는 "입력 토큰 상한 추정 x 입력 단가 + max_tokens x 출력 단가"를 지출 원장
# (core/llm/spend_ledger.py)에 예약한다. 런·일·전체 상한 중 하나라도 넘으면 네트워크 요청 전에
# LLMBudgetExceeded를 낸다. 일·전체 상한이면 킬 스위치 파일도 만든다(다른 프로세스도 멈추게).
#
# 닫힌 쪽으로 실패한다: 단가표에 없는 모델, 숫자로 읽히지 않는 설정, 쓸 수 없는 원장은 전부
# "호출 거부"다. 끄는 스위치는 없다 - 상한을 올리는 것만 가능하다.
#
# 중단 예외(LLMRunStop)는 BaseException이다. 이 파이프라인에는 LLM 실패를 `except Exception`으로
# 받아 로컬 초안이나 원문 그대로를 대신 쓰는 곳이 여럿 있다(문체 변환 노드, 레거시 클라이언트,
# Stage5의 클러스터별 예외 처리). 예산 초과가 그 경로로 들어가면 "LLM 없이 만든 글"이 발행될 수
# 있으므로, 중단은 그 처리기들을 통과해 Stage5까지 올라가야 한다. jobs.runtime.JobTerminated가
# 같은 이유로 BaseException이다.

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config.settings import Settings
from core.llm.client import LLMUsage
from core.llm.pricing import DEFAULT_PRICING_PATH, PriceTable, load_pricing
from core.llm.spend_ledger import (
    NUSD_PER_USD,
    CallKey,
    Caps,
    LedgerError,
    Refusal,
    Reservation,
    SpendLedger,
    usd,
)
from core.llm_metrics import get_metrics_collector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 중단 예외
# ---------------------------------------------------------------------------

class LLMRunStop(BaseException):
    """이번 런은 더 이상 LLM을 부르면 안 된다. 호출 하나의 실패가 아니라 런 전체의 중단 신호다."""

    code = "llm_run_stop"

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details: Dict[str, Any] = dict(details)

    def as_note(self) -> Dict[str, Any]:
        """job_runs.stats, cluster_outcomes, 메트릭 요약에 그대로 넣는 기록."""
        return {"code": self.code, "message": str(self), **self.details}

    def again(self) -> "LLMRunStop":
        """같은 중단을 다른 스레드·다음 호출에서 다시 낼 때 쓰는 새 인스턴스."""
        return type(self)(str(self), **self.details)


class LLMBudgetRefusal(LLMRunStop):
    """지출 가드가 요청을 보내기 전에 거부했다."""

    code = "llm_budget_refused"


class LLMBudgetExceeded(LLMBudgetRefusal):
    """이 요청의 최악 비용을 더하면 런·일·전체 상한 중 하나를 넘는다. details["scope"]가 그 범위다."""

    code = "llm_budget_exceeded"


class LLMUnpricedModel(LLMBudgetRefusal):
    """단가표에 없는 모델이다. 비용을 0으로 치지 않고 거부한다."""

    code = "llm_unpriced_model"


class LLMBudgetUnavailable(LLMBudgetRefusal):
    """설정이나 원장을 읽고 쓸 수 없어 지출을 확인하지 못한다."""

    code = "llm_budget_unavailable"


class LLMCircuitOpen(LLMRunStop):
    """연속 인프라 실패 또는 결제 불가(HTTP 402)로 런을 멈춘다."""

    code = "llm_circuit_open"


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

class BudgetConfigError(ValueError):
    """상한 설정을 해석할 수 없다. 기본값으로 넘어가지 않는다 - 오타 하나가 상한을 조용히 바꾸면 안 된다."""


def _setting(name: str) -> str:
    """환경변수(호출 시점) > Settings.<name>_DEFAULT."""
    value = os.getenv(name)
    if value is None or not value.strip():
        value = getattr(Settings, f"{name}_DEFAULT")
    return str(value).strip()


def _decimal(name: str, raw: str, *, minimum: Decimal, exclusive: bool = False) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise BudgetConfigError(f"{name}={raw!r}: 숫자가 아닙니다") from None
    if not value.is_finite():
        raise BudgetConfigError(f"{name}={raw!r}: 유한한 숫자가 아닙니다")
    if value < minimum or (exclusive and value == minimum):
        raise BudgetConfigError(f"{name}={raw!r}: {minimum}{'보다 커야' if exclusive else ' 이상이어야'} 합니다")
    return value


def _cap_nusd(name: str) -> int:
    value = _decimal(name, _setting(name), minimum=Decimal(0))
    return int((value * NUSD_PER_USD).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class BudgetConfig:
    caps: Caps
    ledger_path: str
    day_tz: str
    reservation_ttl_s: float
    bytes_per_token: float
    krw_per_usd: Decimal
    breaker_threshold: int
    # 단가표에 없는 모델에 적용할 (입력, 출력) USD/1M. None이면 그런 모델은 거부한다.
    fallback_price: Optional[Tuple[Decimal, Decimal]]

    @classmethod
    def from_env(cls) -> "BudgetConfig":
        caps = Caps(
            run_nusd=_cap_nusd("LLM_BUDGET_RUN_USD"),
            day_nusd=_cap_nusd("LLM_BUDGET_DAY_USD"),
            total_nusd=_cap_nusd("LLM_BUDGET_TOTAL_USD"),
        )
        day_tz = _setting("LLM_BUDGET_DAY_TZ")
        try:
            ZoneInfo(day_tz)
        except Exception:  # noqa: BLE001 - 잘못된 키는 ZoneInfoNotFoundError/ValueError 등으로 온다
            raise BudgetConfigError(f"LLM_BUDGET_DAY_TZ={day_tz!r}: 알 수 없는 시간대입니다") from None

        threshold_raw = _setting("LLM_CIRCUIT_BREAKER_THRESHOLD")
        try:
            threshold = int(threshold_raw)
        except ValueError:
            raise BudgetConfigError(f"LLM_CIRCUIT_BREAKER_THRESHOLD={threshold_raw!r}: 정수가 아닙니다") from None
        if threshold < 0:
            raise BudgetConfigError(f"LLM_CIRCUIT_BREAKER_THRESHOLD={threshold_raw!r}: 0 이상이어야 합니다")

        fb_in = os.getenv("LLM_BUDGET_FALLBACK_INPUT_PER_1M", "").strip()
        fb_out = os.getenv("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", "").strip()
        fallback = None
        if fb_in or fb_out:
            if not (fb_in and fb_out):
                raise BudgetConfigError(
                    "LLM_BUDGET_FALLBACK_INPUT_PER_1M과 LLM_BUDGET_FALLBACK_OUTPUT_PER_1M은 둘 다 있어야 합니다"
                )
            fallback = (
                _decimal("LLM_BUDGET_FALLBACK_INPUT_PER_1M", fb_in, minimum=Decimal(0), exclusive=True),
                _decimal("LLM_BUDGET_FALLBACK_OUTPUT_PER_1M", fb_out, minimum=Decimal(0), exclusive=True),
            )

        return cls(
            caps=caps,
            ledger_path=os.getenv("LLM_SPEND_LEDGER_FILE", "").strip() or Settings.LLM_SPEND_LEDGER_FILE_DEFAULT,
            day_tz=day_tz,
            reservation_ttl_s=float(_decimal(
                "LLM_BUDGET_RESERVATION_TTL_S", _setting("LLM_BUDGET_RESERVATION_TTL_S"),
                minimum=Decimal(0), exclusive=True,
            )),
            bytes_per_token=float(_decimal(
                "LLM_BUDGET_BYTES_PER_TOKEN", _setting("LLM_BUDGET_BYTES_PER_TOKEN"),
                minimum=Decimal(0), exclusive=True,
            )),
            krw_per_usd=_decimal(
                "LLM_BUDGET_KRW_PER_USD", _setting("LLM_BUDGET_KRW_PER_USD"), minimum=Decimal(0), exclusive=True,
            ),
            breaker_threshold=threshold,
            fallback_price=fallback,
        )

    def day_of(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, ZoneInfo(self.day_tz)).date().isoformat()


# ---------------------------------------------------------------------------
# 비용 산술
# ---------------------------------------------------------------------------

# 메시지마다 붙는 역할 표시·구분 토큰과 요청 전체의 고정 토큰에 대한 여유분. 프로바이더가 공개한
# 수치가 아니라 넉넉히 잡은 값이다(OpenAI 계열은 메시지당 3~4토큰).
PER_MESSAGE_OVERHEAD_TOKENS = 16
BASE_OVERHEAD_TOKENS = 64


def estimate_prompt_tokens(messages: List[Dict[str, Any]], schema: Any = None, *, bytes_per_token: float = 1.0) -> int:
    """요청을 보내기 전에 쓰는 입력 토큰 수의 상한.

    토크나이저를 돌리지 않는다(Gemini 토크나이저는 로컬에 없다). BPE/SentencePiece의 토큰은 최소
    1바이트를 덮으므로 UTF-8 바이트 수가 토큰 수를 넘지 못한다 - bytes_per_token=1.0이 그 상한이다.
    한국어는 글자당 3바이트라 실제의 몇 배로 잡히지만, 예약은 정산 때 실제 값으로 바뀐다.
    구조화 출력 스키마도 입력으로 세는 프로바이더가 있어 JSON 스키마 길이를 더한다.
    """
    n_bytes = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else message
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        role = str(message.get("role", "")) if isinstance(message, dict) else ""
        n_bytes += len(content.encode("utf-8")) + len(role.encode("utf-8"))
    if schema is not None:
        spec = schema.model_json_schema() if hasattr(schema, "model_json_schema") else schema
        n_bytes += len(json.dumps(spec, ensure_ascii=False, default=str).encode("utf-8"))
    return math.ceil(n_bytes / bytes_per_token) + PER_MESSAGE_OVERHEAD_TOKENS * len(messages) + BASE_OVERHEAD_TOKENS


def token_cost_nusd(tokens: int, usd_per_1m: Decimal) -> int:
    """토큰 수 x 단가(USD/1M)를 nUSD로. 1 nUSD 미만은 올린다(상한 쪽으로)."""
    # USD/1M 토큰 = 1e-6 USD/토큰 = 1,000 nUSD/토큰
    return int((Decimal(int(tokens)) * usd_per_1m * 1000).to_integral_value(rounding=ROUND_CEILING))


def billable_tokens(usage: Optional[LLMUsage]) -> Optional[Tuple[int, int]]:
    """응답 usage에서 과금 대상 (입력, 출력) 토큰을 구한다. usage가 비었으면 None(= 알 수 없음).

    출력은 thinking 토큰까지다. total이 prompt+completion보다 크면 그 차이를 출력으로 센다
    (completion에 thinking을 넣지 않고 total에만 넣는 프로바이더). total이 없는데 thinking만
    있으면 completion에 포함됐는지 알 수 없으므로 더한다. 캐시된 입력은 할인 단가를 적용하지
    않고 전부 정가로 센다(단가표에 캐시 단가가 없다) - 둘 다 과대 계상 쪽이다.
    """
    if usage is None or (not usage.input_tokens and not usage.output_tokens):
        return None
    output = usage.output_tokens
    if usage.total_tokens is not None:
        output = max(output, usage.total_tokens - usage.input_tokens)
    elif usage.thinking_tokens:
        output += usage.thinking_tokens
    return usage.input_tokens, output


# 원장의 역할(role) 열. 클라이언트 인스턴스는 (provider, model)당 하나를 여러 역할이 함께 쓰므로
# (generator와 tone의 기본 모델이 같다) 인스턴스로는 역할을 알 수 없다 - 호출부가 넘기는 purpose로 정한다.
ROLE_BY_PURPOSE = {
    "newsletter_content_gen": "generator",
    "newsletter_meta_gen": "generator",
    "cluster_eval": "judge",
    "newsletter_eval": "judge",
    "tone_convert": "tone",
}


def role_for_purpose(purpose: str) -> str:
    return ROLE_BY_PURPOSE.get(purpose, "other")


# ---------------------------------------------------------------------------
# 시도 결과 -> 과금 근거 / 브레이커 효과
# ---------------------------------------------------------------------------

# 프로바이더가 응답을 돌려준 결과(200). 토큰이 과금됐고, 프로바이더는 살아 있다.
RESPONSE_OUTCOMES = frozenset({"ok", "schema_invalid", "length", "content_filter"})

# HTTP 오류로 실패한 요청은 과금하지 않는다고 공식 문서로 확인한 프로바이더.
# - gemini: https://ai.google.dev/gemini-api/docs/billing (문서 갱신 2026-09-28 UTC, 접근 2026-10-06)
#   "400 또는 500 오류로 실패한 요청은 사용한 토큰이 과금되지 않는다"는 취지의 FAQ.
# 여기에 없는 프로바이더의 HTTP 오류는 "알 수 없음"으로 보고 예약액을 그대로 지출로 잡는다.
HTTP_ERROR_NOT_BILLED_PROVIDERS = frozenset({"gemini"})


def billing_basis(outcome: str, provider: str, usage: Optional[LLMUsage]) -> str:
    """"actual"(보고된 사용량) / "not_billed"(0) / "reserved"(알 수 없어 예약액 그대로)."""
    if outcome in RESPONSE_OUTCOMES:
        return "actual" if billable_tokens(usage) is not None else "reserved"
    if outcome.startswith("http_"):
        return "not_billed" if provider in HTTP_ERROR_NOT_BILLED_PROVIDERS else "reserved"
    return "reserved"  # timeout, connection, error, unsettled: 요청이 처리됐는지 모른다


def _http_status(outcome: str) -> Optional[int]:
    if outcome.startswith("http_"):
        try:
            return int(outcome[5:])
        except ValueError:
            return None
    return None


def breaker_effect(outcome: str) -> str:
    """"healthy"(카운트 0으로) / "failure"(+1) / "fatal"(즉시 중단) / "neutral"."""
    if outcome in RESPONSE_OUTCOMES:
        return "healthy"
    status = _http_status(outcome)
    if status == 402:
        return "fatal"  # 선불 잔액 소진 - 같은 런 안에서는 회복되지 않는다
    if status is not None and status >= 500:
        return "failure"
    if outcome in ("timeout", "connection"):
        return "failure"
    return "neutral"  # 429, 그 밖의 4xx, 분류 못 한 오류


# ---------------------------------------------------------------------------
# 런 상태 (프로세스 전역)
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_run_label: Optional[str] = None
_run_stop: Optional[LLMRunStop] = None
_consecutive_failures = 0
_ledgers: Dict[str, SpendLedger] = {}
_pricing_cache: Optional[Tuple[float, PriceTable]] = None


def _default_run_label() -> str:
    return f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"


def current_run_id() -> str:
    """런 상한이 묶는 단위. LLM_RUN_ID가 있으면 그 값(여러 프로세스가 한 상한을 나눠 쓴다)."""
    global _run_label
    env = os.getenv("LLM_RUN_ID", "").strip()
    if env:
        return env
    with _state_lock:
        if _run_label is None:
            _run_label = _default_run_label()
        return _run_label


def begin_run(label: Optional[str] = None) -> str:
    """새 런을 시작한다: 런 상한의 범위를 바꾸고, 중단 래치와 브레이커 카운트를 지운다."""
    global _run_label, _run_stop, _consecutive_failures
    with _state_lock:
        _run_label = label or _default_run_label()
        _run_stop = None
        _consecutive_failures = 0
    return current_run_id()


def run_stop() -> Optional[LLMRunStop]:
    """이번 런이 멈췄으면 그 사유(예외 인스턴스), 아니면 None."""
    with _state_lock:
        return _run_stop


def raise_if_run_stopped() -> None:
    stop = run_stop()
    if stop is not None:
        raise stop.again()


def reset_run_state() -> None:
    """테스트 전용: 런 상태와 캐시(원장 객체, 단가표)를 모두 지운다."""
    global _run_label, _run_stop, _consecutive_failures, _pricing_cache
    with _state_lock:
        _run_label = None
        _run_stop = None
        _consecutive_failures = 0
        _ledgers.clear()
        _pricing_cache = None


def _latch(stop: LLMRunStop) -> None:
    global _run_stop
    with _state_lock:
        if _run_stop is None:
            _run_stop = stop


def latch_run_stop(stop: LLMRunStop) -> None:
    """런을 멈춘 것으로 표시한다(이미 멈췄으면 첫 사유를 유지). 가드 밖에서 중단을 받은 쪽
    (Stage5의 클러스터 루프)이 다른 워커에게 알릴 때 쓴다."""
    _latch(stop)


def _ledger_for(cfg: BudgetConfig) -> SpendLedger:
    with _state_lock:
        ledger = _ledgers.get(cfg.ledger_path)
        if ledger is None:
            ledger = _ledgers[cfg.ledger_path] = SpendLedger(cfg.ledger_path)
        ledger.reservation_ttl_s = cfg.reservation_ttl_s
        return ledger


def _pricing() -> PriceTable:
    """단가표. 파일이 바뀌면 다시 읽는다(실행 중 단가 수정이 다음 호출부터 반영되게)."""
    global _pricing_cache
    mtime = os.stat(DEFAULT_PRICING_PATH).st_mtime
    with _state_lock:
        if _pricing_cache is None or _pricing_cache[0] != mtime:
            _pricing_cache = (mtime, load_pricing())
        return _pricing_cache[1]


# ---------------------------------------------------------------------------
# 킬 스위치 연동
# ---------------------------------------------------------------------------

def engage_kill_switch(reason: Dict[str, Any]) -> Optional[str]:
    """킬 스위치 파일을 만든다. 이미 있으면 그대로 둔다(먼저 켠 쪽의 사유를 지우지 않는다).

    만든(또는 이미 있던) 파일 경로를 돌려주고, 만들 수 없으면 None이다 - 그래도 이 프로세스의
    호출은 원장이 막고, 같은 원장을 쓰는 다른 프로세스도 같은 상한에서 막힌다.
    """
    path = Settings.LLM_KILL_SWITCH_FILE
    if not path:
        return None
    payload = {"engaged_by": "llm_spend_cap", **reason,
               "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pid": os.getpid()}
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError as e:  # 부모가 파일이거나(FileExistsError 포함) 읽기 전용 마운트
        logger.error("LLM 킬 스위치 디렉터리를 만들지 못했습니다 (%s): %s", path, e)
        return None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return path
    except OSError as e:
        logger.error("LLM 킬 스위치 파일을 만들지 못했습니다 (%s): %s", path, e)
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    logger.error("LLM 지출 상한(%s)에 닿아 킬 스위치를 켰습니다: %s", reason.get("scope"), path)
    return path


# ---------------------------------------------------------------------------
# 가드
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Price:
    input_per_1m: Decimal
    output_per_1m: Decimal
    source: str  # "table" | "fallback_override"


class Attempt:
    """예약 하나. 네트워크 요청이 끝나면 settle로 닫는다."""

    def __init__(self, guard: "BudgetGuard", cfg: BudgetConfig, ledger: SpendLedger,
                 reservation: Reservation, price: _Price):
        self._guard = guard
        self._cfg = cfg
        self._ledger = ledger
        self._price = price
        self._settled = False
        self.reservation = reservation

    def settle(self, outcome: str, usage: Optional[LLMUsage] = None) -> None:
        """시도 결과를 원장에 정산하고 브레이커에 알린다. 두 번째 호출부터는 아무것도 하지 않는다.

        이 결과로 서킷이 열리면 LLMCircuitOpen을 낸다(재시도 루프가 백오프를 기다리지 않고 멈추게).
        그 밖의 예외는 내지 않는다 - 정산 중 오류가 호출부의 재시도 분기로 들어가 요청을 한 번 더
        보내게 되면 안 된다. 정산을 못 쓰면 예약이 열린 채 남아 예약액으로 계속 잡힌다(상한 쪽).
        """
        if self._settled:
            return
        self._settled = True
        key = self.reservation.key
        try:
            self._write_settlement(outcome, usage)
        except Exception as e:  # noqa: BLE001
            logger.error("%s/%s: 지출 정산을 원장에 쓰지 못했습니다(예약 $%.6f은 열린 채 남습니다): %s",
                         key.provider, key.model, usd(self.reservation.nusd), e)
        self._guard._note_outcome(outcome, self._cfg, key)

    def _write_settlement(self, outcome: str, usage: Optional[LLMUsage]) -> None:
        key = self.reservation.key
        basis = billing_basis(outcome, key.provider, usage)
        extra: Dict[str, Any] = {}
        if basis == "actual":
            tokens_in, tokens_out = billable_tokens(usage)
            nusd = token_cost_nusd(tokens_in, self._price.input_per_1m) + token_cost_nusd(
                tokens_out, self._price.output_per_1m)
            extra = {
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens, "cached_tokens": usage.cached_tokens,
                "total_tokens": usage.total_tokens, "billable_output_tokens": tokens_out,
            }
            if nusd > self.reservation.nusd:
                logger.warning(
                    "%s/%s: 실제 비용이 예약을 넘었습니다 ($%.6f > $%.6f) - 토큰 추정이나 max_tokens 가정을 확인하세요",
                    key.provider, key.model, usd(nusd), usd(self.reservation.nusd),
                )
        elif basis == "not_billed":
            nusd = 0
        else:
            nusd = self.reservation.nusd
        self._ledger.settle(self.reservation, nusd=nusd, outcome=outcome, basis=basis, extra=extra)

    def ensure_settled(self) -> None:
        """어느 분기도 정산하지 않고 빠져나온 경우(예상 못 한 예외, Ctrl-C)의 안전망."""
        self.settle("unsettled")


class BudgetGuard:
    def ensure_run_active(self, provider: str, model: str, purpose: str) -> None:
        """이미 멈춘 런이면 같은 중단을 다시 낸다(원장·네트워크를 건드리지 않는다)."""
        stop = run_stop()
        if stop is not None:
            self._record_block(stop, provider, model, purpose)
            raise stop.again()

    def begin_attempt(self, *, provider: str, model: str, purpose: str, messages: List[Dict[str, Any]],
                      schema: Any = None, max_tokens: Optional[int]) -> Attempt:
        """요청 한 번의 최악 비용을 예약한다. 상한을 넘거나 지출을 확인할 수 없으면 예외를 낸다."""
        self.ensure_run_active(provider, model, purpose)
        try:
            cfg = BudgetConfig.from_env()
            if max_tokens is None or int(max_tokens) <= 0:
                raise BudgetConfigError("max_tokens 없이 보낸 요청은 최악 비용을 계산할 수 없습니다")
            now = time.time()
            day = cfg.day_of(now)
            price = self._price(cfg, model, day)
            est_input = estimate_prompt_tokens(messages, schema, bytes_per_token=cfg.bytes_per_token)
            nusd = token_cost_nusd(est_input, price.input_per_1m) + token_cost_nusd(int(max_tokens), price.output_per_1m)
            key = CallKey(run_id=current_run_id(), day=day, provider=provider, model=model,
                          role=role_for_purpose(purpose), purpose=purpose)
            ledger = _ledger_for(cfg)
            result = ledger.reserve(caps=cfg.caps, key=key, nusd=nusd, extra={
                "est_input_tokens": est_input, "max_output_tokens": int(max_tokens),
                "price_in_per_1m": str(price.input_per_1m), "price_out_per_1m": str(price.output_per_1m),
                "price_source": price.source,
            })
        except LLMRunStop as stop:
            self._stop(stop, provider, model, purpose)
        except Exception as e:  # noqa: BLE001 - 설정·단가표·원장의 어떤 실패든 "허용"이 되면 안 된다
            self._stop(LLMBudgetUnavailable(
                f"LLM 지출을 확인할 수 없어 호출을 거부합니다: {e}", reason=f"{type(e).__name__}: {e}",
            ), provider, model, purpose)

        if isinstance(result, Refusal):
            self._stop(self._exceeded(result, cfg, key), provider, model, purpose)
        return Attempt(self, cfg, ledger, result, price)

    # ------------------------------------------------------------------ 내부

    @staticmethod
    def _price(cfg: BudgetConfig, model: str, day: str) -> _Price:
        try:
            entry = _pricing().price(model, on=datetime.strptime(day, "%Y-%m-%d").date())
        except KeyError as e:
            if cfg.fallback_price is None:
                raise LLMUnpricedModel(
                    f"단가표(config/llm_pricing.yaml)에 없는 모델이라 호출을 거부합니다: {model} "
                    "(단가를 추가하거나 LLM_BUDGET_FALLBACK_INPUT_PER_1M/OUTPUT_PER_1M을 명시)",
                    model=model, reason=str(e.args[0]) if e.args else "unpriced",
                ) from None
            return _Price(cfg.fallback_price[0], cfg.fallback_price[1], "fallback_override")
        return _Price(Decimal(str(entry.input_per_1m)), Decimal(str(entry.output_per_1m)), "table")

    @staticmethod
    def _exceeded(refusal: Refusal, cfg: BudgetConfig, key: CallKey) -> LLMBudgetExceeded:
        details: Dict[str, Any] = {
            "scope": refusal.scope,
            "cap_usd": usd(refusal.cap_nusd),
            "spent_usd": usd(refusal.committed_nusd),
            "reserved_usd": usd(refusal.open_nusd),
            "requested_usd": usd(refusal.requested_nusd),
            "run_id": key.run_id, "day": key.day, "model": key.model, "purpose": key.purpose,
            "ledger": cfg.ledger_path,
        }
        if refusal.scope in ("day", "total"):
            details["kill_switch_file"] = engage_kill_switch(
                {k: details[k] for k in ("scope", "day", "cap_usd", "spent_usd", "reserved_usd",
                                         "requested_usd", "run_id", "ledger")}
            )
        return LLMBudgetExceeded(
            f"LLM 지출 상한({refusal.scope}) ${usd(refusal.cap_nusd):.4f}: 정산 ${usd(refusal.committed_nusd):.6f}"
            f" + 예약 ${usd(refusal.open_nusd):.6f} + 이번 요청 최악 ${usd(refusal.requested_nusd):.6f}",
            **details,
        )

    def _stop(self, stop: LLMRunStop, provider: str, model: str, purpose: str):
        _latch(stop)
        self._record_block(stop, provider, model, purpose)
        logger.error("%s/%s: LLM 런 중단 (%s, purpose=%s): %s", provider, model, stop.code, purpose, stop)
        raise stop

    @staticmethod
    def _record_block(stop: LLMRunStop, provider: str, model: str, purpose: str) -> None:
        get_metrics_collector().record_call(
            purpose=purpose, input_tokens=0, output_tokens=0, latency_seconds=0.0, success=False,
            provider=provider, model=model, error_type=stop.code,
        )

    def _note_outcome(self, outcome: str, cfg: BudgetConfig, key: CallKey) -> None:
        global _consecutive_failures
        effect = breaker_effect(outcome)
        with _state_lock:
            if effect == "healthy":
                _consecutive_failures = 0
                return
            if effect == "neutral":
                return
            if effect == "failure":
                _consecutive_failures += 1
                if cfg.breaker_threshold <= 0 or _consecutive_failures < cfg.breaker_threshold:
                    return
            count = _consecutive_failures
        status = _http_status(outcome)
        if effect == "fatal":
            message = f"HTTP {status}: 결제가 필요한 상태라 런을 멈춥니다(선불 잔액 확인)"
        else:
            message = f"연속 인프라 실패 {count}회({outcome})로 런을 멈춥니다"
        self._stop(LLMCircuitOpen(
            message, last_outcome=outcome, consecutive_failures=count, threshold=cfg.breaker_threshold,
            http_status=status, run_id=key.run_id,
        ), key.provider, key.model, key.purpose)


_guard = BudgetGuard()


def get_budget_guard() -> BudgetGuard:
    return _guard


# ---------------------------------------------------------------------------
# 조회 (잡 통계·사전 점검용)
# ---------------------------------------------------------------------------

def spend_snapshot() -> Dict[str, Any]:
    """현재 런·오늘·전체의 사용액(정산 + 열린 예약)과 상한. 읽기만 한다. 실패해도 예외를 내지 않는다."""
    try:
        cfg = BudgetConfig.from_env()
        run_id = current_run_id()
        day = cfg.day_of(time.time())
        totals = _ledger_for(cfg).totals(run_id=run_id, day=day)
    except (BudgetConfigError, LedgerError, OSError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {
        "run_id": run_id,
        "day": day,
        "ledger": cfg.ledger_path,
        "run_usd": usd(totals.used("run")),
        "day_usd": usd(totals.used("day")),
        "total_usd": usd(totals.used("total")),
        "caps_usd": {"run": usd(cfg.caps.run_nusd), "day": usd(cfg.caps.day_nusd), "total": usd(cfg.caps.total_nusd)},
        "open_reservations": totals.open_count,
        "corrupt_ledger_lines": totals.corrupt_lines,
    }


def exhausted_scope() -> Optional[Dict[str, Any]]:
    """일·전체 상한이 이미 찼으면(1 nUSD도 더 못 쓰면) 그 범위와 수치를, 아니면 None.

    잡이 클러스터링 같은 준비 작업을 하기 전에 미리 확인하는 용도다. 런 상한은 보지 않는다
    (잡은 새 런으로 시작한다). 설정·원장 오류는 여기서 판단하지 않는다 - 첫 호출이 거부한다.
    """
    try:
        cfg = BudgetConfig.from_env()
        day = cfg.day_of(time.time())
        totals = _ledger_for(cfg).totals(run_id=current_run_id(), day=day)
    except (BudgetConfigError, LedgerError, OSError, ValueError):
        return None
    for scope in ("total", "day"):
        cap = cfg.caps.for_scope(scope)
        if totals.used(scope) >= cap:
            return {"scope": scope, "cap_usd": usd(cap), "used_usd": usd(totals.used(scope)),
                    "day": day, "ledger": cfg.ledger_path}
    return None
