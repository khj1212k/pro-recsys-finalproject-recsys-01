"""LightGBM 랭커 어댑터 (ADR 0015).

피처 함수는 이 저장소가 아니라 런타임에 주입된다(RECSYS_FEATURE_FN="모듈:함수" 또는
생성자 인자) - 학습 파이프라인이 쓰는 피처 코드를 서빙에서 복제하지 않기 위해서다.
모델은 model_registry 테이블의 활성 행을 reload_interval_s(기본 60초)마다 확인해
버전이 바뀌면 다시 읽는다. 활성 모델이나 피처 함수가 없거나, 피처 계산/예측이
실패하면 휴리스틱 점수로 대신한다.

운영 배선(runtime.build_sql_service)은 reload_in_background=True로 쓴다: 레지스트리 조회와
lightgbm 임포트·모델 파싱을 데몬 스레드가 하고, 요청을 처리하는 작업 스레드는 그동안 현재
모델(없으면 휴리스틱)로 점수를 낸다. 요청 시간 예산 안에서 DB 풀이나 모델 로드를 기다리지
않는다.

피처와 스키마(ADR 0033): 운영의 피처 함수는 recsys_core.serving:features(서빙 어댑터)다. 모델에 열 이름이나
스키마 지문이 등록돼 있고 피처 함수의 것과 다르면 그 모델로 점수를 내지 않는다(이름은 같은데 정의가 달라진
피처로 예전 모델이 점수를 내는 일을 막는다). 점수를 낼 때 쓴 피처 행렬은 ScoreResult.features로 함께 나간다.
묶음(ScorerStack)이 같은 후보의 피처 행렬을 이미 계산했으면 features 인자로 받아 다시 계산하지 않는다.

역할(ADR 0025): role="active"는 레지스트리의 활성 행을 읽어 목록을 만드는 점수를 낸다. role="shadow"는
같은 이름의 shadow 행 중 최신에서 slot번째를 읽고, 폴백을 두지 않는다(fallback=None) - 모델이 없거나
피처가 맞지 않으면 휴리스틱 점수를 대신 내는 것이 아니라 ScorerUnavailable로 "이번에는 점수 없음"을 알린다.
shadow가 휴리스틱 점수를 자기 이름으로 남기면 로그가 거짓이 된다.
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
from app.recsys.scoring import FeatureFn, Scorer, ScorerUnavailable
from app.recsys.types import Item, ScoreResult, UserState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RegisteredModel:
    name: str
    version: str
    model_text: str
    feature_names: Optional[List[str]] = None
    feature_schema_hash: Optional[str] = None


class ModelSource(Protocol):
    def active_version(self, name: str) -> Optional[str]: ...

    def load(self, name: str, version: str) -> Optional[RegisteredModel]: ...

    def shadow_versions(self, name: str, limit: int) -> List[str]:
        """role='shadow'인 버전을 최신 등록순으로 최대 limit개."""
        ...


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

    def shadow_versions(self, name: str, limit: int) -> List[str]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT model_version FROM model_registry "
                    "WHERE model_name = :n AND role = 'shadow' AND model_format = 'lightgbm_text' "
                    "ORDER BY created_at DESC, model_id DESC LIMIT :k"
                ),
                {"n": name, "k": limit},
            ).fetchall()
        return [r[0] for r in rows]

    def load(self, name: str, version: str) -> Optional[RegisteredModel]:
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT model_text, feature_names, feature_schema_hash FROM model_registry "
                    "WHERE model_name = :n AND model_version = :v AND model_format = 'lightgbm_text'"
                ),
                {"n": name, "v": version},
            ).first()
        if row is None:
            return None
        return RegisteredModel(name, version, row[0], row[1], row[2])


@dataclass(frozen=True)
class _Loaded:
    version: str
    booster: object
    num_features: int
    feature_names: Optional[List[str]]
    feature_schema_hash: Optional[str] = None


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
        fallback: Optional[Scorer],
        feature_fn: Optional[FeatureFn] = None,
        model_name: str = "ranker",
        reload_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        counters: Optional[RecsysCounters] = None,
        reload_in_background: bool = False,
        role: str = "active",
        slot: int = 0,
    ):
        if role not in ("active", "shadow"):
            raise ValueError(f"role must be 'active' or 'shadow', got {role!r}")
        self.source = source
        self.fallback = fallback
        self.role = role
        self.slot = slot
        self.feature_fn = feature_fn
        self.model_name = model_name
        self.reload_interval_s = reload_interval_s
        self.reload_in_background = reload_in_background
        self.clock = clock
        self.counters = counters or RecsysCounters()
        self._current: Optional[_Loaded] = None
        self._last_check: Optional[float] = None
        self._reload_lock = threading.Lock()

    def maybe_reload(self) -> None:
        """레지스트리를 확인할 때가 됐으면 확인한다. reload_in_background면 확인·로드를 데몬
        스레드에 넘기고 바로 돌아온다(호출한 요청은 현재 모델로 진행)."""
        now = self.clock()
        if self._last_check is not None and now - self._last_check < self.reload_interval_s:
            return
        # 동시에 들어온 요청 중 하나만 레지스트리를 확인하고, 나머지는 현재 모델로 진행한다.
        if not self._reload_lock.acquire(blocking=False):
            return
        self._last_check = now
        if not self.reload_in_background:
            self._reload_and_release()
            return
        try:
            threading.Thread(
                target=self._reload_and_release, name="recsys-model-reload", daemon=True
            ).start()
        except Exception:
            self._reload_lock.release()
            logger.exception("could not start the model reload thread; keeping current model")
            self.counters.inc("scorer.reload_error")

    def _wanted_version(self) -> Optional[str]:
        if self.role == "active":
            return self.source.active_version(self.model_name)
        versions = self.source.shadow_versions(self.model_name, self.slot + 1)
        return versions[self.slot] if len(versions) > self.slot else None

    def _without_model(self, state: UserState, items: Sequence[Item], now: datetime, why: str) -> ScoreResult:
        if self.fallback is None:
            raise ScorerUnavailable(why)
        return self.fallback.score(state, items, now)

    def _reload_and_release(self) -> None:
        try:
            version = self._wanted_version()
            if version is None:
                if self._current is not None:
                    logger.info("model %s (%s) is no longer registered", self.model_name, self.role)
                self._current = None
                return
            if self._current is not None and self._current.version == version:
                return
            record = self.source.load(self.model_name, version)
            if record is None:
                raise LookupError(f"{self.model_name}@{version} has no lightgbm_text row")
            import lightgbm as lgb

            booster = lgb.Booster(model_str=record.model_text)
            self._current = _Loaded(
                version, booster, booster.num_feature(), record.feature_names,
                getattr(record, "feature_schema_hash", None),
            )
            logger.info("loaded model %s@%s", self.model_name, version)
        except Exception:
            logger.exception("model registry reload failed; keeping current model")
            self.counters.inc("scorer.reload_error")
        finally:
            self._reload_lock.release()

    @property
    def needs_features(self) -> bool:
        """지금 피처 함수로 점수를 낼 상태인가(피처 함수가 있고 모델이 올라와 있다)."""
        return self.feature_fn is not None and self._current is not None

    def score(
        self,
        state: UserState,
        items: Sequence[Item],
        now: datetime,
        features: Optional[np.ndarray] = None,
    ) -> ScoreResult:
        # 피처 함수가 주입되지 않았으면 모델이 있어도 쓸 수 없으니 레지스트리 조회도 생략한다.
        if self.feature_fn is None:
            return self._without_model(state, items, now, "no feature function")
        self.maybe_reload()
        model = self._current
        if model is None:
            return self._without_model(state, items, now, "no registered model")

        fn_names = getattr(self.feature_fn, "feature_names", None)
        if model.feature_names and fn_names and list(fn_names) != list(model.feature_names):
            self.counters.inc("scorer.feature_mismatch")
            return self._without_model(state, items, now, "feature names differ from the model's")
        fn_hash = getattr(self.feature_fn, "schema_hash", None)
        if model.feature_schema_hash and fn_hash and fn_hash != model.feature_schema_hash:
            self.counters.inc("scorer.schema_mismatch")
            return self._without_model(state, items, now, "feature schema hash differs from the model's")
        try:
            # 하네스처럼 피처 함수가 낸 행렬(어댑터는 float32)을 그대로 넘긴다.
            X = np.asarray(self.feature_fn(state, items, now) if features is None else features)
            if X.shape != (len(items), model.num_features):
                raise ValueError(
                    f"feature matrix {X.shape} != ({len(items)}, {model.num_features})"
                )
            scores = np.asarray(model.booster.predict(X), dtype=np.float64)
        except Exception:
            self.counters.inc("scorer.lgbm_error")
            if self.fallback is None:
                raise  # shadow: 묶음(ScorerStack)이 삼키고 센다
            logger.exception("lightgbm scoring failed; using heuristic for this request")
            return self.fallback.score(state, items, now)
        # 해석 버전이 없는 피처 함수의 행렬은 로그에 남기지 않는다(풀 방법이 없는 바이트가 된다).
        schema_version = getattr(self.feature_fn, "schema_version", None)
        return ScoreResult(
            scores=scores,
            model_version=f"lgbm:{self.model_name}@{model.version}",
            features=None if schema_version is None else X.astype(np.float32, copy=False),
            feature_schema_version=schema_version,
        )
