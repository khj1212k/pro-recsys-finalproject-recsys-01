# 뉴스레터 생성 파이프라인을 위한 배치(Batch) 및 run_id 관리
import json
import logging
from typing import Dict, Optional
from db.connection import get_connection, release_connection
from core.reconstruction.validator import sanitize_text
from core.reconstruction.repository import CATEGORY_MAP

logger = logging.getLogger(__name__)



def convert_numpy(obj):
    # Numpy 타입을 Python 기본 타입으로 재귀적 변환
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
    # 안전한 UTF-8 저장을 위해 문자열 정제
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
    # UTF-8 인코딩을 방해할 수 있는 서러게이트 코드 포인트 제거
    return "".join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))


def create_new_batch(cluster_log: dict) -> int:

    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # 다음 run_id 가져오기
        cursor.execute("""
            SELECT COALESCE(MAX(run_id), 0) + 1 FROM cluster_history
        """)
        run_id = cursor.fetchone()[0]
        
        cluster_log_converted = sanitize_obj(convert_numpy(cluster_log))
        
        # cluster_history 레코드 삽입
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
    cur = conn.cursor()
    try:
        # 텍스트 정제 및 정규화
        def coerce_text(value):
            if value is None:
                return ""
            if isinstance(value, bytes):
                value = value.decode('utf-8', errors='ignore')
            return sanitize_text(str(value))

        title = coerce_text(newsletter_result.get('title'))
        sentence = coerce_text(newsletter_result.get('sentence') or newsletter_result.get('summary'))
        content = coerce_text(newsletter_result.get('content'))
        # UTF-8 안전 패스
        title = strip_surrogates(title.encode('utf-8', errors='ignore').decode('utf-8'))
        sentence = strip_surrogates(sentence.encode('utf-8', errors='ignore').decode('utf-8'))
        content = strip_surrogates(content.encode('utf-8', errors='ignore').decode('utf-8'))

        # 키워드 / 생성 이력 (중첩 정제 + numpy 변환)
        keywords = sanitize_obj(convert_numpy(newsletter_result.get('keywords', [])))
        generation_history = sanitize_obj(convert_numpy(generation_history)) if generation_history else None
        article_ids = [int(x) for x in article_ids] if article_ids else []
        
        # 1. 뉴스레터 삽입
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

        # 1.5 카테고리 매핑 저장
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

        # 2. 뉴스 원본 업데이트 (매핑)
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