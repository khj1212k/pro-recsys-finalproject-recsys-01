"""
User Embedder Module
사용자 임베딩 생성 및 업데이트

사용자의 선호 뉴스레터와 읽기 이력을 기반으로
시간 감쇠 가중 평균(Time-Decay Weighted Average)을 사용하여
사용자 임베딩을 생성합니다.
"""

import math
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import numpy as np

from core.embedder import NewsEmbedder
from db.connection import get_connection

logger = logging.getLogger(__name__)


class UserEmbedder:
    """
    사용자 임베딩 생성기
    
    전략: 시간 감쇠 가중 평균 (Time-Decay Weighted Average)
    - 선호 뉴스레터 (가입 시): 고정 가중치 1.0
    - 읽기 이력: exp(-λ * days_since_read)
    
    Formula:
        user_embedding = Σ(w_i * newsletter_emb_i) / Σ(w_i)
    """
    
    def __init__(
        self,
        preference_weight_ratio: float = 0.4,
        decay_rate: float = 0.05,
        lookback_days: int = 90,
        min_interactions: int = 1,
        l2_normalize: bool = True
    ):
        """
        Args:
            preference_weight_ratio: 선호도 vs 읽기이력 비율 (0-1)
            decay_rate: 시간 감쇠율 λ (0.01-0.1)
            lookback_days: 읽기 이력 조회 기간 (일)
            min_interactions: 최소 상호작용 수
            l2_normalize: L2 정규화 여부
        """
        self.preference_weight_ratio = preference_weight_ratio
        self.decay_rate = decay_rate
        self.lookback_days = lookback_days
        self.min_interactions = min_interactions
        self.l2_normalize = l2_normalize
        
        logger.info(f"UserEmbedder 초기화: decay_rate={decay_rate}, lookback={lookback_days}일")
    
    def calculate_time_decay_weight(self, days_ago: float) -> float:
        """
        시간 감쇠 가중치 계산
        
        Formula: w = exp(-λ * days)
        
        Args:
            days_ago: 경과 일수
            
        Returns:
            가중치 (0-1)
        """
        return math.exp(-self.decay_rate * days_ago)
    
    def get_user_preferred_newsletters(self, conn, user_id: int) -> List[int]:
        """
        사용자 선호 뉴스레터 조회 (가입 시 선택)
        
        Args:
            conn: DB connection
            user_id: 사용자 ID
            
        Returns:
            뉴스레터 ID 리스트
        """
        cursor = conn.cursor()
        
        sql = """
        SELECT news_letter_id
        FROM user_preferred_newsletter
        WHERE user_id = %s
        """
        
        cursor.execute(sql, (user_id,))
        results = cursor.fetchall()
        cursor.close()
        
        newsletter_ids = [row[0] for row in results]
        logger.debug(f"User {user_id}: {len(newsletter_ids)}개 선호 뉴스레터")
        
        return newsletter_ids
    
    def get_user_read_history(
        self,
        conn,
        user_id: int
    ) -> List[Tuple[int, datetime]]:
        """
        사용자 읽기 이력 조회
        
        ⚠️ 주의: 이 함수는 user_newsletter_ctr_log 테이블이 존재한다고 가정합니다.
        백엔드 팀에서 테이블을 먼저 생성해야 합니다.
        
        Args:
            conn: DB connection
            user_id: 사용자 ID
            
        Returns:
            [(news_letter_id, read_at), ...]
        """
        cursor = conn.cursor()
        
        cutoff_date = datetime.now() - timedelta(days=self.lookback_days)
        
        # 테이블이 존재하는지 확인
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_name = 'user_newsletter_ctr_log'
            )
        """)
        
        table_exists = cursor.fetchone()[0]
        
        if not table_exists:
            logger.warning("⚠️ user_newsletter_ctr_log 테이블이 존재하지 않습니다.")
            cursor.close()
            return []
        
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
        
        logger.debug(f"User {user_id}: {len(results)}개 읽기 이력 ({self.lookback_days}일)")
        
        return results
    
    def fetch_newsletter_embeddings(
        self,
        conn,
        newsletter_ids: List[int]
    ) -> Dict[int, np.ndarray]:
        """
        뉴스레터 임베딩 배치 조회
        
        Args:
            conn: DB connection
            newsletter_ids: 뉴스레터 ID 리스트
            
        Returns:
            {news_letter_id: embedding_array, ...}
        """
        if not newsletter_ids:
            return {}
        
        cursor = conn.cursor()
        
        # PostgreSQL에서 배열을 사용한 IN 쿼리
        sql = """
        SELECT news_letter_id, news_letter_embedding
        FROM news_letter
        WHERE news_letter_id = ANY(%s)
          AND news_letter_embedding IS NOT NULL
        """
        
        cursor.execute(sql, (newsletter_ids,))
        results = cursor.fetchall()
        cursor.close()
        
        embeddings = {}
        for nl_id, embedding in results:
            if embedding:
                embeddings[nl_id] = np.array(embedding, dtype=np.float32)
        
        logger.debug(f"{len(embeddings)}/{len(newsletter_ids)}개 뉴스레터 임베딩 조회")
        
        return embeddings
    
    def compute_weighted_embedding(
        self,
        embeddings: Dict[int, np.ndarray],
        weights: Dict[int, float]
    ) -> Optional[np.ndarray]:
        """
        가중 평균 임베딩 계산
        
        Formula: Σ(w_i * emb_i) / Σ(w_i)
        
        Args:
            embeddings: {newsletter_id: embedding}
            weights: {newsletter_id: weight}
            
        Returns:
            가중 평균 임베딩 또는 None
        """
        if not embeddings:
            logger.warning("임베딩 데이터가 없습니다")
            return None
        
        # 가중합 계산
        weighted_sum = None
        total_weight = 0.0
        
        for nl_id, embedding in embeddings.items():
            weight = weights.get(nl_id, 0.0)
            if weight <= 0:
                continue
            
            if weighted_sum is None:
                weighted_sum = weight * embedding
            else:
                weighted_sum += weight * embedding
            
            total_weight += weight
        
        if weighted_sum is None or total_weight == 0:
            logger.warning("유효한 가중치가 없습니다")
            return None
        
        # 가중 평균
        user_embedding = weighted_sum / total_weight
        
        # L2 정규화
        if self.l2_normalize:
            norm = np.linalg.norm(user_embedding)
            if norm > 0:
                user_embedding = user_embedding / norm
        
        return user_embedding
    
    def generate_user_embedding(
        self,
        user_id: int,
        conn = None
    ) -> Optional[np.ndarray]:
        """
        사용자 임베딩 생성 (메인 함수)
        
        Steps:
        1. 선호 뉴스레터 조회
        2. 읽기 이력 조회
        3. 가중치 계산
        4. 뉴스레터 임베딩 조회
        5. 가중 평균 계산
        
        Args:
            user_id: 사용자 ID
            conn: DB connection (None이면 자동 생성)
            
        Returns:
            사용자 임베딩 (1024-dim) 또는 None
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        
        try:
            logger.info(f"👤 User {user_id} 임베딩 생성 시작...")
            
            # 1. 선호 뉴스레터
            preferred_ids = self.get_user_preferred_newsletters(conn, user_id)
            
            # 2. 읽기 이력
            read_history = self.get_user_read_history(conn, user_id)
            
            # 3. 최소 상호작용 체크
            total_interactions = len(preferred_ids) + len(read_history)
            if total_interactions < self.min_interactions:
                logger.warning(
                    f"User {user_id}: 상호작용 부족 "
                    f"({total_interactions} < {self.min_interactions})"
                )
                return None
            
            # 4. 가중치 계산
            weights = {}
            all_newsletter_ids = set()
            
            # 4-1. 선호 뉴스레터 (고정 가중치)
            for nl_id in preferred_ids:
                weights[nl_id] = weights.get(nl_id, 0.0) + self.preference_weight_ratio
                all_newsletter_ids.add(nl_id)
            
            # 4-2. 읽기 이력 (시간 감쇠)
            now = datetime.now()
            for nl_id, read_at in read_history:
                days_ago = (now - read_at).total_seconds() / 86400  # 초 -> 일
                decay_weight = self.calculate_time_decay_weight(days_ago)
                
                # 읽기 이력 가중치 = (1 - preference_ratio) * decay
                read_weight = (1 - self.preference_weight_ratio) * decay_weight
                
                weights[nl_id] = weights.get(nl_id, 0.0) + read_weight
                all_newsletter_ids.add(nl_id)
            
            # 5. 뉴스레터 임베딩 조회
            embeddings = self.fetch_newsletter_embeddings(conn, list(all_newsletter_ids))
            
            if not embeddings:
                logger.warning(f"User {user_id}: 뉴스레터 임베딩 없음")
                return None
            
            # 6. 가중 평균 계산
            user_embedding = self.compute_weighted_embedding(embeddings, weights)
            
            if user_embedding is not None:
                logger.info(
                    f"✅ User {user_id} 임베딩 생성 완료 "
                    f"(dim: {len(user_embedding)}, "
                    f"sources: {len(embeddings)}개 뉴스레터)"
                )
            else:
                logger.warning(f"❌ User {user_id} 임베딩 생성 실패")
            
            return user_embedding
            
        except Exception as e:
            logger.error(f"❌ User {user_id} 임베딩 생성 오류: {e}")
            return None
        
        finally:
            if should_close:
                conn.close()
    
    def update_user_embedding_db(
        self,
        user_id: int,
        embedding: np.ndarray,
        conn = None
    ) -> bool:
        """
        사용자 임베딩을 DB에 저장
        
        Args:
            user_id: 사용자 ID
            embedding: 임베딩 벡터
            conn: DB connection
            
        Returns:
            성공 여부
        """
        should_close = False
        if conn is None:
            conn = get_connection()
            should_close = True
        
        try:
            cursor = conn.cursor()
            
            # 임베딩을 리스트로 변환
            embedding_list = embedding.tolist()
            
            sql = """
            UPDATE "user"
            SET user_embedding = %s
            WHERE user_id = %s
            """
            
            cursor.execute(sql, (embedding_list, user_id))
            conn.commit()
            cursor.close()
            
            logger.info(f"💾 User {user_id} 임베딩 DB 저장 완료")
            return True
            
        except Exception as e:
            logger.error(f"❌ User {user_id} 임베딩 저장 실패: {e}")
            conn.rollback()
            return False
        
        finally:
            if should_close:
                conn.close()
    
    def batch_update_all_users(
        self,
        user_ids: Optional[List[int]] = None,
        only_null: bool = True
    ) -> Dict[str, int]:
        """
        여러 사용자 임베딩 배치 업데이트
        
        Args:
            user_ids: 업데이트할 사용자 ID 리스트 (None이면 전체)
            only_null: True면 임베딩이 NULL인 사용자만 처리 (기본값: True)
            
        Returns:
            {'success': count, 'failed': count, 'skipped': count}
        """
        conn = get_connection()
        
        try:
            # 사용자 목록 가져오기
            if user_ids is None:
                cursor = conn.cursor()
                
                if only_null:
                    # 임베딩이 NULL인 사용자만 조회
                    cursor.execute(
                        'SELECT user_id FROM "user" WHERE user_embedding IS NULL'
                    )
                    user_ids = [row[0] for row in cursor.fetchall()]
                    logger.info(f"📊 임베딩이 없는 사용자: {len(user_ids)}명")
                else:
                    # 전체 사용자 조회
                    cursor.execute('SELECT user_id FROM "user"')
                    user_ids = [row[0] for row in cursor.fetchall()]
                    logger.info(f"📊 전체 사용자: {len(user_ids)}명")
                
                cursor.close()
            
            if not user_ids:
                logger.info("처리할 사용자가 없습니다")
                return {'success': 0, 'failed': 0, 'skipped': 0}
            
            logger.info(f"📊 총 {len(user_ids)}명의 사용자 임베딩 업데이트 시작...")
            
            results = {'success': 0, 'failed': 0, 'skipped': 0}
            
            for i, user_id in enumerate(user_ids, 1):
                logger.info(f"\n[{i}/{len(user_ids)}] User {user_id} 처리 중...")
                
                # 임베딩 생성
                embedding = self.generate_user_embedding(user_id, conn)
                
                if embedding is None:
                    results['skipped'] += 1
                    continue
                
                # DB 저장
                success = self.update_user_embedding_db(user_id, embedding, conn)
                
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
            
            logger.info(f"\n{'='*80}")
            logger.info(f"✅ 배치 업데이트 완료!")
            logger.info(f"  성공: {results['success']}명")
            logger.info(f"  실패: {results['failed']}명")
            logger.info(f"  건너뜀: {results['skipped']}명")
            logger.info(f"{'='*80}")
            
            return results
            
        finally:
            conn.close()


# 테스트 함수
def test_user_embedder():
    """UserEmbedder 테스트"""
    logging.basicConfig(level=logging.INFO)
    
    print("="*80)
    print("UserEmbedder 테스트")
    print("="*80)
    
    embedder = UserEmbedder(
        preference_weight_ratio=0.4,
        decay_rate=0.05,
        lookback_days=90
    )
    
    # 테스트: 시간 감쇠 가중치
    print("\n[1] 시간 감쇠 가중치 테스트")
    for days in [1, 7, 30, 90]:
        weight = embedder.calculate_time_decay_weight(days)
        print(f"  {days}일 전: {weight:.4f}")
    
    # 테스트: 사용자 임베딩 생성 (user_id=1)
    print("\n[2] 사용자 임베딩 생성 테스트 (user_id=1)")
    embedding = embedder.generate_user_embedding(user_id=1)
    
    if embedding is not None:
        print(f"  ✅ 임베딩 생성 성공: {len(embedding)}차원")
        print(f"  샘플 값: {embedding[:5]}")
    else:
        print(f"  ❌ 임베딩 생성 실패")
    
    print("\n✅ 테스트 완료")


if __name__ == "__main__":
    test_user_embedder()
