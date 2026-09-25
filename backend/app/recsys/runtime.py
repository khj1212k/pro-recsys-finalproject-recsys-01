"""FastAPI 의존성으로 쓰는 프로세스 단위 싱글턴 조립부."""
import threading
from functools import partial
from typing import Optional

from app.recsys.config import RecsysConfig
from app.recsys.metrics import RecsysCounters
from app.recsys.scoring import HeuristicScorer
from app.recsys.service import RecommendationService, build_service

_service: Optional[RecommendationService] = None
_lock = threading.Lock()


def _build_default_service() -> RecommendationService:
    from app.database import engine
    from app.recsys.lgbm_scorer import LightGBMScorer, SqlModelSource, resolve_feature_fn
    from app.recsys.sql_repository import SqlImpressionWriter, sql_repo_scope

    cfg = RecsysConfig.from_env()
    counters = RecsysCounters()
    scorer = LightGBMScorer(
        SqlModelSource(engine),
        fallback=HeuristicScorer(),
        feature_fn=resolve_feature_fn(cfg.feature_fn),
        model_name=cfg.model_name,
        reload_interval_s=cfg.model_reload_s,
        counters=counters,
    )
    return build_service(
        cfg,
        repo_factory=partial(sql_repo_scope, engine, cfg.time_budget_ms),
        scorer=scorer,
        impression_writer=SqlImpressionWriter(engine),
        counters=counters,
    )


def get_recommendation_service() -> RecommendationService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = _build_default_service()
    return _service
