"""
LangGraph Node Functions
Each function takes state and returns state updates
"""
from typing import Dict, Any
from workflow.state import AgentState
from workflow.evaluators import ClusterEvaluator, NewsletterEvaluator
from core.reconstructor import NewsReconstructor, save_news_letter
from core.clusterer import get_cluster_articles
from db.connection import get_connection
from config.settings import Settings


def initialize_cluster_processing(state: AgentState) -> Dict[str, Any]:
    """
    Initialize processing for the current cluster.
    Loads articles for the current cluster from the data dict.
    """
    """
    Initialize processing for the current cluster.
    Loads articles for the current cluster from the data dict.
    """
    cluster_id = state["current_cluster_id"]
    
    # If articles are already provided (e.g. from main), use them
    if state.get("current_articles"):
        articles = state["current_articles"]
        article_ids = state["current_article_ids"]
    else:
        # Load from all_cluster_groups
        article_ids = state["all_cluster_groups"].get(cluster_id, [])
        articles = get_cluster_articles(cluster_id, article_ids, state["data"])
    
    # print(f"\n{'='*60}")
    # print(f"[Cluster {cluster_id}] Processing {len(articles)} articles...")
    # print(f"{'='*60}")
    
    return {
        "current_cluster_id": cluster_id,
        "current_article_ids": article_ids,
        "current_articles": articles,
        "cluster_eval": None,
        "cluster_retry_count": 0,
        "newsletter_draft": None,
        "newsletter_eval": None,
        "newsletter_retry_count": 0,
        "newsletter_feedback": None,
        "should_continue": True,
        "error_message": None
    }


def evaluate_cluster(state: AgentState) -> Dict[str, Any]:
    """
    Evaluate if the current cluster represents a coherent news event.
    Uses LLM to analyze article titles and content.
    """
    articles = state["current_articles"]
    cluster_id = state["current_cluster_id"]
    
    # print(f"  [Step 1] Evaluating cluster coherence...")
    
    evaluator = ClusterEvaluator(model=Settings.OPENAI_MODEL)
    result = evaluator.evaluate(articles)
    
    # print(f"    Decision: {result['decision']} (confidence: {result['confidence']:.2f})")
    # if result['summary']:
    #     print(f"    Summary: {result['summary']}")
    # if result['feedback']:
    #     print(f"    Feedback: {result['feedback']}")
    
    return {
        "cluster_eval": result,
        "cluster_retry_count": state["cluster_retry_count"] + 1
    }


def handle_cluster_eval_failure(state: AgentState) -> Dict[str, Any]:
    """
    Handle cluster evaluation failure by removing outliers and retrying.
    If no outliers or max retries reached, skip the cluster.
    """
    cluster_id = state["current_cluster_id"]
    eval_result = state.get("cluster_eval", {})
    outlier_indices = eval_result.get("outlier_indices", [])
    retry_count = state.get("cluster_retry_count", 0)
    
    # Constraint: limit retries (e.g. 2 times)
    MAX_RETRIES = 2
    if retry_count >= MAX_RETRIES:
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        # print(f"  [!] Cluster {cluster_id} skipped after max refinement retries")
        return {"skipped_clusters": skipped}
        
    # If no outliers identified, cannot refine
    if not outlier_indices:
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        # print(f"  [!] Cluster {cluster_id} skipped (no outliers to remove)")
        return {"skipped_clusters": skipped}
    
    # Remove outliers
    current_articles = state["current_articles"]
    # Filter valid indices
    valid_indices = [i for i in outlier_indices if 0 <= i < len(current_articles)]
    
    if not valid_indices:
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        return {"skipped_clusters": skipped}

    # Create new article list
    # Use set for faster lookups if many
    outlier_set = set(valid_indices)
    new_articles = [art for i, art in enumerate(current_articles) if i not in outlier_set]
    
    # Check minimum articles
    if len(new_articles) < 3:
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        # print(f"  [!] Cluster {cluster_id} skipped (too few articles after refinement)")
        return {"skipped_clusters": skipped}
    
    print(f"♻️  [Cluster {cluster_id}] Refining: Removed {len(valid_indices)} outliers, retrying with {len(new_articles)} articles")
    
    return {
        "current_articles": new_articles,
        # "cluster_retry_count": retry_count + 1 # Update this in eval_cluster or here? 
        # eval_cluster increments it, but we need to track *refinement* count vs *eval call* count.
        # state's cluster_retry_count is incremented in eval_cluster. 
        # But here we want to retry. Let's rely on state. 
        # Actually eval_cluster increments it blindly. 
        # If we return normally, flow goes to route.
        # We need a way to signal "Retry" to the router.
        # The router will check if we still have the cluster (not skipped).
    }


def route_after_cluster_fail(state: AgentState) -> str:
    """Route after fail handling: retry or end?"""
    # If added to skipped_clusters, then we gave up
    skipped = state.get("skipped_clusters", [])
    if state["current_cluster_id"] in skipped:
        return "end"
    
    return "retry"


def generate_newsletter(state: AgentState) -> Dict[str, Any]:
    """
    Generate newsletter draft from cluster articles.
    Includes feedback from previous evaluation if available.
    """
    articles = state["current_articles"]
    feedback = state.get("newsletter_feedback")
    retry_count = state.get("newsletter_retry_count", 0)
    
    # print(f"  [Step 2] Generating newsletter (attempt {retry_count + 1})...")
    # if feedback:
    #     print(f"    Previous feedback: {feedback[:100]}...")
    
    reconstructor = NewsReconstructor(model=Settings.OPENAI_MODEL)
    draft = reconstructor.reconstruct(articles, feedback=feedback)
    
    # if draft:
    #     print(f"    Title: {draft.get('title', '')}")
    # else:
    #     print(f"    [!] Newsletter generation failed")
    
    return {
        "newsletter_draft": draft,
        "newsletter_retry_count": retry_count + 1
    }


def evaluate_newsletter(state: AgentState) -> Dict[str, Any]:
    """
    Evaluate newsletter quality.
    Checks title format, objectivity, structure, etc.
    """
    draft = state["newsletter_draft"]
    articles = state["current_articles"]
    
    # print(f"  [Step 3] Evaluating newsletter quality...")
    
    if not draft:
        return {
            "newsletter_eval": {
                "decision": "FAIL",
                "score": 0,
                "feedback": "No draft to evaluate",
                "issues": ["Generation failed"]
            }
        }
    
    evaluator = NewsletterEvaluator(model=Settings.OPENAI_MODEL)
    result = evaluator.evaluate(draft, articles)
    
    # print(f"    Decision: {result['decision']} (score: {result['score']}/10)")
    # if result['issues']:
    #     print(f"    Issues: {', '.join(result['issues'][:3])}")
    
    # Accumulate feedback for potential retry
    new_feedback = state.get("newsletter_feedback", "") or ""
    if result["decision"] == "FAIL" and result["feedback"]:
        new_feedback = f"{new_feedback}\n\nAttempt {state['newsletter_retry_count']} feedback:\n{result['feedback']}"
    
    return {
        "newsletter_eval": result,
        "newsletter_feedback": new_feedback if result["decision"] == "FAIL" else None
    }


def save_newsletter_to_db(state: AgentState) -> Dict[str, Any]:
    """
    Save approved newsletter to database.
    Updates news_raw.news_letter_id for associated articles.
    Optionally generates newsletter embedding.
    """
    draft = state["newsletter_draft"]
    article_ids = state["current_article_ids"]
    cluster_id = state["current_cluster_id"]
    
    # print(f"  [Step 4] Saving newsletter to database...")
    
    try:
        conn = get_connection()
        saved_id = save_news_letter(conn, article_ids, draft)
        
        # print(f"    ✅ Saved as news_letter_id: {saved_id}")
        
        # Try to generate newsletter embedding (optional)
        try:
            from core.reconstructor import generate_newsletter_embedding
            content = f"{draft.get('title', '')} {draft.get('sentence', '')} {draft.get('content', '')}"
            if generate_newsletter_embedding(conn, saved_id, content):
                pass # print(f"    📐 Newsletter embedding generated")
        except Exception as e:
            pass # print(f"    ⚠️ Embedding skipped: {e}")
        
        conn.close()
        
        completed = list(state.get("completed_newsletters", []))
        completed.append(saved_id)
        
        return {
            "completed_newsletters": completed
        }
    
    except Exception as e:
        # print(f"    ❌ Save failed: {e}")
        failed = list(state.get("failed_clusters", []))
        failed.append(cluster_id)
        
        return {
            "failed_clusters": failed,
            "error_message": str(e)
        }


def handle_newsletter_max_retries(state: AgentState) -> Dict[str, Any]:
    """
    Handle case when newsletter generation exceeds max retries.
    """
    cluster_id = state["current_cluster_id"]
    failed = list(state.get("failed_clusters", []))
    failed.append(cluster_id)
    
    # print(f"  [!] Cluster {cluster_id} failed after max retries")
    
    last_feedback = state.get("newsletter_feedback", "No feedback")
    return {
        "failed_clusters": failed,
        "error_message": f"Max retries reached. Feedback: {last_feedback}"
    }


def finalize_workflow(state: AgentState) -> Dict[str, Any]:
    """
    Final node that summarizes the workflow results.
    """
    completed = state.get("completed_newsletters", [])
    failed = state.get("failed_clusters", [])
    skipped = state.get("skipped_clusters", [])
    
    # print(f"\n{'='*60}")
    # print("WORKFLOW COMPLETE")
    # print(f"{'='*60}")
    # print(f"  Completed: {len(completed)} newsletters")
    # print(f"  Failed: {len(failed)} clusters")
    # print(f"  Skipped: {len(skipped)} clusters")
    
    return {"should_continue": False}


# ========== Routing Functions ==========

def should_continue_processing(state: AgentState) -> str:
    """Route after initialize: continue or end?"""
    if state["current_cluster_index"] >= len(state["all_cluster_ids"]):
        return "end"
    return "continue"


def route_after_cluster_eval(state: AgentState) -> str:
    """Route after cluster evaluation: pass or fail?"""
    eval_result = state.get("cluster_eval", {})
    
    if eval_result.get("decision") == "PASS":
        return "pass"
    
    # Could add logic here to retry with outlier removal
    return "fail"


def route_after_newsletter_eval(state: AgentState) -> str:
    """Route after newsletter evaluation: pass, retry, or max_retries?"""
    eval_result = state.get("newsletter_eval", {})
    retry_count = state.get("newsletter_retry_count", 0)
    
    if eval_result.get("decision") == "PASS":
        return "pass"
    
    if retry_count >= Settings.MAX_RETRY_NEWSLETTER_EVAL:
        return "max_retries"
    
    return "retry"
