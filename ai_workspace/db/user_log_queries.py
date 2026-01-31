"""
User Newsletter CTR Log Helper Functions
사용자 뉴스레터 읽기 로그 관련 유틸리티 함수들
"""

from typing import List, Tuple, Optional
from datetime import datetime, timedelta
import psycopg2
import logging

logger = logging.getLogger(__name__)


def log_newsletter_read(
    conn,
    user_id: int,
    news_letter_id: int
) -> bool:
    """
    뉴스레터 읽기 이벤트 로그 기록
    
    Args:
        conn: DB connection
        user_id: 사용자 ID
        news_letter_id: 뉴스레터 ID
        
    Returns:
        성공 여부
    """
    cursor = conn.cursor()
    
    try:
        sql = """
        INSERT INTO user_newsletter_ctr_log 
            (user_id, news_letter_id)
        VALUES (%s, %s)
        RETURNING log_id
        """
        
        cursor.execute(sql, (user_id, news_letter_id))
        log_id = cursor.fetchone()[0]
        
        conn.commit()
        cursor.close()
        
        logger.debug(f"✓ 로그 기록: user={user_id}, newsletter={news_letter_id}, log_id={log_id}")
        return True
        
    except Exception as e:
        conn.rollback()
        cursor.close()
        logger.error(f"❌ 로그 기록 실패: {e}")
        return False


def get_user_read_history(
    conn,
    user_id: int,
    lookback_days: int = 90
) -> List[Tuple[int, datetime]]:
    """
    사용자의 뉴스레터 읽기 이력 조회
    
    Args:
        conn: PostgreSQL connection
        user_id: 사용자 ID
        lookback_days: 조회 기간 (일)
        
    Returns:
        [(news_letter_id, created_at), ...]
    """
    cursor = conn.cursor()
    
    cutoff_date = datetime.now() - timedelta(days=lookback_days)
    
    sql = """
    SELECT news_letter_id, created_at
    FROM user_newsletter_ctr_log
    WHERE user_id = %s 
      AND created_at >= %s
    ORDER BY created_at DESC
    """
    cursor.execute(sql, (user_id, cutoff_date))
    
    results = cursor.fetchall()
    cursor.close()
    
    return results


def get_newsletter_read_count(
    conn,
    news_letter_id: int,
    lookback_days: Optional[int] = None
) -> int:
    """
    특정 뉴스레터의 읽기 횟수 조회
    
    Args:
        conn: PostgreSQL connection
        news_letter_id: 뉴스레터 ID
        lookback_days: 조회 기간 (None이면 전체)
        
    Returns:
        읽기 횟수
    """
    cursor = conn.cursor()
    
    if lookback_days:
        cutoff_date = datetime.now() - timedelta(days=lookback_days)
        sql = """
        SELECT COUNT(DISTINCT user_id)
        FROM user_newsletter_ctr_log
        WHERE news_letter_id = %s
          AND created_at >= %s
        """
        cursor.execute(sql, (news_letter_id, cutoff_date))
    else:
        sql = """
        SELECT COUNT(DISTINCT user_id)
        FROM user_newsletter_ctr_log
        WHERE news_letter_id = %s
        """
        cursor.execute(sql, (news_letter_id,))
    
    count = cursor.fetchone()[0]
    cursor.close()
    
    return count


def get_user_interaction_stats(conn, user_id: int) -> dict:
    """
    사용자의 상호작용 통계 조회
    
    Args:
        conn: PostgreSQL connection
        user_id: 사용자 ID
        
    Returns:
        {
            'total_interactions': int,
            'avg_read_duration': float,
            'last_activity': datetime
        }
    """
    cursor = conn.cursor()
    
    sql = """
    SELECT 
        COUNT(*) as total_interactions,
        AVG(read_duration_seconds) as avg_read_duration,
        MAX(created_at) as last_activity
    FROM user_newsletter_ctr_log
    WHERE user_id = %s
    """
    
    cursor.execute(sql, (user_id,))
    result = cursor.fetchone()
    cursor.close()
    
    return {
        'total_interactions': result[0] or 0,
        'avg_read_duration': float(result[1]) if result[1] else 0.0,
        'last_activity': result[2]
    }


def get_popular_newsletters(
    conn,
    top_k: int = 10,
    lookback_days: int = 7
) -> List[Tuple[int, int]]:
    """
    인기 뉴스레터 순위 조회
    
    Args:
        conn: PostgreSQL connection
        top_k: 상위 K개
        lookback_days: 조회 기간 (일)
        
    Returns:
        [(news_letter_id, read_count), ...]
    """
    cursor = conn.cursor()
    
    cutoff_date = datetime.now() - timedelta(days=lookback_days)
    
    sql = """
    SELECT 
        news_letter_id,
        COUNT(DISTINCT user_id) as read_count
    FROM user_newsletter_ctr_log
    WHERE created_at >= %s
    GROUP BY news_letter_id
    ORDER BY read_count DESC
    LIMIT %s
    """
    
    cursor.execute(sql, (cutoff_date, top_k))
    results = cursor.fetchall()
    cursor.close()
    
    return results


def has_user_read_newsletter(
    conn,
    user_id: int,
    news_letter_id: int
) -> bool:
    """
    사용자가 특정 뉴스레터를 읽었는지 확인
    
    Args:
        conn: PostgreSQL connection
        user_id: 사용자 ID
        news_letter_id: 뉴스레터 ID
        
    Returns:
        읽었으면 True, 아니면 False
    """
    cursor = conn.cursor()
    
    sql = """
    SELECT EXISTS(
        SELECT 1
        FROM user_newsletter_ctr_log
        WHERE user_id = %s 
          AND news_letter_id = %s
    )
    """
    
    cursor.execute(sql, (user_id, news_letter_id))
    exists = cursor.fetchone()[0]
    cursor.close()
    
    return exists


# 테스트 함수
def test_log_functions():
    """로그 함수 테스트"""
    from db.connection import get_connection
    
    conn = get_connection()
    
    print("=" * 80)
    print("사용자 뉴스레터 CTR 로그 함수 테스트")
    print("=" * 80)
    
    # 1. 로그 기록 테스트
    print("\n[1] 로그 기록 테스트")
    log_newsletter_read(conn, user_id=1, news_letter_id=100, read_duration_seconds=120)
    
    # 2. 읽기 이력 조회
    print("\n[2] 사용자 읽기 이력 조회")
    history = get_user_read_history(conn, user_id=1, lookback_days=30)
    print(f"   총 {len(history)}개의 읽기 이력")
    
    # 3. 사용자 통계
    print("\n[3] 사용자 상호작용 통계")
    stats = get_user_interaction_stats(conn, user_id=1)
    print(f"   총 읽기: {stats['total_reads']}")
    print(f"   평균 읽기 시간: {stats['avg_read_duration']:.1f}초")
    
    # 4. 인기 뉴스레터
    print("\n[4] 인기 뉴스레터 Top 5")
    popular = get_popular_newsletters(conn, top_k=5, lookback_days=7)
    for nl_id, count in popular:
        print(f"   뉴스레터 {nl_id}: {count}명 읽음")
    
    conn.close()
    print("\n✅ 테스트 완료")


if __name__ == "__main__":
    test_log_functions()
