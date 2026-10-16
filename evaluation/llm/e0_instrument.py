"""E0 계측과 예산 가드 - 운영 생성 경로를 고치지 않고 두 층에서 기록하고, HTTP 시도마다 등록한 상한을 건다 (ADR 0009 A8.4·A8.5).

두 층(A8.4):
- **HTTP 시도 단위**(`http_attempts.jsonl`): 어댑터가 만든 OpenAI 클라이언트의 httpx 요청/응답 훅. 보낸 요청마다 1행.
  응답 없이 끝난 시도(타임아웃·연결 끊김)는 응답 훅이 불리지 않으므로, 같은 클라이언트의 전송 계층을 감싼 관찰자가 예외의
  종류만 보고 그 시도를 닫는다(예외는 그대로 다시 낸다 - 재시도·타임아웃은 운영 그대로).
- **complete() 단위**(`calls.jsonl`): 생성기·평가기·문체 변환기가 부르는 `get_client` 자리에 넣은 래퍼. 호출마다 1행.
- 귀속: 워커 3개가 동시에 돈다. 클러스터·호출 문맥은 contextvars에 둔다(워커 스레드마다 따로이고, 같은 스레드에서 불리는
  동기 httpx 훅이 그 값을 본다 - A8.4의 "스레드 로컬").

예산(A8.5):
- 원장(`ledger.jsonl`, run 디렉터리마다)은 추가만 한다. 시도를 보내기 전에 최악 비용으로 예약(reserve) 줄을, 끝나면 정산
  (settle) 줄을 쓴다. 누적 상한은 E0 디렉터리의 모든 run 원장의 합이다(run을 넘어 이어진다). 프로세스가 죽어 정산 없이 남은
  예약은 이어서 돌 때 최악 비용으로 정산한다(과금됐는지 알 수 없다).
- 가드: `원장 합 + 진행 중 최악 비용 합 + 이 요청의 최악 비용`이 유효 실행 상한($1.00) 또는 누적 상한($1.50)을 넘으면 보내지
  않고 중단 플래그(budget)를 세운다. 최악 비용 = prompt 문자 × 1.5 토큰/자 × 입력 단가 + max_tokens × 출력 단가.
- 401/402/403/404, 킬 스위치, 키 없음은 중단 플래그(infra)를 세운다. 플래그가 선 뒤에는 어떤 요청도 보내지 않는다.
- 클라이언트 안의 지출 가드(ADR 0035)와 같은 경로로 멈춘다: 중단은 `core.llm.budget.LLMRunStop`의 하위 예외이고 같은 래치
  (`latch_run_stop`)를 세운다. 그래서 다른 워커는 다음 complete()에서 원장·네트워크에 닿지 않고 멈추고,
  `run_clusters_bounded`는 남은 클러스터를 시작하지 않는다.

기록에는 프롬프트·응답 텍스트를 넣지 않는다. 오류도 사유 코드로만 남긴다(스키마 검증 오류 메시지에는 모델 응답이 섞인다).
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from core.llm.budget import LLMBudgetRefusal, LLMCircuitOpen, LLMRunStop, latch_run_stop

from evaluation.llm import e0_registered as reg

HTTP_FILE, CALLS_FILE, NODES_FILE, CLUSTERS_FILE, LEDGER_FILE = (
    "http_attempts.jsonl", "calls.jsonl", "nodes.jsonl", "clusters.jsonl", "ledger.jsonl")
EVENTS_FILE = "events.jsonl"
ALLOWED_CATEGORIES = ("정치", "경제", "사회", "세계", "IT/과학", "생활/문화", "스포츠")


class E0BudgetStop(LLMBudgetRefusal):
    """E0 예산 가드(A8.5)가 요청을 보내기 전에 거부했다."""

    code = "e0_budget_refused"


class E0InfraStop(LLMRunStop):
    """인프라 실패(401/402/403/404·킬 스위치·키 없음)로 E0을 멈췄다(A8.5)."""

    code = "e0_infra_stop"


def stop_kind(stop: BaseException) -> str:
    """중단 예외 → "budget" | "infra". 클라이언트 안의 상한 거부도 예산이고, 서킷브레이커(402 포함)는 인프라다."""
    if isinstance(stop, LLMBudgetRefusal):
        return "budget"
    if isinstance(stop, (LLMCircuitOpen, E0InfraStop)):
        return "infra"
    return "infra"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _round(value: float) -> float:
    return float(round(value, 9))


# ---------------------------------------------------------------- 원장


class Ledger:
    """run 디렉터리의 ledger.jsonl(추가만). 합은 정산액 + 정산되지 않은 예약의 최악 비용."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: Dict[str, Any]) -> None:
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with self._lock:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def read(path: Path) -> List[Dict[str, Any]]:
        if not Path(path).exists():
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    @staticmethod
    def totals(path: Path) -> Dict[str, Any]:
        rows = Ledger.read(path)
        settled = {r["attempt_id"]: r for r in rows if r["type"] == "settle"}
        reserved = {r["attempt_id"]: r for r in rows if r["type"] == "reserve"}
        open_ids = sorted(set(reserved) - set(settled))
        return {
            "settled_usd": _round(sum(r["cost_usd"] for r in settled.values())),
            "open_worst_usd": _round(sum(reserved[a]["worst_usd"] for a in open_ids)),
            "open_ids": open_ids,
            "settled": len(settled),
        }

    @staticmethod
    def sum_usd(path: Path) -> float:
        t = Ledger.totals(path)
        return _round(t["settled_usd"] + t["open_worst_usd"])


def cumulative_usd(e0_root: Path, *, exclude: Optional[Path] = None) -> float:
    """E0 디렉터리의 모든 run 원장 합(누적 상한용). exclude는 지금 run(따로 센다)."""
    total = 0.0
    for path in sorted(Path(e0_root).glob(f"*/{LEDGER_FILE}")):
        if exclude is not None and path.parent.resolve() == Path(exclude).resolve():
            continue
        total += Ledger.sum_usd(path)
    return _round(total)


class Prices:
    """config/llm_pricing.yaml의 실행일 단가(USD/1M)."""

    def __init__(self, table=None, on=None):
        from core.llm.pricing import load_pricing

        self.table = table or load_pricing()
        self.on = on

    def __call__(self, model: str) -> Tuple[float, float]:
        entry = self.table.price(model, on=self.on() if callable(self.on) else self.on)
        return float(entry.input_per_1m), float(entry.output_per_1m)


def worst_case_usd(prompt_chars: int, max_tokens: int, price: Tuple[float, float]) -> float:
    """A8.5 (b): prompt 문자 × 1.5 토큰/자 × 입력 단가 + max_tokens × 출력 단가."""
    tokens_in = prompt_chars * reg.BUDGET["worst_case_tokens_per_char"]
    return _round((tokens_in * price[0] + max_tokens * price[1]) / 1_000_000)


def observed_usd(prompt: int, completion: int, hidden: int, price: Tuple[float, float]) -> float:
    """A8.3 #5: prompt × 입력 단가 + (completion + hidden) × 출력 단가."""
    return _round((prompt * price[0] + (completion + hidden) * price[1]) / 1_000_000)


# ---------------------------------------------------------------- 문맥


@dataclass
class ClusterContext:
    item_id: Optional[str]
    cluster_id: Optional[int]
    run_k: Optional[int]
    split: str
    call_seq: int = 0
    counters: Dict[str, int] = field(default_factory=dict)  # purpose별 complete() 수
    gen_attempt: int = 0
    cluster_eval_attempt: int = 0
    tone_round: int = 0
    tone_local_fallbacks: int = 0
    tone_validation_calls: int = 0
    events: List[Dict[str, Any]] = field(default_factory=list)
    last_call: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # purpose -> 마지막 calls.jsonl 행
    calls: List[Dict[str, Any]] = field(default_factory=list)
    stopped: Optional[str] = None  # "budget" | "infra" - 이 클러스터의 요청이 거부·인프라 실패를 받았다


@dataclass
class CallContext:
    cluster: ClusterContext
    call_seq: int
    purpose: str
    provider: str
    model: str
    graph_attempt: Optional[int]
    attempts: List[Dict[str, Any]] = field(default_factory=list)


_cluster_var: contextvars.ContextVar[Optional[ClusterContext]] = contextvars.ContextVar("e0_cluster", default=None)
_call_var: contextvars.ContextVar[Optional[CallContext]] = contextvars.ContextVar("e0_call", default=None)


def current_cluster() -> Optional[ClusterContext]:
    return _cluster_var.get()


# ---------------------------------------------------------------- 기록기


@dataclass
class _Attempt:
    attempt_id: str
    call: Optional[CallContext]
    attempt_seq: int
    model: str
    provider: str
    prompt_chars: int
    max_tokens: int
    worst_usd: float
    price: Tuple[float, float]
    started: float
    started_at: str


def _prompt_chars(messages: Any) -> int:
    total = 0
    for m in messages or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(part.get("text") or "") for part in content if isinstance(part, dict))
    return total


class Recorder:
    """기록 파일들과 예산 가드. 한 run 디렉터리당 하나."""

    def __init__(self, run_dir: Path, *, run_name: str, e0_root: Path, prices: Callable[[str], Tuple[float, float]],
                 cap_run_usd: float = reg.BUDGET["cap_usd_valid_run"],
                 cap_cumulative_usd: float = reg.BUDGET["cap_usd_e0_cumulative"],
                 clock: Callable[[], float] = time.time, on_stop: Optional[Callable[[str, Dict[str, Any]], None]] = None):
        self.dir = Path(run_dir)
        self.run_name = run_name
        self.prices = prices
        self.cap_run = float(cap_run_usd)
        self.cap_cumulative = float(cap_cumulative_usd)
        self.clock = clock
        self.ledger = Ledger(self.dir / LEDGER_FILE)
        self._lock = threading.RLock()
        self._inflight: Dict[str, _Attempt] = {}
        self._by_request: Dict[int, _Attempt] = {}
        self._seq = 0
        self.stop: Optional[Dict[str, Any]] = None
        self.instrumentation_errors: List[str] = []
        self.refusals: List[Dict[str, Any]] = []
        self.on_stop = on_stop
        self.e0_root = Path(e0_root)
        self._prior_runs_usd = cumulative_usd(self.e0_root, exclude=self.dir)
        self._settle_crashed_reservations()
        self._run_usd = Ledger.sum_usd(self.dir / LEDGER_FILE)
        existing = Ledger.read(self.dir / LEDGER_FILE)
        self._seq = len({r["attempt_id"] for r in existing})

    # -- 파일
    def _append(self, name: str, record: Dict[str, Any]) -> None:
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        with self._lock:
            fd = os.open(self.dir / name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def read(self, name: str) -> List[Dict[str, Any]]:
        return Ledger.read(self.dir / name)

    def event(self, kind: str, **fields: Any) -> None:
        self._append(EVENTS_FILE, {"kind": kind, "at": _now(), **fields})

    # -- 원장
    def _settle_crashed_reservations(self) -> None:
        """이전 프로세스가 예약만 쓰고 죽었으면 그 시도를 최악 비용으로 정산하고 http 행을 남긴다(A8.5 재개)."""
        totals = Ledger.totals(self.dir / LEDGER_FILE)
        if not totals["open_ids"]:
            return
        reserved = {r["attempt_id"]: r for r in Ledger.read(self.dir / LEDGER_FILE) if r["type"] == "reserve"}
        for attempt_id in totals["open_ids"]:
            r = reserved[attempt_id]
            self.ledger.append({"type": "settle", "attempt_id": attempt_id, "cost_usd": r["worst_usd"],
                                "cost_kind": "unobserved_upper_bound", "reason": "process_died", "at": _now()})
            self._append(HTTP_FILE, {**{k: r.get(k) for k in reg.HTTP_ATTEMPT_FIELDS}, "status": None,
                                     "transport_error": "process_died", "cost_usd": r["worst_usd"],
                                     "cost_kind": "unobserved_upper_bound", "latency_s": None})

    def spent(self) -> Dict[str, float]:
        with self._lock:
            inflight = sum(a.worst_usd for a in self._inflight.values())
            return {"run_usd": _round(self._run_usd), "inflight_worst_usd": _round(inflight),
                    "cumulative_usd": _round(self._prior_runs_usd + self._run_usd)}

    def set_stop(self, reason: str, detail: Dict[str, Any], stop_exc: Optional[LLMRunStop] = None) -> None:
        with self._lock:
            if self.stop is not None:
                return
            self.stop = {"reason": reason, **detail, "at": _now()}
        self.event("stop", reason=reason, **detail)
        if stop_exc is not None:
            latch_run_stop(stop_exc)
        if self.on_stop:
            self.on_stop(reason, detail)

    def _raise_for_stop(self) -> None:
        stop = self.stop or {}
        exc = E0BudgetStop if stop.get("reason") == "budget" else E0InfraStop
        raise exc(f"E0 중단 플래그({stop.get('reason')}) - 요청을 보내지 않는다", **{k: v for k, v in stop.items()
                                                                        if k in ("reason", "cap", "detail")})

    # -- HTTP 훅
    def on_request(self, request) -> None:
        call = _call_var.get()
        cluster = call.cluster if call else _cluster_var.get()
        if self.stop is not None:
            if cluster is not None and cluster.stopped is None:
                cluster.stopped = self.stop["reason"]
            self.refusals.append({"at": _now(), "item_id": getattr(cluster, "item_id", None),
                                  "reason": self.stop["reason"], "after_stop": True})
            self._raise_for_stop()
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        model = str(body.get("model") or (call.model if call else ""))
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 0)
        prompt_chars = _prompt_chars(body.get("messages"))
        try:
            price = self.prices(model)
        except Exception as e:  # noqa: BLE001 - 값을 모르면 보내지 않는다(fail closed)
            detail = {"detail": f"단가를 알 수 없는 모델({type(e).__name__})", "model": model}
            self.set_stop("budget", detail, E0BudgetStop("E0 가드: 단가 없음", **detail))
            self._raise_for_stop()
        worst = worst_case_usd(prompt_chars, max_tokens, price)
        with self._lock:
            inflight = sum(a.worst_usd for a in self._inflight.values())
            run_total = self._run_usd + inflight + worst
            cum_total = self._prior_runs_usd + self._run_usd + inflight + worst
            refused = None
            if run_total > self.cap_run + 1e-12:
                refused = {"cap": "valid_run", "cap_usd": self.cap_run, "would_be_usd": _round(run_total)}
            elif cum_total > self.cap_cumulative + 1e-12:
                refused = {"cap": "e0_cumulative", "cap_usd": self.cap_cumulative, "would_be_usd": _round(cum_total)}
            if refused is None:
                self._seq += 1
                attempt_id = f"a{self._seq:06d}"
                if call is not None:
                    attempt_seq = len(call.attempts) + 1
                else:
                    attempt_seq = 1
                attempt = _Attempt(attempt_id, call, attempt_seq, model, getattr(call, "provider", None) or "",
                                   prompt_chars, max_tokens, worst, price, self.clock(), _now())
                self._inflight[attempt_id] = attempt
                self._by_request[id(request)] = attempt
        if refused is not None:
            if cluster is not None:
                cluster.stopped = "budget"
            self.refusals.append({"at": _now(), "item_id": getattr(cluster, "item_id", None), **refused})
            detail = {**refused, "item_id": getattr(cluster, "item_id", None)}
            stop = E0BudgetStop(f"E0 예산 가드: {refused['cap']} 상한 ${refused['cap_usd']:.2f}를 넘는다", **detail)
            self.set_stop("budget", detail, stop)
            raise stop
        self.ledger.append({"type": "reserve", "attempt_id": attempt_id, "worst_usd": worst, "at": attempt.started_at,
                            **self._attribution(attempt), "prompt_chars": prompt_chars, "max_tokens": max_tokens,
                            "model_requested": model, "started_at": attempt.started_at})
        if call is not None:
            call.attempts.append({"attempt_id": attempt_id})

    def _attribution(self, attempt: _Attempt) -> Dict[str, Any]:
        call = attempt.call
        cluster = call.cluster if call else _cluster_var.get()
        return {
            "run": self.run_name,
            "cluster_id": getattr(cluster, "cluster_id", None),
            "run_k": getattr(cluster, "run_k", None),
            "graph_attempt": getattr(call, "graph_attempt", None),
            "purpose": getattr(call, "purpose", None),
            "call_seq": getattr(call, "call_seq", None),
            "attempt_seq": attempt.attempt_seq,
            "provider": attempt.provider or None,
        }

    def _close(self, attempt: _Attempt, *, status: Optional[int], transport_error: Optional[str],
               payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        usage = (payload or {}).get("usage") if isinstance(payload, dict) else None
        prompt = completion = total = hidden = reasoning = None
        if isinstance(usage, dict):
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            total = int(usage.get("total_tokens") or (prompt + completion))
            hidden = max(0, total - prompt - completion)
            details = usage.get("completion_tokens_details") or {}
            reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
        if status is not None and 200 <= status < 300 and usage:
            cost, kind = observed_usd(prompt, completion, hidden, attempt.price), "observed"
        elif status is not None and status >= 400:
            cost, kind = 0.0, "error_response"
        else:
            cost, kind = attempt.worst_usd, "unobserved_upper_bound"
        choices = (payload or {}).get("choices") if isinstance(payload, dict) else None
        finish = choices[0].get("finish_reason") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        row = {
            **self._attribution(attempt),
            "model_requested": attempt.model,
            "model_responded": (payload or {}).get("model") if isinstance(payload, dict) else None,
            "system_fingerprint": (payload or {}).get("system_fingerprint") if isinstance(payload, dict) else None,
            "status": status, "transport_error": transport_error, "finish_reason": finish,
            "prompt_chars": attempt.prompt_chars, "max_tokens": attempt.max_tokens,
            "prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total,
            "reasoning_tokens_reported": reasoning, "hidden_tokens": hidden,
            "cost_usd": cost, "cost_kind": kind, "latency_s": round(self.clock() - attempt.started, 3),
            "started_at": attempt.started_at,
        }
        with self._lock:
            self._inflight.pop(attempt.attempt_id, None)
            self._run_usd = _round(self._run_usd + cost)
        self.ledger.append({"type": "settle", "attempt_id": attempt.attempt_id, "cost_usd": cost, "cost_kind": kind,
                            "status": status, "at": _now()})
        self._append(HTTP_FILE, row)
        if attempt.call is not None:
            for a in attempt.call.attempts:
                if a["attempt_id"] == attempt.attempt_id:
                    a.update({"status": status, "cost_usd": cost, "transport_error": transport_error,
                              "finish_reason": finish})
        if status in reg.INFRA_HTTP_STATUSES:
            cluster = attempt.call.cluster if attempt.call else None
            if cluster is not None:
                cluster.stopped = "infra"
                cluster.events.append({"type": f"http_{status}"})
            detail = {"detail": f"http_{status}", "item_id": getattr(cluster, "item_id", None)}
            self.set_stop("infra", detail, E0InfraStop(f"E0 인프라 중단: HTTP {status}", **detail))
        return row

    def on_response(self, response) -> None:
        try:
            with self._lock:
                attempt = self._by_request.pop(id(response.request), None)
            if attempt is None:
                self.instrumentation_errors.append("응답 훅: 짝이 되는 요청 기록이 없다")
                return
            payload = None
            try:
                response.read()
                payload = response.json()
            except Exception:  # noqa: BLE001 - 본문이 json이 아니면 usage 없음으로 친다
                payload = None
            self._close(attempt, status=int(response.status_code), transport_error=None, payload=payload)
        except Exception as e:  # noqa: BLE001 - 계측 오류가 운영 경로를 바꾸면 안 된다
            self.instrumentation_errors.append(f"응답 훅 오류: {type(e).__name__}")

    def on_transport_error(self, request, error: BaseException) -> None:
        try:
            with self._lock:
                attempt = self._by_request.pop(id(request), None)
            if attempt is None:
                return
            name = type(error).__name__.lower()
            kind = "timeout" if "timeout" in name else "connection" if isinstance(error, Exception) else "interrupted"
            self._close(attempt, status=None, transport_error=kind, payload=None)
        except Exception as e:  # noqa: BLE001
            self.instrumentation_errors.append(f"전송 관찰 오류: {type(e).__name__}")

    def close_dangling(self, call: CallContext) -> None:
        """complete()가 끝났는데 닫히지 않은 시도(관찰되지 않은 예외)를 최악 비용으로 닫는다."""
        with self._lock:
            dangling = [a for a in self._inflight.values() if a.call is call]
            for a in dangling:
                for key, value in list(self._by_request.items()):
                    if value is a:
                        self._by_request.pop(key, None)
        for a in dangling:
            self.instrumentation_errors.append("닫히지 않은 HTTP 시도")
            self._close(a, status=None, transport_error="unobserved", payload=None)

    # -- complete() 래퍼가 부르는 것
    def begin_call(self, *, purpose: str, provider: str, model: str) -> CallContext:
        cluster = _cluster_var.get()
        if cluster is None:
            cluster = ClusterContext(item_id=None, cluster_id=None, run_k=None, split="unattributed")
            self.instrumentation_errors.append(f"클러스터 문맥 없이 불린 complete()({purpose})")
        cluster.call_seq += 1
        cluster.counters[purpose] = cluster.counters.get(purpose, 0) + 1
        if purpose == "newsletter_content_gen":
            cluster.gen_attempt += 1
        if purpose == "cluster_eval":
            cluster.cluster_eval_attempt += 1
        if purpose == "tone_convert":
            cluster.tone_validation_calls += 1
        graph_attempt = {
            "cluster_eval": cluster.cluster_eval_attempt,
            "tone_convert": cluster.tone_round or None,
        }.get(purpose, cluster.gen_attempt or None)
        return CallContext(cluster=cluster, call_seq=cluster.call_seq, purpose=purpose, provider=provider,
                           model=model, graph_attempt=graph_attempt)

    def record_call(self, call: CallContext, row: Dict[str, Any]) -> None:
        call.cluster.last_call[call.purpose] = row
        call.cluster.calls.append(row)
        self._append(CALLS_FILE, row)

    # -- 클러스터
    def start_cluster(self, item: Dict[str, Any], *, phase: str) -> ClusterContext:
        ctx = ClusterContext(item_id=item["item_id"], cluster_id=item.get("cluster_id"), run_k=item.get("run_k"),
                             split=item.get("split", phase))
        self._append(CLUSTERS_FILE, {"kind": "start", "item_id": ctx.item_id, "run_k": ctx.run_k,
                                     "cluster_id": ctx.cluster_id, "split": ctx.split, "phase": phase,
                                     "order": item.get("order"), "at": _now(), "t": self.clock()})
        return ctx

    def end_cluster(self, ctx: ClusterContext, row: Dict[str, Any]) -> Dict[str, Any]:
        record = {"kind": "end", "item_id": ctx.item_id, "run_k": ctx.run_k, "cluster_id": ctx.cluster_id,
                  "split": ctx.split, "at": _now(), "t": self.clock(), **row}
        self._append(CLUSTERS_FILE, record)
        return record

    def node(self, record: Dict[str, Any]) -> None:
        self._append(NODES_FILE, {"run": self.run_name, "at": _now(), **record})


# ---------------------------------------------------------------- complete() 래퍼


def classify_call_error(result: Any, attempts: Sequence[Dict[str, Any]]) -> Optional[str]:
    """LLMResult.error → 사유 코드. 원문(스키마 오류 메시지 등)은 기록하지 않는다."""
    error = getattr(result, "error", None)
    if error is None:
        return None
    if error in ("kill_switch", "length", "content_filter"):
        return error
    status = getattr(result, "http_status", None)
    if status in reg.INFRA_HTTP_STATUSES:
        return f"http_{status}"
    text = str(error)
    exhausted = text.startswith("call deadline") or text.startswith("max retries")
    if exhausted:
        last = attempts[-1] if attempts else {}
        last_status = last.get("status")
        if last_status is not None and 200 <= last_status < 300:
            return "schema" if not text.startswith("call deadline") else "deadline_schema"
        return "transport" if not text.startswith("call deadline") else "deadline_transport"
    if status is not None:
        return f"http_{status}"
    return "other"


class RecordingClient:
    """운영 클라이언트를 감싸 complete()마다 calls.jsonl에 1행을 쓴다. 인자·결과는 그대로 넘긴다."""

    def __init__(self, inner: Any, recorder: Recorder):
        self._inner = inner
        self._recorder = recorder
        self.provider = getattr(inner, "provider", "unknown")
        self.model = getattr(inner, "model", "unknown")

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def complete(self, messages, *, schema=None, purpose="unknown", temperature=0.2, max_tokens=4096):
        rec = self._recorder
        call = rec.begin_call(purpose=purpose, provider=self.provider, model=self.model)
        token = _call_var.set(call)
        started_at, t0 = _now(), time.time()
        result, stop = None, None
        try:
            result = self._inner.complete(messages, schema=schema, purpose=purpose, temperature=temperature,
                                          max_tokens=max_tokens)
            return result
        except LLMRunStop as e:
            stop = e
            if call.cluster.stopped is None:
                call.cluster.stopped = stop_kind(e)
            raise
        finally:
            _call_var.reset(token)
            rec.close_dangling(call)
            prompt_text = json.dumps(messages, ensure_ascii=False, sort_keys=True)
            parsed = getattr(result, "parsed", None)
            schema_failures = int(getattr(result, "schema_failures", 0) or 0)
            error = classify_call_error(result, call.attempts) if result is not None else (
                f"stop:{stop.code}" if stop is not None else "exception")
            responded = bool(parsed is not None or schema_failures > 0 or error in ("length", "content_filter"))
            keywords = invalid = None
            if purpose == "newsletter_meta_gen" and parsed is not None:
                keywords = len(getattr(parsed, "keywords", []) or [])
                invalid = sum(1 for c in (getattr(parsed, "categories", []) or []) if c not in ALLOWED_CATEGORIES)
            row = {
                "run": rec.run_name, "cluster_id": call.cluster.cluster_id, "run_k": call.cluster.run_k,
                "graph_attempt": call.graph_attempt, "purpose": purpose, "call_seq": call.call_seq,
                "provider": self.provider, "model": self.model,
                "prompt_sha12": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12],
                "prompt_chars": _prompt_chars(messages), "temperature": temperature, "max_tokens": max_tokens,
                "attempts": getattr(result, "attempts", None), "http_attempts": len(call.attempts),
                "schema_failures": schema_failures, "parsed": parsed is not None, "responded": responded,
                "first_try_schema_pass": bool(parsed is not None and schema_failures == 0),
                "error": error, "http_status": getattr(result, "http_status", None),
                "latency_s": round(float(getattr(result, "latency_s", 0.0) or 0.0), 3),
                "wall_s": round(time.time() - t0, 3),
                "cost_usd": _round(sum(float(a.get("cost_usd") or 0.0) for a in call.attempts)),
                "parsed_keyword_count": keywords, "parsed_invalid_category_count": invalid,
                "started_at": started_at,
            }
            rec.record_call(call, row)
            if error == "kill_switch":
                call.cluster.stopped = "infra"
                call.cluster.events.append({"type": "kill_switch"})
                detail = {"detail": "kill_switch", "item_id": call.cluster.item_id}
                rec.set_stop("infra", detail, E0InfraStop("E0 인프라 중단: 킬 스위치", **detail))


# ---------------------------------------------------------------- 운영 코드에 끼우기


class ObservingTransport:
    """httpx 전송 계층을 감싸 응답 없이 끝난 시도의 예외 종류만 기록한다. 예외는 그대로 다시 낸다."""

    def __init__(self, inner: Any, recorder: Recorder):
        self._inner = inner
        self._recorder = recorder

    def handle_request(self, request):
        try:
            return self._inner.handle_request(request)
        except BaseException as e:
            self._recorder.on_transport_error(request, e)
            raise

    def close(self) -> None:
        self._inner.close()

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def attach_http_instrumentation(llm_client: Any, recorder: Recorder) -> None:
    """운영 어댑터(OpenAICompatLLMClient)가 만든 OpenAI 클라이언트의 httpx 클라이언트에 훅과 관찰자를 단다.

    클라이언트를 다시 만들지 않는다 - 타임아웃·재시도(max_retries=0)·연결 설정은 운영이 만든 그대로다.
    """
    sdk = getattr(llm_client, "_client", None)
    http = getattr(sdk, "_client", None)
    if http is None or not hasattr(http, "event_hooks") or not hasattr(http, "_transport"):
        raise RuntimeError("어댑터의 OpenAI 클라이언트에 httpx 훅을 달 수 없다(SDK 구조가 바뀌었다)")
    if getattr(http, "_e0_instrumented", False):
        return
    hooks = http.event_hooks
    http.event_hooks = {"request": list(hooks.get("request", [])) + [recorder.on_request],
                        "response": list(hooks.get("response", [])) + [recorder.on_response]}
    http._transport = ObservingTransport(http._transport, recorder)
    mounts = getattr(http, "_mounts", {}) or {}
    for pattern, transport in list(mounts.items()):
        if transport is not None:
            mounts[pattern] = ObservingTransport(transport, recorder)
    http._e0_instrumented = True


CLIENT_MODULES = ("core.reconstruction.generator", "workflow.evaluators", "core.tone_converter")


def make_tracking_tone_converter(base_cls: type) -> type:
    """문체 변환기의 로컬 폴백(_fallback_convert) 사용과 변환 회차를 클러스터 문맥에 적는 서브클래스(A8.2 이탈 8)."""

    class TrackingToneConverter(base_cls):  # type: ignore[misc, valid-type]
        def convert(self, newsletter, feedback=None):
            ctx = _cluster_var.get()
            if ctx is not None:
                ctx.tone_round += 1
            return super().convert(newsletter, feedback=feedback)

        def _fallback_convert(self, original, last):
            ctx = _cluster_var.get()
            if ctx is not None:
                ctx.tone_local_fallbacks += 1
                ctx.events.append({"type": "tone_local_fallback", "tone_round": ctx.tone_round})
            return super()._fallback_convert(original, last)

    TrackingToneConverter.__name__ = "TrackingToneConverter"
    return TrackingToneConverter


class Instrumentation:
    """계측을 끼우고 되돌린다. 운영 함수 본문은 바꾸지 않고 이름 자리만 바꾼다(되돌릴 수 있게 원본을 들고 있다)."""

    def __init__(self, recorder: Recorder):
        self.recorder = recorder
        self._patches: List[Tuple[Any, str, Any]] = []
        self.clients: List[Any] = []

    def _patch(self, module: Any, name: str, value: Any) -> None:
        self._patches.append((module, name, getattr(module, name)))
        setattr(module, name, value)

    def install(self, roles: Iterable[str] = ("generator", "judge", "tone")) -> None:
        import importlib

        from core.llm.registry import get_client
        import workflow.nodes as nodes

        for role in roles:  # 레지스트리가 (provider, model)마다 하나씩 만든 운영 클라이언트에 훅을 단다
            client = get_client(role)
            if client not in self.clients:
                attach_http_instrumentation(client, self.recorder)
                self.clients.append(client)
        recorder = self.recorder

        def recording_get_client(role: str):
            return RecordingClient(get_client(role), recorder)

        for name in CLIENT_MODULES:
            self._patch(importlib.import_module(name), "get_client", recording_get_client)
        self._patch(nodes, "ToneConverter", make_tracking_tone_converter(nodes.ToneConverter))

    def uninstall(self) -> None:
        for module, name, original in reversed(self._patches):
            setattr(module, name, original)
        self._patches.clear()

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, *exc):
        self.uninstall()
        return False


def run_in_cluster(ctx: ClusterContext, fn: Callable[[], Any]) -> Any:
    token = _cluster_var.set(ctx)
    try:
        return fn()
    finally:
        _cluster_var.reset(token)
