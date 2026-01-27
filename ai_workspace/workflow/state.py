"""
LangGraph State Definitions
Defines the state schema that flows through the workflow graph
"""
from typing import TypedDict, List, Dict, Optional, Any


class ClusterEvaluation(TypedDict):
    """Result of cluster evaluation by LLM"""
    decision: str  # "PASS" or "FAIL"
    confidence: float
    summary: str
    feedback: str
    outlier_indices: List[int]


class NewsletterEvaluation(TypedDict):
    """Result of newsletter evaluation by LLM"""
    decision: str  # "PASS" or "FAIL"
    score: int  # 0-10
    feedback: str
    issues: List[str]


class AgentState(TypedDict):
    """Main state for the LangGraph workflow"""
    
    # ========== Global State ==========
    # All clusters to process
    all_cluster_groups: Dict[int, List[int]]  # {cluster_id: [article_ids]}
    all_cluster_ids: List[int]  # List of cluster IDs to process
    current_cluster_index: int  # Current index in all_cluster_ids

    # Data loaded from DB (shared)
    data: Dict[str, Any]  # {ids, titles, embeddings, press_names, contents}
    
    # Results tracking
    completed_newsletters: List[int]  # List of saved news_letter_ids
    failed_clusters: List[int]  # Cluster IDs that failed after max retries
    skipped_clusters: List[int]  # Cluster IDs skipped due to eval failure

    # ========== Per-Cluster State (reset each iteration) ==========
    current_cluster_id: int
    current_article_ids: List[int]
    current_articles: List[Dict]  # [{raw_news_id, title, press_name, content}]
    
    # Cluster evaluation
    cluster_eval: Optional[ClusterEvaluation]
    cluster_retry_count: int
    
    # Newsletter generation
    newsletter_draft: Optional[Dict]  # {title, sentence, content, keywords, categories}
    newsletter_eval: Optional[NewsletterEvaluation]
    newsletter_retry_count: int
    newsletter_feedback: Optional[str]  # Accumulated feedback for regeneration
    
    # Workflow control
    should_continue: bool  # Whether to continue to next cluster
    error_message: Optional[str]
