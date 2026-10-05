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


def build_sql_service(cfg: RecsysConfig, database_url: str) -> RecommendationService:
    """운영 배선. 앱 풀(app.database.engine)은 요청 세션(인증, 폴백 체인, 표시 단계)만 쓰고
    여기서 만드는 것은 전부 자기 풀을 쓴다 - 요청 하나가 앱 풀 커넥션을 1개만 쓰도록.

    - 실시간 계산: 작업 스레드 수만큼의 전용 풀(create_recsys_engine)
    - 모델 레지스트리 확인(백그라운드 스레드)과 노출 로그 쓰기(응답 뒤): 보조 풀(create_aux_engine)
    """
    from app.recsys.lgbm_scorer import LightGBMScorer, SqlModelSource, resolve_feature_fn
    from app.recsys.sql_repository import (
        SqlImpressionWriter,
        create_aux_engine,
        create_recsys_engine,
        sql_repo_scope,
    )

    counters = RecsysCounters()
    aux_engine = create_aux_engine(database_url)
    scorer = LightGBMScorer(
        SqlModelSource(aux_engine),
        fallback=HeuristicScorer(),
        feature_fn=resolve_feature_fn(cfg.feature_fn),
        model_name=cfg.model_name,
        reload_interval_s=cfg.model_reload_s,
        counters=counters,
        reload_in_background=True,
    )
    recsys_engine = create_recsys_engine(database_url, cfg.workers, cfg.time_budget_ms)
    service = build_service(
        cfg,
        repo_factory=partial(sql_repo_scope, recsys_engine, cfg.time_budget_ms),
        scorer=scorer,
        impression_writer=SqlImpressionWriter(aux_engine),
        counters=counters,
    )
    service.add_shutdown_hook(recsys_engine.dispose)
    service.add_shutdown_hook(aux_engine.dispose)
    if scorer.feature_fn is not None:
        # 첫 요청이 오기 전에 활성 모델을 읽기 시작한다(백그라운드라 기동을 막지 않는다).
        scorer.maybe_reload()
    return service


def _build_default_service() -> RecommendationService:
    from app.database import DATABASE_URL

    return build_sql_service(RecsysConfig.from_env(), DATABASE_URL)


def get_recommendation_service() -> RecommendationService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = _build_default_service()
    return _service


def startup() -> RecommendationService:
    """앱 기동 시(lifespan) 서비스를 미리 조립한다. RECSYS_MODE 오타 같은 설정 오류가 첫
    /newsletters/today 요청의 500이 아니라 기동 실패로 드러난다. DB 접속은 하지 않는다
    (엔진은 첫 사용 때 접속한다)."""
    return get_recommendation_service()


def shutdown() -> None:
    """작업 스레드 풀을 멈추고 전용·보조 커넥션 풀을 닫는다."""
    global _service
    with _lock:
        service, _service = _service, None
    if service is not None:
        service.shutdown()
