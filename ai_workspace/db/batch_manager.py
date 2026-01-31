"""
Batch and run_id management for newsletter generation pipeline
"""
import json
import logging
from typing import Dict, Optional
from db.connection import get_connection, release_connection

logger = logging.getLogger(__name__)


def create_new_batch(cluster_log: dict) -> int:
    """
    Create new batch and generate run_id
    
    Args:
        cluster_log: Clustering log with cluster information
        
    Returns:
        run_id (batch ID)
        
    Example cluster_log:
        {
            "total_articles": 100,
            "total_clusters": 8,
            "clusters": [
                {
                    "cluster_id": 0,
                    "article_count": 15,
                    "articles": [123, 456, 789]
                }
            ]
        }
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Get next run_id
        cursor.execute("""
            SELECT COALESCE(MAX(run_id), 0) + 1 FROM cluster_history
        """)
        run_id = cursor.fetchone()[0]
        
        # Convert numpy types to native Python types for JSON serialization
        def convert_numpy(obj):
            """Recursively convert numpy types to Python types"""
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
        
        cluster_log_converted = convert_numpy(cluster_log)
        
        # Insert cluster_history record
        cursor.execute("""
            INSERT INTO cluster_history (run_id, cluster_log, created_at)
            VALUES (%s, %s, NOW())
            RETURNING history_id
        """, (run_id, json.dumps(cluster_log_converted)))
        
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
    Get the most recent run_id
    
    Returns:
        run_id or None if no batches exist
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
    Get batch information by run_id
    
    Args:
        run_id: Batch ID
        
    Returns:
        Dict with history_id, cluster_log, created_at or None
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
    Update cluster_log for a batch
    
    Args:
        run_id: Batch ID
        cluster_log: Updated cluster log
        
    Returns:
        Success status
    """
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            UPDATE cluster_history
            SET cluster_log = %s
            WHERE run_id = %s
        """, (json.dumps(cluster_log), run_id))
        
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
