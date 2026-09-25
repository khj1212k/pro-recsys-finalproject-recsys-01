"""FastAPI 의존성으로 쓰는 프로세스 단위 싱글턴 조립부."""
import threading
from functools import partial
from typing import Optional

from sqlalchemy.engine import Engine

from app.recsys.config import RecsysConfig
from app.recsys.metrics import RecsysCounters
from app.recsys.scoring import HeuristicScorer
from app.recsys.service import RecommendationService, build_service

_service: Optional[RecommendationService] = None
_lock = threading.Lock()


def build_sql_service(
    cfg: RecsysConfig, app_engine: Engine, database_url: str
) -> RecommendationService:
    from app.recsys.lgbm_scorer import LightGBMScorer, SqlModelSource, resolve_feature_fn
    from app.recsys.sql_repository import (
        SqlImpressionWriter,
        create_recsys_engine,
        sql_repo_scope,
    )

    counters = RecsysCounters()
    scorer = LightGBMScorer(
        SqlModelSource(app_engine),
        fallback=HeuristicScorer(),
        feature_fn=resolve_feature_fn(cfg.feature_fn),
        model_name=cfg.model_name,
        reload_interval_s=cfg.model_reload_s,
        counters=counters,
    )
    recsys_engine = create_recsys_engine(database_url, cfg.workers, cfg.time_budget_ms)
    service = build_service(
        cfg,
        repo_factory=partial(sql_repo_scope, recsys_engine, cfg.time_budget_ms),
        scorer=scorer,
        impression_writer=SqlImpressionWriter(app_engine),
        counters=counters,
    )
    service.add_shutdown_hook(recsys_engine.dispose)
    return service


def _build_default_service() -> RecommendationService:
    from app.database import DATABASE_URL, engine

    return build_sql_service(RecsysConfig.from_env(), engine, DATABASE_URL)


def get_recommendation_service() -> RecommendationService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = _build_default_service()
    return _service
