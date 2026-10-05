"""FastAPI 의존성으로 쓰는 프로세스 단위 싱글턴 조립부."""
import threading
from functools import partial
from typing import Optional

from app.recsys.config import RecsysConfig
from app.recsys.metrics import RecsysCounters
from app.recsys.scoring import HeuristicScorer, ScorerStack
from app.recsys.service import RecommendationService, build_service

_service: Optional[RecommendationService] = None
_lock = threading.Lock()


def build_sql_service(cfg: RecsysConfig, database_url: str) -> RecommendationService:
    """운영 배선. 앱 풀(app.database.engine)은 요청 세션(인증, 폴백 체인, 표시 단계)만 쓰고
    여기서 만드는 것은 전부 자기 풀을 쓴다 - 요청 하나가 앱 풀 커넥션을 1개만 쓰도록.

    - 실시간 계산: 작업 스레드 수만큼의 전용 풀(create_recsys_engine)
    - 모델 레지스트리 확인(백그라운드 스레드)과 요청·칸 로그 쓰기(응답 뒤): 보조 풀(create_aux_engine)
    """
    from app.recsys.lgbm_scorer import SqlModelSource, resolve_feature_fn
    from app.recsys.sql_repository import (
        SqlImpressionWriter,
        create_aux_engine,
        create_recsys_engine,
        sql_repo_scope,
    )

    counters = RecsysCounters()
    aux_engine = create_aux_engine(database_url)
    scorer = build_scorer_stack(cfg, SqlModelSource(aux_engine), resolve_feature_fn(cfg.feature_fn), counters)
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
    # 첫 요청이 오기 전에 레지스트리의 모델을 읽기 시작한다(백그라운드라 기동을 막지 않는다).
    # 피처 함수가 없으면 모델이 있어도 쓸 수 없으므로 조회하지 않는다.
    for model_scorer in (scorer.active, *scorer.shadows):
        if model_scorer.feature_fn is not None:
            model_scorer.maybe_reload()
    return service


def build_scorer_stack(cfg: RecsysConfig, source, feature_fn, counters: RecsysCounters) -> ScorerStack:
    """활성 스코어러 하나 + shadow 스코어러들 (ADR 0025).

    - 활성: 레지스트리에 role='active' 모델이 있고 피처 함수가 있으면 그 모델, 아니면 4항 휴리스틱.
    - shadow: 레지스트리의 role='shadow' 모델 중 최신 shadow_max개. 같은 후보에 점수만 매겨 칸 로그에
      남긴다. 피처 함수가 없으면 어떤 모델도 점수를 낼 수 없으므로 만들지 않는다.
    """
    from app.recsys.lgbm_scorer import LightGBMScorer

    def model_scorer(**kwargs) -> "LightGBMScorer":
        return LightGBMScorer(
            source,
            feature_fn=feature_fn,
            model_name=cfg.model_name,
            reload_interval_s=cfg.model_reload_s,
            counters=counters,
            reload_in_background=True,
            **kwargs,
        )

    active = model_scorer(fallback=HeuristicScorer())
    shadows = []
    if feature_fn is not None:
        shadows = [model_scorer(fallback=None, role="shadow", slot=i) for i in range(cfg.shadow_max)]
    return ScorerStack(
        active, shadows, deadline_fraction=cfg.shadow_deadline_fraction, counters=counters
    )


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
