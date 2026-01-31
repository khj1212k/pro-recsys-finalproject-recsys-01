"""
LangGraph Node Functions
Each function takes state and returns state updates
"""
from typing import Dict, Any
import logging

from workflow.state import AgentState
from workflow.evaluators import ClusterEvaluator, NewsletterEvaluator
from core.reconstructor import NewsReconstructor, save_news_letter
from core.clusterer import get_cluster_articles
from core.tone_converter import ToneConverter
from core.embedder import NewsEmbedder
from db.connection import get_connection
from config.settings import Settings

logger = logging.getLogger(__name__)


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
        "generation_history": None,  # Reset for each cluster
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
    
    evaluator = ClusterEvaluator()
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
    from datetime import datetime
    
    articles = state["current_articles"]
    feedback = state.get("newsletter_feedback")
    retry_count = state.get("newsletter_retry_count", 0)
    
    # Initialize or get generation_history
    generation_history = state.get("generation_history")
    if generation_history is None:
        generation_history = {"attempts": []}
    
    # print(f"  [Step 2] Generating newsletter (attempt {retry_count + 1})...")
    # if feedback:
    #     print(f"    Previous feedback: {feedback[:100]}...")
    
    reconstructor = NewsReconstructor()
    draft = reconstructor.reconstruct(articles, feedback=feedback)
    
    # Log generation attempt
    attempt_log = {
        "attempt": retry_count + 1,
        "action": "generate" if retry_count == 0 else "regenerate",
        "timestamp": datetime.now().isoformat(),
        "feedback_applied": feedback if feedback else None
    }
    generation_history["attempts"].append(attempt_log)
    
    # if draft:
    #     print(f"    Title: {draft.get('title', '')}")
    # else:
    #     print(f"    [!] Newsletter generation failed")
    
    return {
        "newsletter_draft": draft,
        "newsletter_retry_count": retry_count + 1,
        "generation_history": generation_history
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
    
    evaluator = NewsletterEvaluator()
    result = evaluator.evaluate(draft, articles)
    
    # print(f"    Decision: {result['decision']} (score: {result['score']}/10)")
    # if result['issues']:
    #     print(f"    Issues: {', '.join(result['issues'][:3])}")
    
    # Accumulate feedback for potential retry
    new_feedback = state.get("newsletter_feedback", "") or ""
    if result["decision"] == "FAIL" and result["feedback"]:
        new_feedback = f"{new_feedback}\n\nAttempt {state['newsletter_retry_count']} feedback:\n{result['feedback']}"
    
    # Update generation_history with evaluation results
    generation_history = state.get("generation_history", {"attempts": []})
    if generation_history.get("attempts"):
        # Update the last attempt with evaluation results
        generation_history["attempts"][-1].update({
            "evaluation_score": result["score"],
            "evaluation_result": result["decision"].lower(),
            "evaluation_feedback": result["feedback"],
            "evaluation_issues": result.get("issues", [])
        })
    
    return {
        "newsletter_eval": result,
        "newsletter_feedback": new_feedback if result["decision"] == "FAIL" else None,
        "generation_history": generation_history
    }


def embed_newsletter_node(state: AgentState) -> Dict[str, Any]:
    """
    Generate embedding from the original (formal) newsletter text.
    This embedding will be used for the recommendation system.
    
    The embedding is created BEFORE tone conversion to ensure
    consistency with the formal writing style used in training.
    """
    draft = state["newsletter_draft"]
    
    logger.info("📐 원본 뉴스레터로 임베딩 생성 중...")
    
    if not draft:
        logger.error("❌ 뉴스레터 초안 없음")
        return {
            "newsletter_embedding": None,
            "error_message": "No draft for embedding"
        }
    
    try:
        # Create embedder instance
        embedder = NewsEmbedder(force_cpu=False, verbose=False, l2_normalize=True)
        
        # Combine title, summary, and content for embedding
        text_for_embedding = f"{draft.get('title', '')} {draft.get('sentence', '')} {draft.get('content', '')}"
        
        # Generate embedding
        embedding = embedder.generate_embedding(text_for_embedding)
        
        if embedding:
            logger.info(f"✅ 임베딩 생성 완료 (차원: {len(embedding)})")
        else:
            logger.warning("⚠️ 임베딩 생성 실패")
        
        # Save original newsletter for reference
        return {
            "original_newsletter": draft.copy(),  # Save original before conversion
            "newsletter_embedding": embedding
        }
        
    except Exception as e:
        logger.error(f"❌ 임베딩 생성 오류: {e}")
        return {
            "newsletter_embedding": None,
            "error_message": f"Embedding failed: {e}"
        }


def convert_tone_node(state: AgentState) -> Dict[str, Any]:
    """
    Convert newsletter tone from formal to casual with emojis.
    This converted version will be stored in the database for users.
    
    The original formal version is preserved in 'original_newsletter'
    and its embedding in 'newsletter_embedding' for the recommendation system.
    """
    draft = state["newsletter_draft"]
    
    logger.info("🎨 문체 변환 중...")
    
    if not draft:
        logger.error("❌ 뉴스레터 초안 없음")
        return {
            "converted_newsletter": None,
            "error_message": "No draft for conversion"
        }
    
    try:
        # Create tone converter
        converter = ToneConverter()
        
        # Convert tone
        converted = converter.convert(draft)
        
        if not converted:
            logger.warning("⚠️ 문체 변환 실패, 원본 사용")
            return {
                "converted_newsletter": draft.copy(),  # Fallback to original
                "conversion_feedback": "Conversion failed, using original"
            }
        
        # Validate conversion
        is_valid = converter.validate_conversion(draft, converted)
        
        if not is_valid:
            logger.warning("⚠️ 변환 검증 실패, 원본 사용")
            return {
                "converted_newsletter": draft.copy(),  # Fallback to original
                "conversion_feedback": "Validation failed, using original"
            }
        
        logger.info("✅ 문체 변환 완료")
        
        return {
            "converted_newsletter": converted,
            "conversion_feedback": "Conversion successful"
        }
        
    except Exception as e:
        logger.error(f"❌ 문체 변환 오류: {e}")
        # Fallback to original on error
        return {
            "converted_newsletter": draft.copy(),
            "conversion_feedback": f"Conversion error: {e}, using original",
            "error_message": f"Tone conversion failed: {e}"
        }


def save_newsletter_to_db(state: AgentState) -> Dict[str, Any]:
    """
    Save approved newsletter to database.
    Uses the CONVERTED (casual tone) newsletter for content
    and the pre-generated embedding from ORIGINAL (formal tone) text.
    Updates news_raw.news_letter_id for associated articles.
    """
    # Use converted newsletter if available, otherwise fall back to draft
    newsletter_to_save = state.get("converted_newsletter") or state["newsletter_draft"]
    embedding = state.get("newsletter_embedding")  # Pre-generated from original text
    article_ids = state["current_article_ids"]
    cluster_id = state["current_cluster_id"]
    
    logger.info(f"💾 뉴스레터 저장 중 (Cluster {cluster_id})...")
    
    try:
        conn = get_connection()
        
        # Get run_id and generation_history from state
        run_id = state.get("run_id")
        generation_history = state.get("generation_history")
        
        saved_id = save_news_letter(
            conn, 
            article_ids, 
            newsletter_to_save,
            run_id=run_id,
            generation_history=generation_history
        )
        
        logger.info(f"✅ 뉴스레터 저장 완료 (ID: {saved_id})")
        
        # Save pre-generated embedding if available
        if embedding:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE news_letter SET news_letter_embedding = %s WHERE news_letter_id = %s",
                    (embedding, saved_id)
                )
                conn.commit()
                cursor.close()
                logger.info(f"📐 임베딩 저장 완료 (원본 텍스트 기준)")
            except Exception as e:
                logger.warning(f"⚠️ 임베딩 저장 실패: {e}")
        
        conn.close()
        
        completed = list(state.get("completed_newsletters", []))
        completed.append(saved_id)
        
        return {
            "completed_newsletters": completed
        }
    
    except Exception as e:
        logger.error(f"❌ 뉴스레터 저장 실패: {e}")
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
