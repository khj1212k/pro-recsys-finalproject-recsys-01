from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol, Sequence

import numpy as np

from app.recsys.types import Item, ScoreResult, UserState


class Scorer(Protocol):
    def score(self, state: UserState, items: Sequence[Item], now: datetime) -> ScoreResult: ...


def _normalize_rows(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms > 0, norms, 1.0)


def _cosine(matrix_normed: np.ndarray, v: Optional[np.ndarray]) -> Optional[np.ndarray]:
    if v is None:
        return None
    n = float(np.linalg.norm(v))
    if n == 0.0:
        return None
    return matrix_normed @ (np.asarray(v, dtype=np.float32) / n)


def item_ages_hours(items: Sequence[Item], now: datetime) -> np.ndarray:
    ages = []
    for it in items:
        created = it.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        ages.append(max(0.0, (now - created).total_seconds() / 3600.0))
    return np.asarray(ages, dtype=np.float32)


@dataclass(frozen=True)
class HeuristicWeights:
    """데이터로 튜닝한 값이 아니라 사전(prior) 가중치다 (ADR 0015).

    장기 프로필이 목록의 대부분을 정하고, 방금 클릭한 주제(단기)는 클릭 한 번으로
    순위를 눈에 띄게 끌어올릴 만큼만 준다. 신선도/인기는 코사인이 비슷할 때의
    동점 깨기 역할이라 작게 둔다. 신선도·인기 식은 배치 인기 랭킹
    (scheduler/calculate_ranking.compute_scores)과 같은 척도를 쓴다.
    """

    long_term: float = 0.5
    short_term: float = 0.3
    recency: float = 0.15
    popularity: float = 0.05
    recency_tau_hours: float = 48.0


class HeuristicScorer:
    version = "heuristic-v1"

    def __init__(self, weights: HeuristicWeights = HeuristicWeights()):
        self.w = weights

    def score(self, state: UserState, items: Sequence[Item], now: datetime) -> ScoreResult:
        emb = _normalize_rows(np.stack([it.embedding for it in items]).astype(np.float32))
        cos_long = _cosine(emb, state.profile)
        cos_short = _cosine(emb, state.short_term)

        w_long, w_short = self.w.long_term, self.w.short_term
        # 한쪽 신호가 없으면 그 가중치를 다른 쪽으로 넘겨 개인화 비중(0.8)을 유지한다.
        if cos_short is None:
            w_long, w_short = w_long + w_short, 0.0
        elif cos_long is None:
            w_long, w_short = 0.0, w_long + w_short

        recency = np.exp(-item_ages_hours(items, now) / self.w.recency_tau_hours)
        popularity = np.minimum(
            1.0, np.log1p(np.asarray([it.raw_news_count for it in items], dtype=np.float32)) / 5.0
        )

        scores = self.w.recency * recency + self.w.popularity * popularity
        if cos_long is not None:
            scores = scores + w_long * cos_long
        if cos_short is not None:
            scores = scores + w_short * cos_short
        return ScoreResult(scores=scores.astype(np.float64), model_version=self.version)
