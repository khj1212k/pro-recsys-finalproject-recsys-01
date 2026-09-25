# 워크플로우 노드 함수 모음
# - 각 노드는 State를 받아 처리 후 업데이트된 State 반환
from typing import Dict, Any
import logging
import threading
from datetime import datetime
import json

from workflow.state import AgentState
from core.reconstructor import NewsReconstructor
from core.tone_converter import ToneConverter
from db.connection import get_connection, release_connection
from db.batch_manager import save_news_letter
from workflow.helpers import initialize_generation_history, log_generation_attempt
from workflow.evaluators import ClusterEvaluator, NewsletterEvaluator
from workflow.gates import check_newsletter_faithfulness, check_tone_drift
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
    # NewsletterEvaluator 프롬프트가 언론사(press_name)를 참조하는데
    # (workflow/evaluators.py의 source_summary) 이 필드가 없어서 항상 빈 문자열로
    # 채워지고 있었다 (data에는 press_names가 있음 - workflow/state.py 참고).
    # press_names가 없는(예: 구버전 테스트 픽스처) data도 있을 수 있어 방어적으로 처리한다.
    press_names = data.get('press_names') or []

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
                "content": data['contents'][position],
                "press_name": press_names[position] if position < len(press_names) else "",
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
        "faithfulness_report": None,
        "tone_drift_report": None,
        "tone_retry_count": 0,
        "tone_feedback": None,
        "tone_fallback": None,
        "failure_reason": None,
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
    
    # 평가 수행 (LLM 사용) - role="judge"로 레지스트리에서 클라이언트를 가져온다 (docs/adr/0005)
    from workflow.evaluators import ClusterEvaluator

    evaluator = ClusterEvaluator()
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

    # 1. Constraint: limit retries first — 서브그룹 분할 경로도 이 가드를 반드시
    #    거치도록 분기 순서를 조정함(기존에는 이 체크가 서브그룹 분기 아래에 있어
    #    서브그룹 경로에서는 도달 불가능했고, MAX_RETRIES 가드가 무력화되어 있었음).
    if retry_count >= Settings.MAX_RETRY_CLUSTER_EVAL:
        skipped = list(state.get("skipped_clusters", []))
        skipped.append(cluster_id)
        return {"skipped_clusters": skipped}

    # 2. LLM이 서브 그룹(쪼개기)을 제안한 경우
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
                "cluster_retry_count": retry_count + 1,
            }

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
        "current_articles": new_articles,
        "cluster_retry_count": retry_count + 1,
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


def check_faithfulness(state: AgentState) -> Dict[str, Any]:
    """생성 초안의 수치/인용/개체명을 클러스터 원문과 결정론적으로 대조한다 (ADR 0010).

    LLM judge보다 먼저 돈다: 원문에 없는 수치처럼 규칙으로 확실히 잡히는 오류는 judge
    호출 비용을 쓰기 전에 걸러 구체적인 피드백으로 재생성한다.
    """
    mode = Settings.FAITHFULNESS_GATE_MODE
    draft = state.get("newsletter_draft")
    if mode == "off" or not draft:
        return {"faithfulness_report": None}

    result = check_newsletter_faithfulness(
        draft, state.get("current_articles") or [], blocking_types=Settings.FAITHFULNESS_BLOCKING_TYPES
    )
    report = {**result.to_dict(), "mode": mode, "gate_passed": result.passed or mode == "shadow"}

    history = state.get("generation_history") or {"attempts": []}
    if history.get("attempts"):
        history["attempts"][-1]["faithfulness"] = {
            "passed": result.passed,
            "blocking": result.blocking,
            "advisory_entities": sorted({e["surface"] for e in result.advisory.get("entities", [])}),
            "entity_extractor": result.entity_extractor,
        }

    update: Dict[str, Any] = {"faithfulness_report": report, "generation_history": history}
    if not report["gate_passed"]:
        logger.info(
            f"🔎 [Cluster {state.get('current_cluster_id')}] 사실성 게이트 실패: "
            + ", ".join(f"{k}={len(v)}" for k, v in result.blocking.items() if v)
        )
        prev = state.get("newsletter_feedback") or ""
        update["newsletter_feedback"] = (
            f"{prev}\n\nAttempt {state.get('newsletter_retry_count', 0)} 사실성 검사:\n{result.feedback}"
        ).strip()
    return update


def route_after_faithfulness(state: AgentState) -> str:
    report = state.get("faithfulness_report")
    if not report or report.get("gate_passed"):
        return "pass"
    if state.get("newsletter_retry_count", 0) >= Settings.MAX_RETRY_NEWSLETTER_EVAL:
        return "max_retries"
    return "retry"


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
# Stage5가 클러스터를 스레드 풀로 병렬 처리한다(ADR 0010). BGE-M3(~2GB)를 스레드마다
# 따로 올리지 않도록 초기화를 잠그고, 한 모델 인스턴스에 추론이 겹치지 않게
# 호출도 직렬화한다 - 뉴스레터당 임베딩 1회라 LLM 대기에 비해 병목이 아니다.
_EMBEDDER_LOCK = threading.Lock()

def get_shared_embedder():
    global _CACHED_EMBEDDER
    with _EMBEDDER_LOCK:
        if _CACHED_EMBEDDER is None:
            logger.info("🔌 Loading shared NewsEmbedder for workflow...")
            from core.embedder import NewsEmbedder
            _CACHED_EMBEDDER = NewsEmbedder(l2_normalize=True)
        return _CACHED_EMBEDDER

def cleanup_workflow_embedder():
    global _CACHED_EMBEDDER
    with _EMBEDDER_LOCK:
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
        with _EMBEDDER_LOCK:
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
    
    # 이미 변환된 것이 있다면 스킵 (드리프트 재변환 시에는 check_tone_drift가 비워 둔다)
    if state.get("converted_newsletter"):
        return {}
        
    try:
        converter = ToneConverter()
        converted = converter.convert(draft, feedback=state.get("tone_feedback"))
        
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


def check_tone_drift_node(state: AgentState) -> Dict[str, Any]:
    """문체 변환본이 형식체 초안의 수치/날짜/고유명사를 바꿨는지 확인한다 (ADR 0010).

    드리프트가 있으면 피드백과 함께 최대 MAX_RETRY_TONE_DRIFT회 다시 변환하고, 그래도
    남으면 캐주얼본을 버리고 형식체 초안을 저장한다 - 사실이 바뀐 친근한 문장보다
    딱딱하지만 정확한 문장이 낫다.
    """
    mode = Settings.TONE_DRIFT_GATE_MODE
    draft = state.get("newsletter_draft")
    converted = state.get("converted_newsletter")
    if mode == "off" or not draft or not converted:
        return {"tone_drift_report": None}

    result = check_tone_drift(draft, converted, blocking_types=Settings.TONE_DRIFT_BLOCKING_TYPES)
    retries = state.get("tone_retry_count", 0)
    report = {**result.to_dict(), "mode": mode, "gate_passed": result.passed or mode == "shadow",
              "retries": retries, "fallback": None}
    update: Dict[str, Any] = {"tone_drift_report": report}

    if not report["gate_passed"]:
        if retries < Settings.MAX_RETRY_TONE_DRIFT:
            logger.info(f"🎨 [Cluster {state.get('current_cluster_id')}] 문체 드리프트 - 재변환 ({retries + 1})")
            update.update({"tone_retry_count": retries + 1, "tone_feedback": result.feedback,
                           "converted_newsletter": None})
        else:
            logger.info(f"🎨 [Cluster {state.get('current_cluster_id')}] 문체 드리프트 지속 - 형식체 초안 저장")
            report["fallback"] = "formal"
            update.update({"tone_fallback": "formal", "converted_newsletter": None,
                           "conversion_feedback": "tone drift persisted: saving formal draft"})

    history = state.get("generation_history") or {"attempts": []}
    history["tone_drift"] = {"passed": result.passed, "blocking": result.blocking,
                             "retries": update.get("tone_retry_count", retries), "fallback": report["fallback"]}
    update["generation_history"] = history
    return update


def route_after_tone_drift(state: AgentState) -> str:
    report = state.get("tone_drift_report")
    if report and not report.get("gate_passed") and state.get("tone_fallback") != "formal":
        return "retry"
    return "save"


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
    report = state.get("faithfulness_report")
    reason = "faithfulness" if report and not report.get("gate_passed") else "judge"
    return {
        "failed_clusters": failed,
        "failure_reason": reason,
        "error_message": f"Max retries reached ({reason}). Feedback: {last_feedback}"
    }


# ========== Routing Functions ==========

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

    # judge가 사람 라벨과 충분히 맞지 않으면(OOF kappa < 0.40, ADR 0009) 판정을 기록만
    # 하고 발행을 막지 않는다 - 사실성은 결정론적 게이트(check_faithfulness)가 맡는다.
    if Settings.JUDGE_GATE_MODE == "shadow":
        return "pass"

    if retry_count >= Settings.MAX_RETRY_NEWSLETTER_EVAL:
        return "max_retries"
    
    return "retry"