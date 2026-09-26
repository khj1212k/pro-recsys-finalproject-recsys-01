"""워밍업 생성 실행의 비용 원장·예산 가드·클러스터 선택 (순수 로직).

단가는 공식 가격 페이지 기준(1M 토큰당 USD, 유료 티어 표준, 텍스트 입력). 같은 값이
eval/llm-bakeoff-and-gate 브랜치의 ai_workspace/config/llm_pricing.yaml에 출처·접근일과 함께
있다 - 그 브랜치가 병합되면 이 표 대신 core.llm.pricing을 쓰도록 바꾼다.
단가를 모르는 모델은 0원으로 치지 않고 KeyError로 멈춘다(비용이 조용히 0으로 잡히지 않게).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

PRICE_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"

# model -> (input USD / 1M tokens, output USD / 1M tokens)
PRICES_PER_1M: Dict[str, Tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),  # accessed 2026-09-25
    "gemini-3.1-flash-lite": (0.25, 1.50),  # accessed 2026-09-26
}

# 요청 전 최악 비용 추정에 쓰는 문자당 토큰 상한. 한국어는 Gemini 토크나이저에서 대개
# 1자 ≤ 1토큰이지만(실측 비율은 리포트에 기록) 드문 문자·바이트 폴백을 감안해 여유를 둔다.
WORST_CASE_TOKENS_PER_CHAR = 1.5


def price(model: str) -> Tuple[float, float]:
    if model not in PRICES_PER_1M:
        raise KeyError(f"단가표에 없는 모델: {model!r}")
    return PRICES_PER_1M[model]


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int, total_tokens: Optional[int] = None) -> float:
    """응답 usage로 비용을 계산한다. total_tokens가 prompt+completion보다 크면(보고되지 않은
    reasoning/thinking 토큰) 그 차이를 출력 단가로 과금된다고 보수적으로 친다."""
    p_in, p_out = price(model)
    hidden = 0
    if total_tokens is not None:
        hidden = max(0, int(total_tokens) - int(prompt_tokens) - int(completion_tokens))
    return (prompt_tokens * p_in + (completion_tokens + hidden) * p_out) / 1_000_000


def worst_case_cost_usd(model: str, prompt_chars: int, max_tokens: int) -> float:
    """요청을 보내기 전 이 요청 하나가 쓸 수 있는 최대 비용(입력은 문자 수로 상한 추정)."""
    p_in, p_out = price(model)
    est_in = math.ceil(prompt_chars * WORST_CASE_TOKENS_PER_CHAR)
    return (est_in * p_in + max_tokens * p_out) / 1_000_000


@dataclass
class LedgerEntry:
    ts: float
    model: str
    purpose: str
    kind: str  # "observed"(응답 usage) | "unobserved_upper_bound"(응답 없는 요청의 최악 추정)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    status: Optional[int] = None
    tag: str = ""


class CostLedger:
    """태스크 전체(여러 번 실행해도) 누적 지출을 파일에 남기는 원장."""

    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else None
        self.entries: List[LedgerEntry] = []
        if self.path and self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [LedgerEntry(**e) for e in raw.get("entries", [])]

    def spent(self) -> float:
        return float(sum(e.cost_usd for e in self.entries))

    def add(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)
        self._flush()

    def _flush(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"spent_usd": self.spent(), "entries": [asdict(e) for e in self.entries]}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


@dataclass
class BudgetGuard:
    cap_usd: float
    ledger: CostLedger
    blocked: List[dict] = field(default_factory=list)

    def allows(self, model: str, prompt_chars: int, max_tokens: int) -> bool:
        """지금까지 지출 + 이 요청의 최악 비용이 상한 이하일 때만 허용한다."""
        return self.ledger.spent() + worst_case_cost_usd(model, prompt_chars, max_tokens) <= self.cap_usd

    def record_block(self, model: str, purpose: str, prompt_chars: int, max_tokens: int) -> None:
        self.blocked.append({
            "ts": time.time(), "model": model, "purpose": purpose,
            "spent_usd": self.ledger.spent(),
            "worst_case_usd": worst_case_cost_usd(model, prompt_chars, max_tokens),
        })


def select_clusters(clusters: Sequence[dict], n: int, seed: int, min_size: int = 3) -> List[dict]:
    """클러스터 크기가 퍼지도록 n개를 고른다.

    크기 min_size 이상인 그룹의 서로 다른 크기 값들을 오름차순으로 놓고, 그 위에서 균등 간격으로
    n개 크기를 골라(최소·최대 크기 포함) 각 크기에서 seed 난수로 한 개씩 뽑는다. 서로 다른 크기가
    n개보다 적으면 남은 그룹 중에서 seed 난수로 채운다. min_size 미만(split_v2가 만든 1~2건
    조각)은 제외한다. 반환 순서는 크기 오름차순으로 뽑힌 순서.
    """
    rng = np.random.default_rng(seed)
    by_size: Dict[int, List[dict]] = {}
    for c in sorted(clusters, key=lambda c: int(c["cluster_idx"])):
        size = int(c["size"])
        if size >= min_size:
            by_size.setdefault(size, []).append(c)
    levels = sorted(by_size)
    if not levels or n <= 0:
        return []
    k = min(n, len(levels))
    positions = sorted(set(int(p) for p in np.round(np.linspace(0, len(levels) - 1, k))))
    picked: List[dict] = []
    for p in positions:
        pool = by_size[levels[p]]
        picked.append(pool.pop(int(rng.integers(len(pool)))))
    rest = [c for lv in levels for c in by_size[lv]]
    while len(picked) < n and rest:
        picked.append(rest.pop(int(rng.integers(len(rest)))))
    return picked


def generator_visible_articles(articles: Sequence[dict], max_articles: int = 10, chars: int = 1500) -> List[dict]:
    """NewsReconstructor.reconstruct가 실제로 프롬프트에 넣는 부분: 기사가 max_articles보다
    많으면 본문 길이 내림차순 상위 max_articles건, 각 본문의 앞 chars자."""
    if len(articles) > max_articles:
        articles = sorted(articles, key=lambda a: len(a.get("content") or ""), reverse=True)[:max_articles]
    return [{**a, "content": (a.get("content") or "")[:chars]} for a in articles]
