"""LLM 지출 원장 CLI (docs/adr/0035).

  python -m core.llm.spend_cli summary [--days N] [--json]
      일·모델·역할별 호출 수, 토큰, 금액(USD와 가정 환율의 KRW), 상한 대비 사용액.
  python -m core.llm.spend_cli reset-day [YYYY-MM-DD] --yes [--note 사유] [--clear-kill-switch]
      그날의 일 상한 창을 비운다. 전체 상한과 지출 기록은 그대로다.
  python -m core.llm.spend_cli estimate --plan MODEL:CALLS:PROMPT:MAX_OUTPUT_TOKENS [--plan ...]
      계획한 호출의 최악 비용(가드가 예약할 금액)을 미리 계산하고 남은 상한과 비교한다.
      MODEL 자리에 generator/judge/tone을 쓰면 지금 설정된 모델로 바꾼다.
      PROMPT는 문자 수(한글 기준 글자당 3바이트로 환산)이고, 끝에 t를 붙이면 토큰 수다(예: 16000t).

ai_workspace/를 임포트 경로에 둔 상태로 실행한다(가상환경에 editable 설치했거나 ai_workspace/에서 실행).
원장 위치와 상한은 환경변수(LLM_SPEND_LEDGER_FILE, LLM_BUDGET_*_USD)를 따른다. --ledger로 다른 원장을 볼 수 있다.

금액은 단가표(config/llm_pricing.yaml)로 계산한 추정이다. 실제 청구액은 프로바이더 청구서가 기준이다.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from config.settings import Settings
from core.llm import budget
from core.llm.budget import (
    BASE_OVERHEAD_TOKENS,
    PER_MESSAGE_OVERHEAD_TOKENS,
    BudgetConfig,
    BudgetConfigError,
    LLMUnpricedModel,
    token_cost_nusd,
)
from core.llm.spend_ledger import LedgerError, SpendLedger, Totals, usd

PLAN_FORMAT = "MODEL:CALLS:PROMPT:MAX_OUTPUT_TOKENS"
# 문자 수로 준 프롬프트를 바이트로 바꿀 때의 기본 가정: 한글 1자 = UTF-8 3바이트.
DEFAULT_BYTES_PER_CHAR = 3.0
# estimate가 가정하는 요청당 메시지 수(시스템 + 사용자).
ESTIMATE_MESSAGES_PER_CALL = 2


# ---------------------------------------------------------------------------
# 집계
# ---------------------------------------------------------------------------

def summarize(events) -> Dict[str, Any]:
    """원장 이벤트를 (일, 모델, 역할) 행으로 모은다. 금액은 nUSD 정수로 돌려준다(표시는 호출부).

    행의 금액은 "기록된 지출"이다 - reset-day로 일 창을 비워도 여기서는 지워지지 않는다.
    """
    rows: Dict[tuple, Dict[str, Any]] = {}
    open_res: Dict[str, Dict[str, Any]] = {}
    settled: Dict[str, int] = {}
    refusals: Counter = Counter()
    resets: List[Dict[str, Any]] = []
    expired = 0
    last_run_id: Optional[str] = None

    def row_for(ev):
        key = (ev.get("day") or "?", ev.get("model") or "?", ev.get("role") or "other")
        if key not in rows:
            rows[key] = {"day": key[0], "model": key[1], "role": key[2], "provider": ev.get("provider") or "?",
                         "calls": 0, "input_tokens": 0, "output_tokens": 0, "billable_output_tokens": 0,
                         "thinking_tokens": 0, "cached_tokens": 0, "nusd": 0, "basis": Counter()}
        return rows[key]

    for ev in events:
        kind = ev.get("ev")
        if kind in ("reserve", "commit") and ev.get("run_id"):
            last_run_id = ev["run_id"]
        if kind == "reserve":
            if ev.get("id") not in settled:
                open_res[ev["id"]] = ev
        elif kind in ("commit", "expire"):
            rid, nusd = ev.get("id"), int(ev.get("nusd", 0))
            if rid in settled:
                extra = nusd - settled[rid]  # 만료 뒤 늦게 온 정산: 더 클 때만 올린다(원장과 같은 규칙)
                if extra > 0:
                    row_for(ev)["nusd"] += extra
                    settled[rid] = nusd
                continue
            reserve = open_res.pop(rid, None)
            row = row_for(reserve or ev)
            settled[rid] = nusd
            row["calls"] += 1
            row["nusd"] += nusd
            row["basis"][ev.get("basis") or "reserved"] += 1
            for name in ("input_tokens", "output_tokens", "billable_output_tokens", "thinking_tokens", "cached_tokens"):
                row[name] += int(ev.get(name) or 0)
            if kind == "expire":
                expired += 1
        elif kind == "refuse":
            refusals[ev.get("scope") or "?"] += 1
        elif kind == "reset_day":
            resets.append({"day": ev.get("day"), "cleared_nusd": int(ev.get("cleared_nusd", 0)), "note": ev.get("note") or ""})

    return {
        "rows": [rows[k] for k in sorted(rows)],
        "open": list(open_res.values()),
        "refusals": dict(refusals),
        "expired": expired,
        "resets": resets,
        "last_run_id": last_run_id,
    }


def _krw(nusd: int, rate: Decimal) -> float:
    return float(Decimal(nusd) / Decimal(10**9) * rate)


def _caps_view(cfg: BudgetConfig, ledger: SpendLedger, run_id: Optional[str] = None) -> Tuple[Dict[str, Any], Totals]:
    run_id = run_id or budget.current_run_id()
    day = cfg.day_of(time.time())
    totals = ledger.totals(run_id=run_id, day=day)
    view: Dict[str, Any] = {}
    for scope in ("run", "day", "total"):
        cap, used = cfg.caps.for_scope(scope), totals.used(scope)
        view[scope] = {"cap_usd": usd(cap), "used_usd": usd(used), "remaining_usd": usd(cap - used),
                       "cap_nusd": cap, "used_nusd": used}
    view["run"]["run_id"] = run_id
    view["day"]["day"] = day
    return view, totals


def build_summary(cfg: BudgetConfig, ledger: SpendLedger, days: Optional[int] = None) -> Dict[str, Any]:
    agg = summarize(ledger.read_events())
    rows = agg["rows"]
    if days:
        keep = set(sorted({r["day"] for r in rows})[-days:])
        rows = [r for r in rows if r["day"] in keep]
    # 런 줄: LLM_RUN_ID로 이어 쓰는 런이 있으면 그 런, 아니면 원장에 마지막으로 기록된 런을 보여준다
    # (이 CLI 프로세스 자신의 런은 지출이 없다).
    caps, totals = _caps_view(cfg, ledger, os.getenv("LLM_RUN_ID", "").strip() or agg["last_run_id"])
    out_rows = []
    for r in rows:
        out_rows.append({
            **{k: r[k] for k in ("day", "model", "role", "provider", "calls", "input_tokens", "output_tokens",
                                 "billable_output_tokens", "thinking_tokens", "cached_tokens")},
            "usd": usd(r["nusd"]), "krw": _krw(r["nusd"], cfg.krw_per_usd), "basis": dict(r["basis"]),
        })
    total_nusd = sum(r["nusd"] for r in rows)
    open_nusd = sum(int(e.get("nusd", 0)) for e in agg["open"])
    return {
        "ledger": ledger.path,
        "day_tz": cfg.day_tz,
        "krw_per_usd": float(cfg.krw_per_usd),
        "krw_is_assumed_rate": True,
        "rows": out_rows,
        "totals": {"calls": sum(r["calls"] for r in rows), "usd": usd(total_nusd),
                   "krw": _krw(total_nusd, cfg.krw_per_usd)},
        "caps": {scope: {k: v for k, v in caps[scope].items() if not k.endswith("_nusd")} for scope in caps},
        "open_reservations": {"count": len(agg["open"]), "usd": usd(open_nusd)},
        "expired_reservations": agg["expired"],
        "refusals": agg["refusals"],
        "day_resets": [{"day": r["day"], "cleared_usd": usd(r["cleared_nusd"]), "note": r["note"]} for r in agg["resets"]],
        "corrupt_ledger_lines": totals.corrupt_lines,
        "kill_switch": _kill_switch_state(),
    }


def _kill_switch_state() -> Dict[str, Any]:
    path = Settings.LLM_KILL_SWITCH_FILE
    state: Dict[str, Any] = {"env": os.getenv("LLM_KILL_SWITCH", "").strip().lower() in {"1", "true", "yes"},
                             "file": path, "file_exists": bool(path and os.path.exists(path))}
    if state["file_exists"]:
        state["payload"] = _read_kill_switch(path)
    return state


def _read_kill_switch(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.loads(f.read() or "null")
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------

def _rate_note(rate: float) -> str:
    return (f"KRW는 가정 환율 ₩{rate:,.0f}/$(LLM_BUDGET_KRW_PER_USD)로 환산한 값이다 - 실제 청구 환율·세금과 다르다. "
            "금액은 단가표로 계산한 추정이고 실제 청구액은 프로바이더 청구서가 기준이다.")


def _basis_text(basis: Dict[str, int]) -> str:
    labels = {"actual": "보고된 사용량", "reserved": "예약액 그대로", "not_billed": "미과금"}
    return ", ".join(f"{labels.get(k, k)} {v}" for k, v in sorted(basis.items()))


def render_summary(data: Dict[str, Any]) -> str:
    lines = [f"LLM 지출 원장: {data['ledger']}", f"날짜 기준 시간대: {data['day_tz']}", _rate_note(data["krw_per_usd"]), ""]
    if not data["rows"] and not data["open_reservations"]["count"]:
        lines.append("기록 없음")
    else:
        header = f"{'day':<11} {'model':<28} {'role':<10} {'calls':>5} {'in_tok':>10} {'out_tok':>9} " \
                 f"{'think':>7} {'cached':>7} {'USD':>11} {'KRW(가정)':>10}  정산 근거"
        lines.append(header)
        for r in data["rows"]:
            lines.append(
                f"{r['day']:<11} {r['model']:<28} {r['role']:<10} {r['calls']:>5} {r['input_tokens']:>10,} "
                f"{r['billable_output_tokens']:>9,} {r['thinking_tokens']:>7,} {r['cached_tokens']:>7,} "
                f"{r['usd']:>11.6f} {'₩' + format(r['krw'], ',.1f'):>10}  {_basis_text(r['basis'])}"
            )
        t = data["totals"]
        lines.append(f"{'합계':<11} {'':<28} {'':<10} {t['calls']:>5} {'':>10} {'':>9} {'':>7} {'':>7} "
                     f"{t['usd']:>11.6f} {'₩' + format(t['krw'], ',.1f'):>10}")
        lines.append("(out_tok은 thinking을 포함한 과금 대상 출력 토큰, 캐시 입력은 정가로 계산)")
    lines.append("")
    lines.append("상한 대비 사용액 (정산 + 열린 예약)")
    rate = data["krw_per_usd"]
    for scope in ("run", "day", "total"):
        c = data["caps"][scope]
        label = {"run": f"run   {c.get('run_id', '')} (마지막 런)", "day": f"day   {c.get('day', '')} (오늘)",
                 "total": "total (원장 전체)"}[scope]
        pct = (c["used_usd"] / c["cap_usd"] * 100) if c["cap_usd"] else 100.0
        lines.append(f"  {label:<52} ${c['used_usd']:.4f} / ${c['cap_usd']:.4f} ({pct:.1f}%)"
                     f"  ≈ ₩{c['used_usd'] * rate:,.0f} / ₩{c['cap_usd'] * rate:,.0f}")
    o = data["open_reservations"]
    lines.append(f"열린 예약 {o['count']}건 (${o['usd']:.6f}) · 만료돼 예약액으로 확정된 예약 {data['expired_reservations']}건"
                 f" · 거부 {sum(data['refusals'].values())}건"
                 + (f" ({', '.join(f'{k} {v}' for k, v in sorted(data['refusals'].items()))})" if data["refusals"] else "")
                 + f" · 해석 못 한 줄 {data['corrupt_ledger_lines']}")
    for r in data["day_resets"]:
        lines.append(f"일 창 초기화: {r['day']} (${r['cleared_usd']:.6f} 비움) {r['note']}".rstrip())
    ks = data["kill_switch"]
    if ks["env"] or ks["file_exists"]:
        owner = (ks.get("payload") or {}).get("engaged_by", "알 수 없음(내용 없음)") if ks["file_exists"] else "env"
        lines.append(f"킬 스위치: 켜짐 ({'env LLM_KILL_SWITCH' if ks['env'] else ks['file']}; 켠 주체: {owner})")
    else:
        lines.append("킬 스위치: 꺼짐")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# estimate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    target: str        # 모델 id 또는 역할 이름
    calls: int
    prompt: int        # 문자 수 또는 토큰 수
    prompt_is_tokens: bool
    max_output_tokens: int


def parse_plan(text: str) -> Plan:
    parts = text.rsplit(":", 3)
    try:
        if len(parts) != 4 or not parts[0].strip():
            raise ValueError
        prompt_raw = parts[2].strip().lower()
        is_tokens = prompt_raw.endswith("t")
        plan = Plan(parts[0].strip(), int(parts[1]), int(prompt_raw.rstrip("t")), is_tokens, int(parts[3]))
        if plan.calls <= 0 or plan.prompt < 0 or plan.max_output_tokens <= 0:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: 형식은 {PLAN_FORMAT} (예: generator:10:17000:8192, "
                                         "gemini-3.5-flash-lite:10:16000t:2048)") from None
    return plan


def build_estimate(cfg: BudgetConfig, ledger: SpendLedger, plans: List[Plan], *, attempts_per_call: int = 1,
                   bytes_per_char: float = DEFAULT_BYTES_PER_CHAR) -> Dict[str, Any]:
    from core.llm.registry import ROLES, resolve_role_config

    day = cfg.day_of(time.time())
    out, total_nusd = [], 0
    for plan in plans:
        role = plan.target.lower() if plan.target.lower() in ROLES else None
        model = resolve_role_config(role)[1] if role else plan.target
        price = budget.BudgetGuard._price(cfg, model, day)
        if plan.prompt_is_tokens:
            est_input = plan.prompt
        else:
            est_input = (math.ceil(plan.prompt * bytes_per_char / cfg.bytes_per_token)
                         + PER_MESSAGE_OVERHEAD_TOKENS * ESTIMATE_MESSAGES_PER_CALL + BASE_OVERHEAD_TOKENS)
        per_call = token_cost_nusd(est_input, price.input_per_1m) + token_cost_nusd(plan.max_output_tokens, price.output_per_1m)
        subtotal = per_call * plan.calls * attempts_per_call
        total_nusd += subtotal
        out.append({
            "model": model, "role": role, "calls": plan.calls, "attempts_per_call": attempts_per_call,
            "est_input_tokens": est_input, "max_output_tokens": plan.max_output_tokens,
            "price_in_per_1m": str(price.input_per_1m), "price_out_per_1m": str(price.output_per_1m),
            "price_source": price.source,
            "worst_case_usd_per_call": usd(per_call), "worst_case_usd": usd(subtotal),
        })
    caps, _ = _caps_view(cfg, ledger)
    # 런 상한: LLM_RUN_ID로 이어 쓰는 런이면 이미 쓴 만큼을 빼고, 아니면 새 런이므로 상한 전체가 남아 있다.
    shared_run = bool(os.getenv("LLM_RUN_ID", "").strip())
    remaining = {
        "run": caps["run"]["cap_nusd"] - (caps["run"]["used_nusd"] if shared_run else 0),
        "day": caps["day"]["cap_nusd"] - caps["day"]["used_nusd"],
        "total": caps["total"]["cap_nusd"] - caps["total"]["used_nusd"],
    }
    return {
        "plans": out,
        "worst_case_usd": usd(total_nusd),
        "worst_case_krw": _krw(total_nusd, cfg.krw_per_usd),
        "krw_per_usd": float(cfg.krw_per_usd),
        "krw_is_assumed_rate": True,
        "remaining_usd": {scope: usd(v) for scope, v in remaining.items()},
        "fits": {scope: total_nusd <= remaining[scope] for scope in ("run", "day", "total")},
        "ledger": ledger.path,
    }


def render_estimate(data: Dict[str, Any]) -> str:
    lines = ["계획한 호출의 최악 비용 (가드가 요청 전에 예약하는 금액 - 실제 비용은 보통 이보다 작다)", ""]
    lines.append(f"{'model':<28} {'role':<10} {'calls':>5} {'x시도':>5} {'in_tok(상한)':>12} {'max_out':>8} {'$/호출':>10} {'소계 USD':>11}")
    for p in data["plans"]:
        lines.append(f"{p['model']:<28} {(p['role'] or '-'):<10} {p['calls']:>5} {p['attempts_per_call']:>5} "
                     f"{p['est_input_tokens']:>12,} {p['max_output_tokens']:>8,} {p['worst_case_usd_per_call']:>10.6f} "
                     f"{p['worst_case_usd']:>11.6f}"
                     + ("  (대체 단가)" if p["price_source"] != "table" else ""))
    lines.append("")
    lines.append(f"합계: ${data['worst_case_usd']:.6f} ≈ ₩{data['worst_case_krw']:,.0f}")
    lines.append(_rate_note(data["krw_per_usd"]))
    lines.append("")
    over = [s for s in ("run", "day", "total") if not data["fits"][s]]
    for scope in ("run", "day", "total"):
        mark = "안" if data["fits"][scope] else "초과"
        lines.append(f"  남은 {scope:<5} 상한 ${data['remaining_usd'][scope]:.4f}  -> {mark}")
    if over:
        lines.append(f"최악 비용 합계가 남은 상한을 넘습니다: {', '.join(over)}. 실제 비용이 예약보다 작으면 더 진행되지만, "
                     "그 상한에서 멈출 수 있습니다(LLM_BUDGET_*_USD로 명시적으로 올리거나 호출 수를 줄이세요).")
    else:
        lines.append("최악의 경우에도 남은 상한 안입니다(재시도는 --attempts-per-call로 반영).")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# reset-day
# ---------------------------------------------------------------------------

def _clear_owned_kill_switch(day: str) -> str:
    path = Settings.LLM_KILL_SWITCH_FILE
    if not path or not os.path.exists(path):
        return "킬 스위치 파일이 없습니다."
    payload = _read_kill_switch(path) or {}
    if payload.get("engaged_by") != "llm_spend_cap":
        return f"킬 스위치 파일은 그대로 둡니다 - 지출 가드가 만든 파일이 아닙니다: {path}"
    if payload.get("scope") != "day" or payload.get("day") != day:
        return (f"킬 스위치 파일은 그대로 둡니다 - {payload.get('scope')} 상한({payload.get('day')})으로 켜진 것이라 "
                f"{day} 하루를 비워도 풀리지 않습니다: {path}")
    os.remove(path)
    return f"킬 스위치 파일을 지웠습니다(지출 가드가 {day} 일 상한으로 켠 것): {path}"


def cmd_reset_day(args, cfg: BudgetConfig, ledger: SpendLedger) -> int:
    day = args.day or cfg.day_of(time.time())
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        print(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {day!r}", file=sys.stderr)
        return 2
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        print(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {day!r}", file=sys.stderr)
        return 2
    current = ledger.totals(run_id=budget.current_run_id(), day=day)
    if not args.yes:
        print(f"{day}의 일 상한 창에 정산된 ${usd(current.committed['day']):.6f}을 비우려면 --yes를 붙이세요. "
              "아무것도 바꾸지 않았습니다.")
        return 2
    cleared = ledger.reset_day(day, note=args.note or "")
    print(f"{day} 일 상한 창을 비웠습니다: ${usd(cleared):.6f}. 지출 기록과 전체 상한 누계는 그대로입니다 "
          f"(전체 사용액 ${usd(current.used('total')):.6f}). 진행 중인 예약 ${usd(current.open['day']):.6f}은 계속 잡힙니다.")
    if args.clear_kill_switch:
        print(_clear_owned_kill_switch(day))
    elif Settings.LLM_KILL_SWITCH_FILE and os.path.exists(Settings.LLM_KILL_SWITCH_FILE):
        print(f"킬 스위치 파일이 남아 있습니다: {Settings.LLM_KILL_SWITCH_FILE} "
              "(지출 가드가 이 날 켠 것이면 --clear-kill-switch로 지울 수 있습니다)")
    return 0


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m core.llm.spend_cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ledger", help="원장 파일 경로 (기본: LLM_SPEND_LEDGER_FILE 또는 "
                                         "<메인 체크아웃>/.ops/llm_spend_ledger.jsonl)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sum = sub.add_parser("summary", parents=[common], help="일·모델·역할별 지출과 상한 대비 사용액")
    p_sum.add_argument("--days", type=int, default=None, help="기록이 있는 날 중 최근 N일만 표에 싣는다(상한 계산은 항상 전체)")
    p_sum.add_argument("--json", action="store_true")

    p_reset = sub.add_parser("reset-day", parents=[common], help="하루의 일 상한 창을 비운다(전체 상한은 그대로)")
    p_reset.add_argument("day", nargs="?", help="YYYY-MM-DD (기본: 오늘, LLM_BUDGET_DAY_TZ 기준)")
    p_reset.add_argument("--yes", action="store_true", help="실제로 비운다. 없으면 무엇을 비울지만 보여준다")
    p_reset.add_argument("--note", default="", help="원장에 남길 사유(예: 콘솔 청구액 확인)")
    p_reset.add_argument("--clear-kill-switch", action="store_true",
                         help="지출 가드가 이 날 일 상한으로 켠 킬 스위치 파일이면 함께 지운다")

    p_est = sub.add_parser("estimate", parents=[common], help="계획한 호출 수의 최악 비용")
    p_est.add_argument("--plan", action="append", type=parse_plan, required=True, metavar=PLAN_FORMAT)
    p_est.add_argument("--attempts-per-call", type=int, default=1, help="호출당 시도 수(재시도 포함) 가정. 기본 1")
    p_est.add_argument("--bytes-per-char", type=float, default=DEFAULT_BYTES_PER_CHAR,
                       help="문자 수를 바이트로 바꾸는 가정(한글 3, 영문 1). 기본 3")
    p_est.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = BudgetConfig.from_env()
        ledger = SpendLedger(args.ledger or cfg.ledger_path, reservation_ttl_s=cfg.reservation_ttl_s)
        if args.cmd == "summary":
            data = build_summary(cfg, ledger, days=args.days)
            print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render_summary(data))
            return 0
        if args.cmd == "reset-day":
            return cmd_reset_day(args, cfg, ledger)
        if args.attempts_per_call <= 0 or args.bytes_per_char <= 0:
            print("--attempts-per-call과 --bytes-per-char는 0보다 커야 합니다", file=sys.stderr)
            return 2
        data = build_estimate(cfg, ledger, args.plan, attempts_per_call=args.attempts_per_call,
                              bytes_per_char=args.bytes_per_char)
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render_estimate(data))
        return 0
    except LLMUnpricedModel as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except (BudgetConfigError, LedgerError, ValueError, OSError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
