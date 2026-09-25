"""FastAPI 의존성으로 쓰는 프로세스 단위 싱글턴 조립부."""
import threading
from functools import partial
from typing import Optional

from app.recsys.config import RecsysConfig
from app.recsys.service import RecommendationService, build_service

_service: Optional[RecommendationService] = None
_lock = threading.Lock()


def _build_default_service() -> RecommendationService:
    from app.database import engine
    from app.recsys.sql_repository import SqlImpressionWriter, sql_repo_scope

    cfg = RecsysConfig.from_env()
    return build_service(
        cfg,
        repo_factory=partial(sql_repo_scope, engine, cfg.time_budget_ms),
        impression_writer=SqlImpressionWriter(engine),
    )


def get_recommendation_service() -> RecommendationService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = _build_default_service()
    return _service
