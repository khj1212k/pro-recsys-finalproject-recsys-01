import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

import numpy as np

from recsys_core import serving as core_serving

from app.recsys.deadline import Deadline
from app.recsys.metrics import RecsysCounters
from app.recsys.popularity import compute_scores
from app.recsys.shadow import DeferredResult, ShadowRunner
from app.recsys.throttle import ThrottledExceptionLog
from app.recsys.types import Item, NewsletterMeta, ScoreResult, UserState, WindowCounts

logger = logging.getLogger(__name__)

# 칸 로그의 features(float32 바이트)를 푸는 방법. 요청 로그의 feature_schema_version이 가리킨다.
# 버전을 바꾸지 않고 순서나 뜻을 바꾸면 예전 로그를 잘못 읽게 되므로, 바뀌면 새 번호를 더한다.
# 1 = heuristic-v1(사전값)의 4항. 2026-10-17 전의 로그와 되돌리기 세트(RECSYS_HEURISTIC_SET=prior)가 쓴다.
PRIOR_HEURISTIC_FEATURE_SCHEMA = 1
# recsys_core 서빙 어댑터의 22열(ADR 0033). 값의 정의는 recsys_core.serving의 시각 규칙과 SCHEMA_DEFINITION이다.
ADAPTER_FEATURE_SCHEMA = core_serving.FEATURE_SCHEMA_VERSION
# 3 = heuristic-v2의 4항(ADR 0014 사전 등록 2). 활성 휴리스틱의 기본 스키마다.
HEURISTIC_FEATURE_SCHEMA = 3
FEATURE_SCHEMAS: Dict[int, Tuple[str, ...]] = {
    # 가중치를 곱하기 전 값. 신호가 없는 항(장기·단기 벡터 없음)은 NaN이다.
    PRIOR_HEURISTIC_FEATURE_SCHEMA: ("cos_long", "cos_short", "recency", "popularity"),
    ADAPTER_FEATURE_SCHEMA: tuple(core_serving.FEATURE_NAMES),
    # 가중치를 곱하기 전 값. 개인 신호가 없는 항과 읽지 못한 클릭 항은 NaN이다(점수에서는 0).
    HEURISTIC_FEATURE_SCHEMA: ("cos_long", "cos_short", "recency", "log1p_clicks_6h"),
}
assert len(FEATURE_SCHEMAS) == 3, "피처 스키마 버전 번호가 겹칩니다"

FeatureFn = Callable[[UserState, Sequence[Item], datetime], np.ndarray]
PopularityLoader = Callable[[], Dict[int, WindowCounts]]


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


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)


def item_ages_hours(items: Sequence[Item], now: datetime) -> np.ndarray:
    """heuristic-v1의 경과 시간: now - created_at(초 미만 포함)."""
    ages = []
    for it in items:
        ages.append(max(0.0, (now - _aware(it.created_at)).total_seconds() / 3600.0))
    return np.asarray(ages, dtype=np.float32)


def hours_since_pub(created_at: Sequence[datetime], now: datetime) -> np.ndarray:
    """서빙 어댑터의 hours_since_pub과 같은 값(ADR 0014 사전 등록 2): (T - floor(생성 시각)) / 3600, T = floor(now) + 1.
    하네스·어댑터와 같은 정수 초 규칙이라 같은 요청의 값이 1e-6 안에서 같다."""
    t_req = core_serving.request_second(now)
    seconds = np.asarray([t_req - core_serving.epoch_seconds(_aware(c)) for c in created_at], dtype=np.float64)
    return (np.maximum(0.0, seconds) / 3600.0).astype(np.float32)


@dataclass(frozen=True)
class HeuristicWeights:
    """heuristic-v2의 4항 가중치(ADR 0014). 기본값은 서빙 기본 세트 (b)다.

    값은 ADR 0013 A2 E7이 EB-NeRD에서 seed 0·1·2마다 적합한 원값의 원소별 산술 평균이다
    (reports/recsys/ebnerd_v1_2_cold.json e7.weights, ADR 0014 사전 등록 1). 점수는 가중치에 선형이라 이 점수는 세
    seed 점수의 평균과 같다. 평균 가중치의 점수기 자체는 E7에서 평가하지 않았다(미측정).
    - long_term: cos(사용자 프로필, 후보)        - 하네스 hist_cos
    - short_term: cos(최근 클릭 단위 벡터 합, 후보) - 하네스 short_cos
    - recency: exp(-hours_since_pub / recency_tau_hours)
    - popularity: log1p(최근 6시간 클릭 수)       - 하네스 pop_clicks_6h
    """

    long_term: float = 7.676315219348940
    short_term: float = 0.861686427004918
    recency: float = 4.508614262759206
    popularity: float = 1.066841429686268
    recency_tau_hours: float = 48.0


# 서빙 세트(ADR 0014 사전 등록 1·5). (b) = sub1(유저 1%) 적합, 서빙 기본값. (a) = 전체 트래픽 적합 - 전환 규칙(사전 등록 6)을
# 만족할 때만 쓴다.
WEIGHTS_B = HeuristicWeights()
WEIGHTS_A = HeuristicWeights(
    long_term=5.041179796065179,
    short_term=2.772215179557055,
    recency=1.846242902324785,
    popularity=0.705847576322416,
)
HEURISTIC_SETS: Dict[str, HeuristicWeights] = {"b": WEIGHTS_B, "a": WEIGHTS_A}
PRIOR_SET = "prior"


def _set_name(weights: HeuristicWeights) -> str:
    for name, w in HEURISTIC_SETS.items():
        if w == weights:
            return name
    return "custom"


def _counts_vector(ids: Sequence[int], clicks_6h: Optional[Mapping[int, int]]) -> Optional[np.ndarray]:
    if clicks_6h is None:
        return None
    return np.asarray([int(clicks_6h.get(int(i), 0)) for i in ids], dtype=np.float64)


def _ordered(ids: Sequence[int], created_at: Sequence[datetime], scores: np.ndarray) -> List[Tuple[int, float]]:
    """점수 내림차순, 동점은 생성 시각 내림차순 -> 뉴스레터 ID 내림차순(ADR 0014 사전 등록 3·4)."""
    keys = sorted(
        range(len(ids)),
        key=lambda j: (-float(scores[j]), -_aware(created_at[j]).timestamp(), -int(ids[j])),
    )
    return [(int(ids[j]), float(scores[j])) for j in keys]


def popularity_order(metas: Sequence[NewsletterMeta], clicks_6h: Optional[Mapping[int, int]]) -> List[int]:
    """후보 출처 popular의 순서(ADR 0014 사전 등록 4): 6시간 클릭 수 내림차순 -> 생성 시각 내림차순 -> ID 내림차순.
    클릭 수를 읽지 못했으면(None) 전부 0으로 본다(생성 시각 순서가 된다)."""
    ids = [int(m.news_letter_id) for m in metas]
    counts = _counts_vector(ids, clicks_6h)
    scores = np.zeros(len(ids)) if counts is None else counts
    return [nid for nid, _ in _ordered(ids, [m.news_letter_created_at for m in metas], scores)]


class HeuristicScorer:
    """활성 휴리스틱 스코어러 heuristic-v2 (ADR 0014 사전 등록 2).

        s = w_long·cos_long + w_short·cos_short + w_recency·exp(-h/48) + w_pop·log1p(c6h)

    - cos_long: state.profile(장기 벡터 = 증분 상태의 방향, 없으면 콜드 체인의 온보딩·카테고리 중심 벡터)과의 코사인.
    - cos_short: state.short_term(서빙 어댑터와 같은 최근 클릭의 단위 벡터 합)과의 코사인.
    - h: 서빙 어댑터의 hours_since_pub(hours_since_pub 함수).
    - c6h: state.clicks_6h - 어댑터의 pop_clicks_6h와 같은 창의 클릭 수. None이면 읽지 못한 것이고 그 항은 모든 후보에서
      0이다(순위에 영향이 없다).
    신호가 없는 항은 0이고 다른 항으로 가중치를 넘기지 않는다 - 하네스의 피처 값(히스토리가 없으면 hist_cos = 0)과 E7이
    적합한 식 그대로다. 칸 로그의 피처(스키마 3)에는 신호가 없는 항과 읽지 못한 클릭 항을 NaN으로 남긴다.
    """

    def __init__(self, weights: Optional[HeuristicWeights] = None, set_name: Optional[str] = None):
        if weights is None:
            set_name = set_name or "b"
            if set_name not in HEURISTIC_SETS:
                raise ValueError(f"unknown heuristic weight set {set_name!r}; expected one of {sorted(HEURISTIC_SETS)}")
            weights = HEURISTIC_SETS[set_name]
        self.w = weights
        self.set_name = set_name or _set_name(weights)
        self.version = f"heuristic-v2-{self.set_name}"

    # 인기 항이 6시간 클릭 수를 읽는다(state.clicks_6h). 콜드 경로가 이 값을 보고 클릭 수를 읽을지 정한다.
    uses_clicks = True

    @property
    def cold_model_version(self) -> str:
        return self.version

    def _non_personal(self, created_at: Sequence[datetime], counts: Optional[np.ndarray], now: datetime):
        recency = np.exp(-hours_since_pub(created_at, now).astype(np.float64) / self.w.recency_tau_hours)
        log_clicks = None if counts is None else np.log1p(counts)
        scores = self.w.recency * recency
        if log_clicks is not None:
            scores = scores + self.w.popularity * log_clicks
        return scores, recency, log_clicks

    def score(self, state: UserState, items: Sequence[Item], now: datetime) -> ScoreResult:
        n = len(items)
        ids = [int(it.news_letter_id) for it in items]
        scores, recency, log_clicks = self._non_personal(
            [it.created_at for it in items], _counts_vector(ids, getattr(state, "clicks_6h", None)), now
        )
        missing = np.full(n, np.nan, dtype=np.float64)
        cos_long = cos_short = None
        if n and (state.profile is not None or state.short_term is not None):
            emb = _normalize_rows(np.stack([it.embedding for it in items]).astype(np.float32))
            cos_long = _cosine(emb, state.profile)
            cos_short = _cosine(emb, state.short_term)
        if cos_long is not None:
            scores = scores + self.w.long_term * cos_long.astype(np.float64)
        if cos_short is not None:
            scores = scores + self.w.short_term * cos_short.astype(np.float64)
        features = np.column_stack(
            [
                missing if cos_long is None else cos_long,
                missing if cos_short is None else cos_short,
                recency,
                missing if log_clicks is None else log_clicks,
            ]
        ).astype(np.float32)
        return ScoreResult(
            scores=np.asarray(scores, dtype=np.float64),
            model_version=self.version,
            features=features.reshape(n, 4),
            feature_schema_version=HEURISTIC_FEATURE_SCHEMA,
        )

    def cold_order(
        self, metas: Sequence[NewsletterMeta], clicks_6h: Optional[Mapping[int, int]], now: datetime
    ) -> List[Tuple[int, float]]:
        """개인 신호가 전혀 없는 사용자의 순서(ADR 0014 사전 등록 3): 같은 식의 비개인 두 항.
        반환: (뉴스레터 ID, 점수) 점수 내림차순, 동점은 생성 시각 내림차순 -> ID 내림차순."""
        ids = [int(m.news_letter_id) for m in metas]
        created = [m.news_letter_created_at for m in metas]
        scores, _, _ = self._non_personal(created, _counts_vector(ids, clicks_6h), now)
        return _ordered(ids, created, np.asarray(scores, dtype=np.float64))


@dataclass(frozen=True)
class PriorHeuristicWeights:
    """heuristic-v1의 사전(prior) 가중치 (ADR 0015 증거 4). 되돌리기 세트(RECSYS_HEURISTIC_SET=prior)로만 남는다.

    장기:단기 = 0.45:0.35는 합성 점검(evaluation/serving/click_shift_sensitivity.py)에서 고른 값이고 절벽 위에 있다.
    E7(ADR 0013 A2)에서 EB-NeRD sub1 칸의 (b) 적합 세트가 이 사전값보다 나았다(+0.0208 [+0.0080, +0.0339]).
    """

    long_term: float = 0.45
    short_term: float = 0.35
    recency: float = 0.15
    popularity: float = 0.05
    recency_tau_hours: float = 48.0


class PriorHeuristicScorer:
    """heuristic-v1: 2026-10-17까지의 활성 스코어러를 바꾸지 않고 둔 것(되돌리기용, ADR 0014 사전 등록 5).

    인기 항은 클릭이 아니라 뉴스레터에 묶인 기사 수(raw_news_count)의 min(1, log1p(x)/5)이고, 한쪽 벡터가 없으면 그
    가중치를 다른 쪽으로 넘겨 개인화 비중을 유지한다. 콜드 경로는 compute_scores(popularity-v1) 순서다.
    """

    version = "heuristic-v1"
    cold_model_version = "popularity-v1"
    uses_clicks = False

    def __init__(self, weights: PriorHeuristicWeights = PriorHeuristicWeights()):
        self.w = weights
        self.set_name = PRIOR_SET

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
            feature_schema_version=PRIOR_HEURISTIC_FEATURE_SCHEMA,
        )

    def cold_order(
        self, metas: Sequence[NewsletterMeta], clicks_6h: Optional[Mapping[int, int]], now: datetime
    ) -> List[Tuple[int, float]]:
        """popularity-v1: compute_scores(최신성 + 묶인 기사 수) 순서. 클릭 수는 읽지 않는다."""
        return [(int(s["id"]), float(s["score"])) for s in compute_scores(metas, now)]


def make_heuristic(set_name: str):
    """설정(RECSYS_HEURISTIC_SET)의 활성 휴리스틱. prior는 heuristic-v1이다."""
    if set_name == PRIOR_SET:
        return PriorHeuristicScorer()
    return HeuristicScorer(set_name=set_name)


def heuristic_of(scorer) -> Optional[object]:
    """스코어러(또는 그 폴백)가 휴리스틱이면 그것. 콜드 경로가 같은 휴리스틱의 순서를 쓰려고 찾는다."""
    for candidate in (scorer, getattr(scorer, "fallback", None)):
        if candidate is not None and callable(getattr(candidate, "cold_order", None)):
            return candidate
    return None


class ScorerStack:
    """활성 스코어러 하나, shadow 스코어러들, 로그용 피처 (ADR 0025, 0033).

    목록을 만드는 것은 활성 스코어러의 점수뿐이다. shadow는 같은 사용자 상태·같은 아이템에 점수만 매기고
    그 점수는 칸 로그에 남는다. feature_fn(서빙 피처 어댑터)이 있으면 같은 후보의 피처 행렬도 한 번 계산해
    칸 로그에 남기고, 같은 어댑터를 쓰는 shadow 모델들이 그 행렬을 같이 쓴다.

    shadow와 피처 계산이 응답에 영향을 주지 않도록:
    - runner(app.recsys.shadow.ShadowRunner)가 있으면 그 일을 전용 스레드에 넘기고 기다리지 않는다. 결과는
      ScoreResult.deferred로 나가고 응답을 보낸 뒤 로그를 쓸 때 찾아간다. 느린 shadow가 요청의 시간 예산을
      쓰지 않는다. 운영 배선(runtime.build_sql_service)은 이쪽이다. 활성 점수를 낸 시점에 남은 시간 예산이
      deadline_fraction 미만이면(프로세스가 바쁘다) 넘기지도 않는다(shadow.shed).
    - runner가 없으면 여기서 바로 돈다(테스트와 단순 배선). 활성 점수를 낸 뒤 남은 시간 예산이
      deadline_fraction 미만이면 건너뛴다(shadow.skipped, features.skipped).
    - 어느 쪽이든 예외는 삼키고 센다(shadow.error, features.error). 점수를 낼 수 없는 상태
      (ScorerUnavailable)와 피처 입력이 맞지 않는 요청(features.inputs_missing)은 오류로 세지 않는다.
    - 점수 배열의 모양이 다르면 버린다.

    어댑터의 인기도 입력(state.popularity)은 활성 스코어러가 그 피처로 점수를 낼 때만 요청 경로에서 읽는다
    (active_needs_features). 아니면 popularity_loader가 요청 경로 밖에서 자기 커넥션으로 읽는다.
    """

    def __init__(
        self,
        active: Scorer,
        shadows: Sequence[Scorer] = (),
        deadline_fraction: float = 0.5,
        counters: Optional[RecsysCounters] = None,
        feature_fn: Optional[FeatureFn] = None,
        runner: Optional[ShadowRunner] = None,
    ):
        self.active = active
        self.shadows = list(shadows)
        self.deadline_fraction = deadline_fraction
        self.counters = counters or RecsysCounters()
        self.feature_fn = feature_fn
        self.runner = runner
        self._errors = ThrottledExceptionLog(logger=logger)

    def active_needs_features(self) -> bool:
        """활성 스코어러가 지금 어댑터 피처로 점수를 내는가(모델이 올라와 있는 LightGBM)."""
        return bool(getattr(self.active, "needs_features", False))

    def score(
        self,
        state: UserState,
        items: Sequence[Item],
        now: datetime,
        deadline: Optional[Deadline] = None,
        popularity_loader: Optional[PopularityLoader] = None,
    ) -> ScoreResult:
        result = self.active.score(state, items, now)
        if self.feature_fn is None and not self.shadows:
            return result

        def job(expired: Callable[[], bool]) -> DeferredResult:
            return self._extras(state, items, now, result, popularity_loader, expired)

        def out_of_budget() -> bool:
            return deadline is not None and deadline.remaining_fraction() < self.deadline_fraction

        if self.runner is not None:
            if out_of_budget():
                # 활성 점수를 내기까지 이미 예산의 큰 부분을 썼다 = 프로세스가 바쁘다. 전용 스레드의 일도 같은
                # 프로세스의 CPU를 쓰므로 이런 요청의 shadow·피처 작업은 만들지 않는다(부하가 걸릴 때 먼저 버린다).
                self.counters.inc("shadow.shed")
                return result
            # 요청 경로는 여기서 끝난다: 넘기고 기다리지 않는다. 받지 않으면(밀림·차단) shadow 없이 간다.
            return replace(result, deferred=self.runner.submit(job))

        extras = job(out_of_budget)
        if extras.features is None:
            return replace(result, extra_scores=extras.extra_scores)
        return replace(
            result,
            extra_scores=extras.extra_scores,
            features=extras.features,
            feature_schema_version=extras.feature_schema_version,
        )

    # ------------------------------------------------------------------ 요청 경로 밖에서 도는 부분
    def _shared_features(
        self,
        state: UserState,
        items: Sequence[Item],
        now: datetime,
        active: ScoreResult,
        popularity_loader: Optional[PopularityLoader],
        expired: Callable[[], bool],
    ) -> Tuple[UserState, Optional[np.ndarray], Optional[int]]:
        fn = self.feature_fn
        if fn is None:
            return state, None, None
        version = getattr(fn, "schema_version", None)
        if active.features is not None and version is not None and active.feature_schema_version == version:
            return state, active.features, version  # 활성 모델이 이미 같은 어댑터로 계산했다
        if expired():
            self.counters.inc("features.skipped")
            return state, None, None
        try:
            if getattr(state, "popularity", None) is None and popularity_loader is not None:
                state = replace(state, popularity=popularity_loader())
            features = np.asarray(fn(state, items, now))
            if features.ndim != 2 or features.shape[0] != len(items):
                raise ValueError(f"feature matrix {features.shape} does not have {len(items)} rows")
        except core_serving.FeatureInputsMissing:
            # 입력이 이 요청 시각의 것으로 맞지 않는다(예: 상태가 클릭 로그보다 뒤처짐). 틀린 값을 남기지 않는다.
            self.counters.inc("features.inputs_missing")
            return state, None, None
        except Exception:
            self._errors.exception("features", "serving feature computation failed; the response is unaffected")
            self.counters.inc("features.error")
            return state, None, None
        self.counters.inc("features.computed")
        return state, features, version

    def _extras(
        self,
        state: UserState,
        items: Sequence[Item],
        now: datetime,
        active: ScoreResult,
        popularity_loader: Optional[PopularityLoader],
        expired: Callable[[], bool],
    ) -> DeferredResult:
        state, features, version = self._shared_features(state, items, now, active, popularity_loader, expired)
        extra: Dict[str, np.ndarray] = {}
        for i, shadow in enumerate(self.shadows):
            if expired():
                self.counters.inc("shadow.skipped", len(self.shadows) - i)
                break
            shares = self.feature_fn is not None and getattr(shadow, "feature_fn", None) is self.feature_fn
            if shares and features is None:
                # 같은 어댑터를 쓰는 모델인데 이번 요청의 피처가 없다: 점수 없음(오류가 아니다).
                self.counters.inc("shadow.unavailable")
                continue
            try:
                if shares:
                    shadow_result = shadow.score(state, items, now, features=features)
                else:
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
            shadow_version = shadow_result.model_version
            if shadow_version == active.model_version or shadow_version in extra:
                # 활성 모델과 같은 버전(또는 같은 shadow 둘)은 같은 점수를 한 번 더 남길 뿐이다.
                self.counters.inc("shadow.duplicate")
                continue
            extra[shadow_version] = scores
            self.counters.inc("shadow.scored")
        return DeferredResult(features=features, feature_schema_version=version, extra_scores=extra)
