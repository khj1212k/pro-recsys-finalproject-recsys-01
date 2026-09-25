"""LightGBM 랭커 어댑터 (ADR 0015).

피처 함수는 이 저장소가 아니라 런타임에 주입된다(RECSYS_FEATURE_FN="모듈:함수" 또는
생성자 인자) - 학습 파이프라인이 쓰는 피처 코드를 서빙에서 복제하지 않기 위해서다.
모델은 model_registry 테이블의 활성 행을 reload_interval_s(기본 60초)마다 확인해
버전이 바뀌면 다시 읽는다. 활성 모델이나 피처 함수가 없거나, 피처 계산/예측이
실패하면 휴리스틱 점수로 대신한다.
"""
import importlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Protocol, Sequence

import numpy as np
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.recsys.metrics import RecsysCounters
from app.recsys.scoring import Scorer
from app.recsys.types import Item, ScoreResult, UserState

logger = logging.getLogger(__name__)

FeatureFn = Callable[[UserState, Sequence[Item], datetime], np.ndarray]


@dataclass(frozen=True)
class RegisteredModel:
    name: str
    version: str
    model_text: str
    feature_names: Optional[List[str]] = None


class ModelSource(Protocol):
    def active_version(self, name: str) -> Optional[str]: ...

    def load(self, name: str, version: str) -> Optional[RegisteredModel]: ...


class SqlModelSource:
    def __init__(self, engine: Engine):
        self.engine = engine

    def active_version(self, name: str) -> Optional[str]:
        with self.engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT model_version FROM model_registry "
                    "WHERE model_name = :n AND is_active LIMIT 1"
                ),
                {"n": name},
            ).scalar()

    def load(self, name: str, version: str) -> Optional[RegisteredModel]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT model_text, feature_names FROM model_registry "
                    "WHERE model_name = :n AND model_version = :v AND model_format = 'lightgbm_text'"
                ),
                {"n": name, "v": version},
            ).first()
        if row is None:
            return None
        return RegisteredModel(name, version, row[0], row[1])


@dataclass(frozen=True)
class _Loaded:
    version: str
    booster: object
    num_features: int
    feature_names: Optional[List[str]]


def resolve_feature_fn(dotted: Optional[str]) -> Optional[FeatureFn]:
    if not dotted:
        return None
    module_name, _, attr = dotted.partition(":")
    try:
        return getattr(importlib.import_module(module_name), attr)
    except Exception:
        logger.exception("RECSYS_FEATURE_FN=%s could not be imported; using heuristic scorer", dotted)
        return None


class LightGBMScorer:
    def __init__(
        self,
        source: ModelSource,
        fallback: Scorer,
        feature_fn: Optional[FeatureFn] = None,
        model_name: str = "ranker",
        reload_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        counters: Optional[RecsysCounters] = None,
    ):
        self.source = source
        self.fallback = fallback
        self.feature_fn = feature_fn
        self.model_name = model_name
        self.reload_interval_s = reload_interval_s
        self.clock = clock
        self.counters = counters or RecsysCounters()
        self._current: Optional[_Loaded] = None
        self._last_check: Optional[float] = None
        self._reload_lock = threading.Lock()

    def maybe_reload(self) -> None:
        now = self.clock()
        if self._last_check is not None and now - self._last_check < self.reload_interval_s:
            return
        # 동시에 들어온 요청 중 하나만 레지스트리를 확인하고, 나머지는 현재 모델로 진행한다.
        if not self._reload_lock.acquire(blocking=False):
            return
        try:
            self._last_check = now
            version = self.source.active_version(self.model_name)
            if version is None:
                if self._current is not None:
                    logger.info("model %s deactivated; using heuristic", self.model_name)
                self._current = None
                return
            if self._current is not None and self._current.version == version:
                return
            record = self.source.load(self.model_name, version)
            if record is None:
                raise LookupError(f"{self.model_name}@{version} has no lightgbm_text row")
            import lightgbm as lgb

            booster = lgb.Booster(model_str=record.model_text)
            self._current = _Loaded(version, booster, booster.num_feature(), record.feature_names)
            logger.info("loaded model %s@%s", self.model_name, version)
        except Exception:
            logger.exception("model registry reload failed; keeping current model")
            self.counters.inc("scorer.reload_error")
        finally:
            self._reload_lock.release()

    def score(self, state: UserState, items: Sequence[Item], now: datetime) -> ScoreResult:
        # 피처 함수가 주입되지 않았으면 모델이 있어도 쓸 수 없으니 레지스트리 조회도 생략한다.
        if self.feature_fn is None:
            return self.fallback.score(state, items, now)
        self.maybe_reload()
        model = self._current
        if model is None:
            return self.fallback.score(state, items, now)

        fn_names = getattr(self.feature_fn, "feature_names", None)
        if model.feature_names and fn_names and list(fn_names) != list(model.feature_names):
            self.counters.inc("scorer.feature_mismatch")
            return self.fallback.score(state, items, now)
        try:
            X = np.asarray(self.feature_fn(state, items, now), dtype=np.float64)
            if X.shape != (len(items), model.num_features):
                raise ValueError(
                    f"feature matrix {X.shape} != ({len(items)}, {model.num_features})"
                )
            scores = np.asarray(model.booster.predict(X), dtype=np.float64)
        except Exception:
            logger.exception("lightgbm scoring failed; using heuristic for this request")
            self.counters.inc("scorer.lgbm_error")
            return self.fallback.score(state, items, now)
        return ScoreResult(scores=scores, model_version=f"lgbm:{self.model_name}@{model.version}")
