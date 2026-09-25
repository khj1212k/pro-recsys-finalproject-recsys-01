# 워크플로우 노드 함수 모음
# - 각 노드는 State를 받아 처리 후 업데이트된 State 반환
from typing import Dict, Any
import logging
from datetime import datetime
import json

from workflow.state import AgentState
from core.reconstructor import NewsReconstructor
from core.tone_converter import ToneConverter
from db.connection import get_connection, release_connection
from db.batch_manager import save_news_letter
from workflow.helpers import initialize_generation_history, log_generation_attempt
from workflow.evaluators import ClusterEvaluator, NewsletterEvaluator
from config.settings import Settings

logger = logging.getLogger(__name__)


def initialize_cluster_processing(state: AgentState) -> Dict[str, Any]:
    """
    클러스터 처리 초기화 노드
    """
    idx = state.get("current_cluster_index", 0)
    all_ids = state.get("all_cluster_ids", [])
    
    if idx >= len(all_ids):
        return {"should_continue": False}
        
    cluster_id = all_ids[idx]
    
    # 클러스터 데이터 로드
    cluster_groups = state["all_cluster_groups"]
    article_ids = cluster_groups.get(cluster_id, [])
    
    # 기사 내용 로드 (제목, 본문 등)
    data = state.get("data", {})
    articles = []
    
    for aid in article_ids:
        try:
            import numpy as np
            if isinstance(data['ids'], np.ndarray):
                matches = np.where(data['ids'] == aid)[0]
                if len(matches) == 0: raise ValueError
                position = matches[0]
            else:
                position = data['ids'].index(aid)
            articles.append({
                "id": aid,
                "title": data['titles'][position],
                "content": data['contents'][position]
            })
        except ValueError:
            logger.info(f"ℹ️ 기사 ID {aid}를 데이터에서 찾을 수 없습니다.")
            continue
            
    logger.info(f"🚀 [Cluster {cluster_id}] 처리 시작 (기사 {len(articles)}개)")
    
    return {
        "current_cluster_id": cluster_id,
        "current_article_ids": article_ids,
        "current_articles": articles,
        "newsletter_draft": None,
        "cluster_eval": None,
        "newsletter_eval": None,
        "cluster_retry_count": 0,
        "newsletter_retry_count": 0,
        "error_message": None
    }


def evaluate_cluster(state: AgentState) -> Dict[str, Any]:
    """
    클러스터 평가 노드
    군집화된 기사들이 하나의 뉴스레터로 묶이기에 적절한지 평가 (주제 일관성, 기사 개수 등)
    """
    articles = state["current_articles"]
    cluster_id = state["current_cluster_id"]
    
    # 기본 검사: 기사 개수 부족
    if len(articles) < 2:
        logger.info(f"⏭️ [Cluster {cluster_id}] 기사 수 부족으로 패스 (기사 {len(articles)}개)")
        return {
            "cluster_eval": {
                "decision": "FAIL",
                "reason": "Not enough articles",
                "feedback": "Need at least 2 articles"
            }
        }
    
    # 평가 수행 (LLM 사용)
    from workflow.evaluators import ClusterEvaluator
    
    # Provider 설정 (None이면 환경변수나 기본값 사용)
    evaluator = ClusterEvaluator(provider=None)
    eval_result = evaluator.evaluate(articles)
    
    if eval_result["decision"] == "FAIL":
        # 평가 실패는 정상적인 흐름(필터링)으로 처리
        logger.info(f"⛔ [Cluster {cluster_id}] 평가 실패 (Conf: {eval_result.get('confidence')}): {eval_result.get('feedback')}")
    else:
        logger.info(f"✅ [Cluster {cluster_id}] 평가 통과 (Conf: {eval_result.get('confidence')})")
        
    return {"cluster_eval": eval_result}


def handle_cluster_eval_failure(state: AgentState) -> Dict[str, Any]:
    """
    If no outliers or max retries reached, skip the cluster.
    """
    cluster_id = state["current_cluster_id"]
    eval_result = state.get("cluster_eval", {})
    outlier_indices = eval_result.get("outlier_indices", [])
    sub_groups = eval_result.get("sub_groups", [])
    
    retry_count = state.get("cluster_retry_count", 0)
    
    # 1. LLM이 서브 그룹(쪼개기)을 제안한 경우
    if sub_groups and len(sub_groups) >= 2:
        # 가장 큰 서브 그룹을 선택하여 진행 (현재 1:1 구조상 대표 그룹 1개만 살림)
        sub_groups.sort(key=len, reverse=True)
        best_indices = sub_groups[0]
        
        current_articles = state["current_articles"]
        valid_indices = [i for i in best_indices if 0 <= i < len(current_articles)]
        
        if len(valid_indices) >= 3:
            new_articles = [current_articles[i] for i in valid_indices]
            logger.info(f"♻️  [Cluster {cluster_id}] LLM 제안으로 그룹 쪼개기: Top 그룹({len(new_articles)}개)으로 재시도")
            
            return {
                "current_articles": new_articles,
                # 재시도 횟수 증가 (무한 루프 방지)
                # "cluster_retry_count": retry_count + 1 
            }

    # 2. Constraint: limit retries (e.g. 2 times)
    MAX_RETRIES = 2
    if retry_count >= MAX_RETRIES:
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        return {"skipped_clusters": skipped}
        
    # 3. 아웃라이어 제거 로직 (기존)
    if not outlier_indices:
        # 아웃라이어도 없고 서브그룹도 없는데 FAIL이면 스킵
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        return {"skipped_clusters": skipped}
    
    # 아웃라이어 제거
    current_articles = state["current_articles"]
    valid_indices = [i for i in outlier_indices if 0 <= i < len(current_articles)]
    
    if not valid_indices:
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        return {"skipped_clusters": skipped}

    outlier_set = set(valid_indices)
    new_articles = [art for i, art in enumerate(current_articles) if i not in outlier_set]
    
    # 최소 기사 수 확인
    if len(new_articles) < 3:
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        return {"skipped_clusters": skipped}
    
    logger.info(f"♻️  [Cluster {cluster_id}] Refining: Removed {len(valid_indices)} outliers, retrying with {len(new_articles)} articles")
    
    return {
        "current_articles": new_articles
    }


def route_after_cluster_fail(state: AgentState) -> str:
    skipped = state.get("skipped_clusters", [])
    if state["current_cluster_id"] in skipped:
        return "end"
    
    return "retry"


def generate_newsletter(state: AgentState) -> Dict[str, Any]:
    from datetime import datetime
    
    articles = state["current_articles"]
    feedback = state.get("newsletter_feedback")
    retry_count = state.get("newsletter_retry_count", 0)
    
    generation_history = initialize_generation_history(state)
    
    reconstructor = NewsReconstructor()
    draft = reconstructor.reconstruct(articles, feedback=feedback)
    
    log_generation_attempt(generation_history, draft, state)
    
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
    
    new_feedback = state.get("newsletter_feedback", "") or ""
    if result["decision"] == "FAIL" and result["feedback"]:
        new_feedback = f"{new_feedback}\n\nAttempt {state['newsletter_retry_count']} feedback:\n{result['feedback']}"
    
    generation_history = state.get("generation_history", {"attempts": []})
    if generation_history.get("attempts"):
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



_CACHED_EMBEDDER = None

def get_shared_embedder():
    global _CACHED_EMBEDDER
    if _CACHED_EMBEDDER is None:
        logger.info("🔌 Loading shared NewsEmbedder for workflow...")
        from core.embedder import NewsEmbedder
        _CACHED_EMBEDDER = NewsEmbedder(l2_normalize=True)
    return _CACHED_EMBEDDER

def cleanup_workflow_embedder():
    global _CACHED_EMBEDDER
    if _CACHED_EMBEDDER:
        logger.info("🧹 Cleaning up shared NewsEmbedder...")
        _CACHED_EMBEDDER.cleanup()
        _CACHED_EMBEDDER = None


def embed_newsletter_node(state: AgentState) -> Dict[str, Any]:
    """
    뉴스레터 임베딩 생성 노드
    """
    draft = state["newsletter_draft"]

    if not draft or not draft.get("content"):
        logger.info("ℹ️ 임베딩을 위한 초안 내용이 없습니다.")
        return {
            "original_newsletter": draft,
            "newsletter_embedding": None
        }
    
    try:
        # 공유된 임베더 인스턴스 가져오기
        embedder = get_shared_embedder()
        
        # 제목과 본문을 결합하여 임베딩 생성
        text_to_embed = f"{draft.get('title', '')} {draft.get('content', '')}"
        
        # 단일 문자열 임베딩 (배치 함수 재사용)
        embeddings, _ = embedder.generate_embeddings_batch([text_to_embed])
        
        if not embeddings:
            raise ValueError("임베딩 반환값 없음")
            
        embedding = embeddings[0]
        
        logger.info("✅ 원본 초안(Formal) 기반 임베딩 생성 완료")
        
        return {
            "original_newsletter": draft,
            "newsletter_embedding": embedding # 리스트 형태
        }
        
    except Exception as e:
        logger.info(f"ℹ️ 임베딩 생성 실패: {e}")
        return {
            "original_newsletter": draft,
            "newsletter_embedding": None
        }


def convert_tone_node(state: AgentState) -> Dict[str, Any]:
    """
    문체 변환 노드
    """
    draft = state["newsletter_draft"]
    
    # 이미 변환된 것이 있다면 스킵 (재시도 로직 등)
    if state.get("converted_newsletter"):
        return {}
        
    try:
        converter = ToneConverter()
        converted = converter.convert(draft)
        
        if not converted:
            logger.info("ℹ️ 문체 변환 실패, 원본 사용")
            return {
                "converted_newsletter": draft.copy(),  # Fallback to original
                "conversion_feedback": "Conversion failed, using original"
            }
        
        logger.info("✅ 문체 변환 완료")
        
        return {
            "converted_newsletter": converted,
            "conversion_feedback": "Conversion successful"
        }
        
    except Exception as e:
        logger.info(f"ℹ️ 문체 변환 오류: {e}")
        # Fallback to original on error
        return {
            "converted_newsletter": draft.copy(),
            "conversion_feedback": f"Conversion error: {e}, using original",
            "error_message": f"Tone conversion failed: {e}"
        }


def save_newsletter_to_db(state: AgentState) -> Dict[str, Any]:
    """
    뉴스레터 DB 저장 노드
    
    - 내용(Content): 변환된 부드러운 문체(Casual)
    - 임베딩(Embedding): 원본 딱딱한 문체(Formal) 기반
    """
    # 변환된 뉴스레터 우선 사용, 없으면 원본 사용
    draft = state.get("newsletter_draft") or {}
    converted = state.get("converted_newsletter") or {}
    newsletter_to_save = {**draft, **converted} if converted else draft.copy()
    if not newsletter_to_save.get("sentence"):
        newsletter_to_save["sentence"] = (
            newsletter_to_save.get("summary")
            or draft.get("sentence")
            or (newsletter_to_save.get("content", "").split("\n")[0].strip() if newsletter_to_save.get("content") else "")
        )
    if not newsletter_to_save.get("title"):
        newsletter_to_save["title"] = draft.get("title") or "뉴스 요약"
    if not newsletter_to_save.get("content"):
        newsletter_to_save["content"] = draft.get("content") or ""
    embedding = state.get("newsletter_embedding")  # 원본 기반 임베딩
    article_ids = state["current_article_ids"]
    cluster_id = state["current_cluster_id"]
    
    logger.info(f"💾 뉴스레터 저장 중 (Cluster {cluster_id})...")
    
    try:
        conn = get_connection()
        
        # 실행 ID 및 생성 이력 가져오기
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
        
        # 미리 생성된 임베딩이 있으면 업데이트
        if embedding:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE news_letter \
                     SET news_letter_embedding = %s \
                     WHERE news_letter_id = %s",
                    (embedding, saved_id)
                )
                conn.commit()
                cursor.close()
                logger.info(f"📐 임베딩 저장 완료 (원본 텍스트 기준)")
            except Exception as e:
                logger.info(f"ℹ️ 임베딩 저장 실패: {e}")
        
        release_connection(conn)

        completed = list(state.get("completed_newsletters", []))
        completed.append(saved_id)
        
        return {
            "completed_newsletters": completed
        }
    
    except Exception as e:
        logger.info(f"ℹ️ 뉴스레터 저장 실패: {e}")
        failed = list(state.get("failed_clusters", []))
        failed.append(cluster_id)
        
        return {
            "failed_clusters": failed,
            "error_message": str(e)
        }


def handle_newsletter_max_retries(state: AgentState) -> Dict[str, Any]:
    cluster_id = state["current_cluster_id"]
    failed = list(state.get("failed_clusters", []))
    failed.append(cluster_id)
    
    
    last_feedback = state.get("newsletter_feedback", "No feedback")
    return {
        "failed_clusters": failed,
        "error_message": f"Max retries reached. Feedback: {last_feedback}"
    }


def finalize_workflow(state: AgentState) -> Dict[str, Any]:
    completed = state.get("completed_newsletters", [])
    failed = state.get("failed_clusters", [])
    skipped = state.get("skipped_clusters", [])
    
    
    return {"should_continue": False}


# ========== Routing Functions ==========

def should_continue_processing(state: AgentState) -> str:
    if state["current_cluster_index"] >= len(state["all_cluster_ids"]):
        return "end"
    return "continue"


def route_after_cluster_eval(state: AgentState) -> str:
    eval_result = state.get("cluster_eval", {})
    
    if eval_result.get("decision") == "PASS":
        return "pass"
    
    return "fail"


def route_after_newsletter_eval(state: AgentState) -> str:
    eval_result = state.get("newsletter_eval", {})
    retry_count = state.get("newsletter_retry_count", 0)
    
    if eval_result.get("decision") == "PASS":
        return "pass"
    
    if retry_count >= Settings.MAX_RETRY_NEWSLETTER_EVAL:
        return "max_retries"
    
    return "retry"