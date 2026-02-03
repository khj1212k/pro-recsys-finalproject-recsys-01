"""
Helper functions for workflow nodes

These functions extract common logic from workflow nodes to improve
readability and maintainability. They handle state manipulation,
history tracking, and data extraction.
"""
from typing import Dict, Any, Optional, List


def initialize_generation_history(state: Dict[str, Any]) -> Dict[str, List]:
    """
    Initialize or retrieve generation history from state.
    
    Args:
        state: Current workflow state
        
    Returns:
        Dict with 'attempts' key containing list of generation attempts
    """
    generation_history = state.get("generation_history")
    if generation_history is None:
        generation_history = {"attempts": []}
    return generation_history


def extract_feedback(state: Dict[str, Any]) -> Optional[str]:
    """
    Extract feedback from previous evaluation.
    
    Args:
        state: Current workflow state
        
    Returns:
        Feedback string or None if no feedback available
    """
    newsletter_eval = state.get("newsletter_eval")
    if newsletter_eval and newsletter_eval.get("feedback"):
        return newsletter_eval["feedback"]
    return None


def log_generation_attempt(
    history: Dict[str, List],
    draft: Optional[Dict[str, Any]],
    state: Dict[str, Any]
) -> None:
    """
    Log a generation attempt to history.
    
    Args:
        history: Generation history dict
        draft: Generated newsletter draft
        state: Current workflow state (for retry count, eval info)
    """
    attempt_log = {
        "attempt_number": state.get("newsletter_retry_count", 0) + 1,
        "draft_title": draft.get("title") if draft else None,
        "evaluation": state.get("newsletter_eval"),
    }
    history["attempts"].append(attempt_log)


def get_outlier_indices(articles: List[Dict], cluster_eval: Dict) -> List[int]:
    """
    Extract outlier indices from cluster evaluation.
    
    Args:
        articles: List of articles in cluster
        cluster_eval: Cluster evaluation result
        
    Returns:
        List of indices to remove as outliers
    """
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
    """
    Remove outlier articles by index.
    
    Args:
        articles: Original list of articles
        indices_to_remove: Indices of articles to remove
        
    Returns:
        Filtered list with outliers removed
    """
    if not indices_to_remove:
        return articles
    
    return [
        article for idx, article in enumerate(articles)
        if idx not in indices_to_remove
    ]


def format_refine_message(removed_count: int, remaining_count: int) -> str:
    """
    Format a user-friendly refine message.
    
    Args:
        removed_count: Number of outliers removed
        remaining_count: Number of articles remaining
        
    Returns:
        Formatted message string
    """
    return f"Removed {removed_count} outliers, retrying with {remaining_count} articles"
