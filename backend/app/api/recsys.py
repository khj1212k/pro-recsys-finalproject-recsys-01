from fastapi import APIRouter, Depends

from app.api.user_check import get_current_user
from app.recsys.runtime import get_recommendation_service
from app.recsys.service import RecommendationService

router = APIRouter(prefix="/recsys", tags=["recsys"])


@router.get("/stats", dependencies=[Depends(get_current_user)])
def get_recsys_stats(service: RecommendationService = Depends(get_recommendation_service)):
    """이 워커 프로세스의 추천 카운터(출처별 응답 수, 폴백 사유, 캐시 적중 등). 로그인한
    사용자만 볼 수 있다(사용자 데이터는 없지만 운영 상태를 익명에게 열어 둘 이유가 없다).

    프로세스 안의 값이라 재시작하면 0이 되고 uvicorn 워커가 여러 개면 워커마다 따로다 -
    지금 살아 있는 프로세스를 들여다보는 용도이지 폴백률·노출 로그 유실률의 근거가 아니다.
    기간별 출처 분포는 recommendation_impression_log에서 집계한다(jobs.run daily_report)."""
    return {
        "mode": service.cfg.mode,
        "counters": service.counters.snapshot(),
        "cache_entries": len(service.cache),
    }
