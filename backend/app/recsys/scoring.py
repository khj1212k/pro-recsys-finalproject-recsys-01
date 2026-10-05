import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Dict, Optional, Protocol, Sequence, Tuple

import numpy as np

from app.recsys.deadline import Deadline
from app.recsys.metrics import RecsysCounters
from app.recsys.throttle import ThrottledExceptionLog
from app.recsys.types import Item, ScoreResult, UserState

logger = logging.getLogger(__name__)

# 칸 로그의 features(float32 바이트)를 푸는 방법. 요청 로그의 feature_schema_version이 가리킨다.
# 버전을 바꾸지 않고 순서나 뜻을 바꾸면 예전 로그를 잘못 읽게 되므로, 바뀌면 새 번호를 더한다.
HEURISTIC_FEATURE_SCHEMA = 1
FEATURE_SCHEMAS: Dict[int, Tuple[str, ...]] = {
    # 휴리스틱 4항의 가중치를 곱하기 전 값. 신호가 없는 항(장기·단기 벡터 없음)은 NaN이다.
    HEURISTIC_FEATURE_SCHEMA: ("cos_long", "cos_short", "recency", "popularity"),
}


def decode_features(payload: bytes) -> np.ndarray:
    return np.frombuffer(payload, dtype="<f4")


def encode_features(row: np.ndarray) -> bytes:
    return np.asarray(row, dtype="<f4").tobytes()


class Scorer(Protocol):
    def score(self, state: UserState, items: Sequence[Item], now: datetime) -> ScoreResult: ...


class ScorerUnavailable(Exception):
    """이 스코어러가 지금은 점수를 낼 수 없다(모델 미등록, 피처 함수 없음 등). 오류가 아니다."""


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
    """실사용 데이터로 튜닝한 값이 아니라 사전(prior) 가중치다 (ADR 0015).

    장기:단기 = 0.45:0.35는 합성 점검(evaluation/serving/click_shift_sensitivity.py)에서
    고른 값이다. MMR이 후보 풀 안에서 점수를 min-max 정규화하기 때문에 반응이 계단형이라,
    0.5:0.3은 다른 주제를 3번 클릭해도 상위 10개에 그 주제가 10%만 들어오고 0.4:0.4
    이상은 클릭 한 번에 80~90%로 뒤집혔다. 0.45:0.35는 클릭 1/2/3회에 평균 15/29/39%로
    근거가 쌓일수록 늘어난다 - 실수 클릭 한 번에 피드가 장악되지 않으면서 반응은 한다.
    다만 이 값은 절벽 위에 있다: 0.05만 옮겨도 "반응 없음"과 "피드 장악" 사이를 넘어가고, 시드별
    범위도 넓다(클릭 3회 후 20~60%). 주제 4개가 거의 직교하는 합성 코퍼스에서 결과를 보고 고른
    값이므로 실제 임베딩·실제 클릭에서 다시 정해야 한다(ADR 0015 증거 4).
    신선도/인기는 코사인이 비슷할 때의 동점 깨기 역할이라 작게 두고, 식은 배치 인기 랭킹
    (app/recsys/popularity.compute_scores)과 같은 척도를 쓴다.
    """

    long_term: float = 0.45
    short_term: float = 0.35
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
        missing = np.full(len(items), np.nan, dtype=np.float32)
        features = np.column_stack(
            [
                missing if cos_long is None else cos_long,
                missing if cos_short is None else cos_short,
                recency,
                popularity,
            ]
        ).astype(np.float32)
        return ScoreResult(
            scores=scores.astype(np.float64),
            model_version=self.version,
            features=features,
            feature_schema_version=HEURISTIC_FEATURE_SCHEMA,
        )


class ScorerStack:
    """활성 스코어러 하나와 shadow 스코어러들 (ADR 0025).

    목록을 만드는 것은 활성 스코어러의 점수뿐이다. shadow는 같은 사용자 상태·같은 아이템에 점수만 매기고,
    그 점수는 extra_scores로 넘어가 칸 로그에 남는다. shadow가 응답에 영향을 주지 않도록:
    - 예외는 여기서 삼키고 센다(shadow.error). 점수를 낼 수 없는 상태(ScorerUnavailable)는 오류로 세지 않는다.
    - 활성 점수를 낸 뒤 남은 시간 예산이 deadline_fraction 미만이면 남은 shadow를 건너뛴다(shadow.skipped).
    - 점수 배열의 모양이 다르면 버린다.
    막지 못하는 것: shadow 하나가 예산보다 오래 걸리면 그 요청은 시간 초과 폴백으로 끝난다. 건너뛰기는
    shadow를 시작하기 전에만 판단한다.
    """

    def __init__(
        self,
        active: Scorer,
        shadows: Sequence[Scorer] = (),
        deadline_fraction: float = 0.5,
        counters: Optional[RecsysCounters] = None,
    ):
        self.active = active
        self.shadows = list(shadows)
        self.deadline_fraction = deadline_fraction
        self.counters = counters or RecsysCounters()
        self._errors = ThrottledExceptionLog(logger=logger)

    def score(
        self,
        state: UserState,
        items: Sequence[Item],
        now: datetime,
        deadline: Optional[Deadline] = None,
    ) -> ScoreResult:
        result = self.active.score(state, items, now)
        extra: Dict[str, np.ndarray] = {}
        for i, shadow in enumerate(self.shadows):
            if deadline is not None and deadline.remaining_fraction() < self.deadline_fraction:
                self.counters.inc("shadow.skipped", len(self.shadows) - i)
                break
            try:
                shadow_result = shadow.score(state, items, now)
                scores = np.asarray(shadow_result.scores, dtype=np.float64)
                if scores.shape != (len(items),):
                    raise ValueError(f"shadow scores {scores.shape} != ({len(items)},)")
            except ScorerUnavailable:
                self.counters.inc("shadow.unavailable")
                continue
            except Exception:
                # 요청마다 같은 예외가 날 수 있어 traceback은 분당 한 번만 남긴다. 건수는 카운터가 센다.
                self._errors.exception(
                    f"shadow.{i}", "shadow scorer #%d failed; the response is unaffected", i
                )
                self.counters.inc("shadow.error")
                continue
            version = shadow_result.model_version
            if version == result.model_version or version in extra:
                # 활성 모델과 같은 버전(또는 같은 shadow 둘)은 같은 점수를 한 번 더 남길 뿐이다.
                self.counters.inc("shadow.duplicate")
                continue
            extra[version] = scores
            self.counters.inc("shadow.scored")
        return replace(result, extra_scores=extra)
