"""popularity: 인기도-최신성 랭킹 배치 (backend/scheduler/calculate_ranking.py, KST 하루 단위 UPSERT)."""
from typing import Any, Dict


def run(ctx) -> Dict[str, Any]:
    from scheduler.calculate_ranking import calculate_ranking

    return {"ranking": calculate_ranking()}
