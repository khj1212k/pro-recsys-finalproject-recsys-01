from fastapi import APIRouter, Depends

from app.recsys.runtime import get_recommendation_service
from app.recsys.service import RecommendationService

router = APIRouter(prefix="/recsys", tags=["recsys"])


@router.get("/stats")
def get_recsys_stats(service: RecommendationService = Depends(get_recommendation_service)):
    """이 워커 프로세스의 추천 카운터(출처별 응답 수, 폴백 사유, 캐시 적중 등).
    uvicorn 워커가 여러 개면 워커마다 값이 따로다."""
    return {
        "mode": service.cfg.mode,
        "counters": service.counters.snapshot(),
        "cache_entries": len(service.cache),
    }
