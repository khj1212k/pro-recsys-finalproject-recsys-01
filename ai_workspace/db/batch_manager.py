"""
뉴스레터 생성 파이프라인을 위한 배치(Batch) 및 run_id 관리
"""
import json
import logging
from typing import Dict, Optional
from db.connection import get_connection, release_connection
from core.reconstruction.validator import sanitize_text
from core.reconstruction.repository import CATEGORY_MAP

logger = logging.getLogger(__name__)



def convert_numpy(obj):
    """Numpy 타입을 Python 기본 타입으로 재귀적 변환"""
    import numpy as np
    if isinstance(obj, np.integer):
        return int(obj)
    elif isinstance(obj, np.floating):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    elif isinstance(obj, dict):
        return {key: convert_numpy(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [convert_numpy(item) for item in obj]
    return obj


def sanitize_obj(obj):
    """Recursively sanitize strings for safe UTF-8 storage"""
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, bytes):
        return sanitize_text(obj.decode('utf-8', errors='ignore'))
    if isinstance(obj, dict):
        return {k: sanitize_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_obj(v) for v in obj]
    return obj


def strip_surrogates(text: str) -> str:
    """Drop any surrogate code points that can break UTF-8 encoding"""
    return "".join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))


def create_new_batch(cluster_log: dict) -> int:
    """
    새로운 배치를 생성하고 run_id를 발급합니다.
    
    Args:
        cluster_log: 클러스터링 정보 로그
        
    Returns:
        run_id (배치 ID)
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get next run_id
        cursor.execute("""
            SELECT COALESCE(MAX(run_id), 0) + 1 FROM cluster_history
        """)
        run_id = cursor.fetchone()[0]
        
        cluster_log_converted = sanitize_obj(convert_numpy(cluster_log))
        
        # Insert cluster_history record
        cursor.execute("""
            INSERT INTO cluster_history (run_id, cluster_log, created_at)
            VALUES (%s, %s, NOW())
            RETURNING history_id
        """, (run_id, json.dumps(cluster_log_converted, ensure_ascii=False)))
        
        history_id = cursor.fetchone()[0]
        conn.commit()
        
        logger.info(f"Created batch run_id={run_id}, history_id={history_id}")
        return run_id
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to create batch: {e}")
        raise
    finally:
        cursor.close()
        release_connection(conn)


def get_current_run_id() -> Optional[int]:
    """
    가장 최근 실행된 run_id를 조회합니다.
    
    Returns:
        run_id 또는 배치가 없으면 None
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT run_id FROM cluster_history 
            ORDER BY created_at DESC 
            LIMIT 1
        """)
        result = cursor.fetchone()
        return result[0] if result else None
        
    finally:
        cursor.close()
        release_connection(conn)


def get_batch_info(run_id: int) -> Optional[Dict]:
    """
    run_id로 배치 정보를 조회합니다.
    
    Args:
        run_id: 배치 ID
        
    Returns:
        history_id, cluster_log, created_at을 포함한 Dict 또는 None
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT history_id, cluster_log, created_at
            FROM cluster_history
            WHERE run_id = %s
        """, (run_id,))
        
        result = cursor.fetchone()
        if result:
            return {
                "history_id": result[0],
                "cluster_log": result[1],
                "created_at": result[2]
            }
        return None
        
    finally:
        cursor.close()
        release_connection(conn)


def update_cluster_log(run_id: int, cluster_log: dict) -> bool:
    """
    특정 배치의 cluster_log를 업데이트합니다.
    
    Args:
        run_id: 배치 ID
        cluster_log: 업데이트할 클러스터 로그
        
    Returns:
        성공 여부
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cluster_log_converted = sanitize_obj(convert_numpy(cluster_log))
        cursor.execute("""
            UPDATE cluster_history
            SET cluster_log = %s
            WHERE run_id = %s
        """, (json.dumps(cluster_log_converted, ensure_ascii=False), run_id))
        
        conn.commit()
        logger.info(f"Updated cluster_log for run_id={run_id}")
        return True
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update cluster_log: {e}")
        return False
    finally:
        cursor.close()
        release_connection(conn)


def save_news_letter(
    conn, 
    article_ids: list, 
    newsletter_result: dict, 
    run_id: Optional[int] = None, 
    generation_history: Optional[list] = None
) -> int:
    """
    생성된 뉴스레터를 DB에 저장하고 관련 기사들을 업데이트합니다.
    
    Args:
        conn: DB Connection 객체
        article_ids: 뉴스레터에 포함된 news_raw_id 리스트
        newsletter_result: 생성된 뉴스레터 데이터 (title, sentence, content, keywords 등)
        run_id: 배치 실행 ID
        generation_history: 생성 과정 로그 리스트
        
    Returns:
        saved_news_letter_id (int)
    """
    cur = conn.cursor()
    try:
        # sanitize and normalize
        def coerce_text(value):
            if value is None:
                return ""
            if isinstance(value, bytes):
                value = value.decode('utf-8', errors='ignore')
            return sanitize_text(str(value))

        title = coerce_text(newsletter_result.get('title'))
        sentence = coerce_text(newsletter_result.get('sentence') or newsletter_result.get('summary'))
        content = coerce_text(newsletter_result.get('content'))
        # Final UTF-8 safety pass
        title = strip_surrogates(title.encode('utf-8', errors='ignore').decode('utf-8'))
        sentence = strip_surrogates(sentence.encode('utf-8', errors='ignore').decode('utf-8'))
        content = strip_surrogates(content.encode('utf-8', errors='ignore').decode('utf-8'))

        # keywords / generation history (nested sanitize + numpy conversion)
        keywords = sanitize_obj(convert_numpy(newsletter_result.get('keywords', [])))
        generation_history = sanitize_obj(convert_numpy(generation_history)) if generation_history else None
        article_ids = [int(x) for x in article_ids] if article_ids else []
        
        # 1. Insert Newsletter
        keywords_json = json.dumps(keywords, ensure_ascii=False)
        keywords_json = strip_surrogates(keywords_json.encode('utf-8', errors='ignore').decode('utf-8'))
        generation_history_json = json.dumps(generation_history, ensure_ascii=False) if generation_history else None
        if generation_history_json is not None:
            generation_history_json = strip_surrogates(generation_history_json.encode('utf-8', errors='ignore').decode('utf-8'))

        cur.execute("""
            INSERT INTO news_letter (
                news_letter_title, news_letter_sentence, news_letter_content,
                news_letter_keywords, raw_news_count, news_letter_created_at,
                run_id, generation_history
            ) VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s)
            RETURNING news_letter_id
        """, (
            title,
            sentence,
            content,
            keywords_json,
            len(article_ids),
            run_id,
            generation_history_json
        ))
        
        news_letter_id = cur.fetchone()[0]

        # 1.5 Save categories mapping
        categories = newsletter_result.get('categories') or []
        for category in categories:
            cat = sanitize_text(str(category))
            if not cat:
                continue
            mapped = CATEGORY_MAP.get(cat, cat)
            cur.execute("SELECT category_id FROM category WHERE category_name = %s", (mapped,))
            cat_row = cur.fetchone()
            if cat_row:
                cur.execute("""
                    INSERT INTO news_letter_categories (news_letter_id, category_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (news_letter_id, cat_row[0]))

        # 2. Update News Raw (Mapping)
        if article_ids:
            cur.execute("""
                UPDATE news_raw
                SET news_letter_id = %s
                WHERE raw_news_id = ANY(%s)
            """, (news_letter_id, article_ids))
        
        conn.commit()
        return news_letter_id
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save newsletter: {e}")
        raise
    finally:
        cur.close()