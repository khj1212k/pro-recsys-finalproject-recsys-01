# LLM 토큰 단가표 로더 - config/llm_pricing.yaml
# 단가를 모르는 모델은 0원이 아니라 KeyError로 실패한다: 비용이 조용히 0으로 잡히면
# bake-off의 동률 처리(ADR 0009, 100건당 비용)가 그 후보에게 유리하게 뒤집힌다.

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import yaml

DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[2] / "config" / "llm_pricing.yaml"


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


class PriceTable:
    def __init__(self, currency: str, entries: Dict[str, List[PriceEntry]]):
        self.currency = currency
        self._entries = entries

    @classmethod
    def from_dict(cls, raw: dict) -> "PriceTable":
        entries: Dict[str, List[PriceEntry]] = {}
        for model, spec in (raw.get("models") or {}).items():
            parsed = []
            for item in spec.get("prices") or []:
                source = str(item.get("source") or "")
                accessed = item.get("accessed")
                if not source.startswith("http") or not accessed:
                    raise ValueError(f"{model}: 모든 가격 항목에 source URL과 accessed 날짜가 필요합니다")
                parsed.append(PriceEntry(
                    input_per_1m=float(item["input_per_1m"]),
                    output_per_1m=float(item["output_per_1m"]),
                    source=source,
                    accessed=_parse_date(accessed),
                    valid_from=_parse_date(item.get("from")),
                    valid_until=_parse_date(item.get("until")),
                ))
            if not parsed:
                raise ValueError(f"{model}: 가격 항목이 없습니다")
            entries[model] = parsed
        return cls(raw.get("currency", "USD"), entries)

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
