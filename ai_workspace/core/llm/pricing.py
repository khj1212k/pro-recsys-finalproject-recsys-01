# LLM 토큰 단가표 로더 - config/llm_pricing.yaml
# 단가를 모르는 모델은 0원이 아니라 KeyError로 실패한다: 비용이 조용히 0으로 잡히면
# bake-off의 동률 처리(ADR 0009, 100건당 비용)가 그 후보에게 유리하게 뒤집힌다.
#
# 단가표의 값도 검증한다. 지출 가드(core/llm/budget.py, docs/adr/0035)는 이 값으로 최악 비용을
# 예약하므로, 값이 틀리면 상한이 걸리지 않는다: 0이면 예약이 0원이라 무제한이고, 1K 토큰당 단가를
# 1M 칸에 적으면 1000배 적게 잡히며, 통화가 USD가 아니면 숫자의 뜻이 달라진다. 그런 표는 로드를
# 거부한다(= 가드가 호출을 거부한다).

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import yaml

DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[2] / "config" / "llm_pricing.yaml"

# 지출 가드와 bake-off 비용 계산이 전제하는 통화.
CURRENCY = "USD"
# USD / 1M 토큰. 이보다 작은 단가는 1K 토큰당 단가를 잘못 적은 것으로 보고 거부한다(2026-10 기준
# 단가표에서 가장 싼 값은 입력 $0.15다). 정말 그만큼 싼 모델이면 그 가격 항목에
# `allow_below_floor: true`를 명시한다.
MIN_PLAUSIBLE_PER_1M = 0.01


def _parse_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass(frozen=True)
class PriceEntry:
    input_per_1m: float
    output_per_1m: float
    source: str
    accessed: date
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None

    def applies(self, on: date) -> bool:
        if self.valid_from and on < self.valid_from:
            return False
        if self.valid_until and on > self.valid_until:
            return False
        return True


def _price_per_1m(model: str, item: dict, name: str) -> float:
    raw = item.get(name)
    try:
        if isinstance(raw, bool) or raw is None:
            raise ValueError
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{model}: {name}={raw!r}는 숫자가 아닙니다") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{model}: {name}={raw!r}는 0보다 큰 유한한 수여야 합니다(0이면 비용이 0원으로 잡혀 상한이 걸리지 않습니다)"
        )
    if value < MIN_PLAUSIBLE_PER_1M and item.get("allow_below_floor") is not True:
        raise ValueError(
            f"{model}: {name}={raw!r}는 1M 토큰당 ${MIN_PLAUSIBLE_PER_1M} 미만입니다 - 1K 토큰당 단가를 적은 것이 "
            "아닌지 확인하세요(맞는 값이면 그 항목에 allow_below_floor: true)"
        )
    return value


def _windows_overlap(a: PriceEntry, b: PriceEntry) -> bool:
    return ((a.valid_from or date.min) <= (b.valid_until or date.max)
            and (b.valid_from or date.min) <= (a.valid_until or date.max))


class PriceTable:
    def __init__(self, currency: str, entries: Dict[str, List[PriceEntry]]):
        self.currency = currency
        self._entries = entries

    @classmethod
    def from_dict(cls, raw: dict) -> "PriceTable":
        currency = raw.get("currency", CURRENCY)
        if currency != CURRENCY:
            raise ValueError(f"단가표의 currency는 {CURRENCY}여야 합니다(지금 {currency!r}) - 비용 계산이 {CURRENCY}를 전제합니다")
        entries: Dict[str, List[PriceEntry]] = {}
        for model, spec in (raw.get("models") or {}).items():
            parsed: List[PriceEntry] = []
            for item in spec.get("prices") or []:
                source = str(item.get("source") or "")
                accessed = item.get("accessed")
                if not source.startswith("http") or not accessed:
                    raise ValueError(f"{model}: 모든 가격 항목에 source URL과 accessed 날짜가 필요합니다")
                entry = PriceEntry(
                    input_per_1m=_price_per_1m(model, item, "input_per_1m"),
                    output_per_1m=_price_per_1m(model, item, "output_per_1m"),
                    source=source,
                    accessed=_parse_date(accessed),
                    valid_from=_parse_date(item.get("from")),
                    valid_until=_parse_date(item.get("until")),
                )
                if entry.valid_from and entry.valid_until and entry.valid_from > entry.valid_until:
                    raise ValueError(f"{model}: from({entry.valid_from})이 until({entry.valid_until})보다 늦습니다")
                # 같은 날에 유효한 항목이 둘이면 어느 가격인지 정할 수 없다. until을 달지 않은 옛 가격이
                # 새 가격을 가리는 실수가 이 경우다.
                if any(_windows_overlap(entry, other) for other in parsed):
                    raise ValueError(
                        f"{model}: 유효 기간이 겹치는 가격 항목이 있습니다 - 옛 항목에 until을 달고 새 항목에 from을 다세요"
                    )
                parsed.append(entry)
            if not parsed:
                raise ValueError(f"{model}: 가격 항목이 없습니다")
            entries[model] = parsed
        return cls(currency, entries)

    def models(self) -> List[str]:
        return sorted(self._entries)

    def price(self, model: str, on: Optional[date] = None) -> PriceEntry:
        if model not in self._entries:
            raise KeyError(f"단가표에 없는 모델: {model!r} (config/llm_pricing.yaml에 추가 필요)")
        on = on or date.today()
        for entry in self._entries[model]:
            if entry.applies(on):
                return entry
        raise KeyError(f"{model}: {on.isoformat()}에 유효한 가격 항목이 없습니다")

    def cost(self, model: str, input_tokens: int, output_tokens: int, on: Optional[date] = None) -> float:
        entry = self.price(model, on)
        return (input_tokens * entry.input_per_1m + output_tokens * entry.output_per_1m) / 1_000_000


def load_pricing(path: Optional[Path] = None) -> PriceTable:
    with open(path or DEFAULT_PRICING_PATH, encoding="utf-8") as f:
        return PriceTable.from_dict(yaml.safe_load(f))
