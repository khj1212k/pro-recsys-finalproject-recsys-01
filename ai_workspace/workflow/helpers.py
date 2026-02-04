# 워크플로우 헬퍼 함수
# - 생성 이력 관리, 아웃라이어 처리 등 공통 로직
from typing import Dict, Any, Optional, List


def initialize_generation_history(state: Dict[str, Any]) -> Dict[str, List]:
    generation_history = state.get("generation_history")
    if generation_history is None:
        generation_history = {"attempts": []}
    return generation_history


def extract_feedback(state: Dict[str, Any]) -> Optional[str]:
    newsletter_eval = state.get("newsletter_eval")
    if newsletter_eval and newsletter_eval.get("feedback"):
        return newsletter_eval["feedback"]
    return None


def log_generation_attempt(
    history: Dict[str, List],
    draft: Optional[Dict[str, Any]],
    state: Dict[str, Any]
) -> None:
    attempt_log = {
        "attempt_number": state.get("newsletter_retry_count", 0) + 1,
        "draft_title": draft.get("title") if draft else None,
        "evaluation": state.get("newsletter_eval"),
    }
    history["attempts"].append(attempt_log)


def get_outlier_indices(articles: List[Dict], cluster_eval: Dict) -> List[int]:
    if not cluster_eval:
        return []
    
    outlier_indices = cluster_eval.get("outlier_indices", [])
    
    # Validate indices
    valid_indices = [
        idx for idx in outlier_indices 
        if 0 <= idx < len(articles)
    ]
    
    return valid_indices


def remove_outliers(articles: List[Dict], indices_to_remove: List[int]) -> List[Dict]:
    if not indices_to_remove:
        return articles
    
    return [
        article for idx, article in enumerate(articles)
        if idx not in indices_to_remove
    ]


def format_refine_message(removed_count: int, remaining_count: int) -> str:
    return f"Removed {removed_count} outliers, retrying with {remaining_count} articles"
