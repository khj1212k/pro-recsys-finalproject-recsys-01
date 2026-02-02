# src/core/reranker.py
"""
MMR(Maximal Marginal Relevance) 재정렬 모듈
관련성과 다양성의 균형을 위한 재정렬
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any


class MMRReranker:
    """
    Maximal Marginal Relevance 재정렬기
    
    MMR = λ * Relevance(item) - (1-λ) * max_sim(item, selected_items)
    
    - λ가 높을수록 관련성 우선
    - λ가 낮을수록 다양성 강화
    """
    
    def __init__(
        self,
        lambda_param: float = 0.7,
        pool_multiplier: int = 4
    ):
        """
        Args:
            lambda_param: λ 파라미터 (0~1)
            pool_multiplier: top_k * pool_multiplier 개를 MMR 후보로 사용
        """
        self.lambda_param = lambda_param
        self.pool_multiplier = pool_multiplier
    
    def rerank(
        self,
        scores: np.ndarray,
        embeddings: np.ndarray,
        top_k: int
    ) -> List[Tuple[int, float]]:
        """
        MMR 재정렬 수행
        
        Args:
            scores: [N] 각 아이템의 관련성 점수
            embeddings: [N, D] 각 아이템의 임베딩
            top_k: 최종 선택할 아이템 수
            
        Returns:
            [(item_idx, mmr_score), ...] top_k개
        """
        n_items = len(scores)
        
        if n_items <= top_k:
            # 아이템이 부족하면 점수순 반환
            sorted_indices = np.argsort(scores)[::-1]
            return [(idx, scores[idx]) for idx in sorted_indices]
        
        # Pool 크기 결정
        pool_size = min(top_k * self.pool_multiplier, n_items)
        
        # 상위 pool_size개만 후보로
        top_indices = np.argsort(scores)[::-1][:pool_size]
        pool_scores = scores[top_indices]
        pool_embeddings = embeddings[top_indices]
        
        # 점수 정규화 (0~1)
        min_score, max_score = pool_scores.min(), pool_scores.max()
        if max_score > min_score:
            norm_scores = (pool_scores - min_score) / (max_score - min_score)
        else:
            norm_scores = np.ones_like(pool_scores)
        
        # 임베딩 정규화 (코사인 유사도용)
        norms = np.linalg.norm(pool_embeddings, axis=1, keepdims=True)
        norms = np.where(norms > 0, norms, 1.0)
        norm_embeddings = pool_embeddings / norms
        
        # Greedy MMR 선택
        selected = []
        selected_embs = []
        remaining = list(range(pool_size))
        
        for _ in range(top_k):
            if not remaining:
                break
            
            best_idx = None
            best_mmr = float('-inf')
            
            for idx in remaining:
                # 관련성 점수
                relevance = norm_scores[idx]
                
                # 다양성 패널티 (선택된 아이템들과의 최대 유사도)
                if selected_embs:
                    selected_matrix = np.stack(selected_embs)
                    similarities = np.dot(selected_matrix, norm_embeddings[idx])
                    max_sim = similarities.max()
                else:
                    max_sim = 0.0
                
                # MMR 점수
                mmr = self.lambda_param * relevance - (1 - self.lambda_param) * max_sim
                
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = idx
            
            if best_idx is not None:
                selected.append((top_indices[best_idx], pool_scores[best_idx]))
                selected_embs.append(norm_embeddings[best_idx])
                remaining.remove(best_idx)
        
        return selected


class CategoryBasedMMRReranker:
    """
    카테고리 개수 기반 적응형 MMR 재정렬기
    
    선호 카테고리가 적을수록 관련성 우선 (높은 λ)
    선호 카테고리가 많을수록 다양성 강화 (낮은 λ)
    """
    
    def __init__(
        self,
        lambda_few: float = 0.8,      # 1~2개 카테고리
        lambda_medium: float = 0.7,    # 3~4개 카테고리
        lambda_many: float = 0.6,      # 5개 이상
        lambda_default: float = 0.7,
        pool_multiplier: int = 4
    ):
        """
        Args:
            lambda_few: 카테고리 1~2개일 때 λ
            lambda_medium: 카테고리 3~4개일 때 λ
            lambda_many: 카테고리 5개 이상일 때 λ
            lambda_default: 기본 λ
            pool_multiplier: top_k * pool_multiplier 개를 MMR 후보로 사용
        """
        self.lambda_few = lambda_few
        self.lambda_medium = lambda_medium
        self.lambda_many = lambda_many
        self.lambda_default = lambda_default
        self.pool_multiplier = pool_multiplier
    
    def get_lambda_for_category_count(self, num_categories: int) -> float:
        """카테고리 개수에 따른 λ 반환"""
        if num_categories <= 0:
            return self.lambda_default
        elif num_categories <= 2:
            return self.lambda_few
        elif num_categories <= 4:
            return self.lambda_medium
        else:
            return self.lambda_many
    
    def rerank_for_user(
        self,
        scores: np.ndarray,
        embeddings: np.ndarray,
        top_k: int,
        num_preferred_categories: int
    ) -> List[Tuple[int, float]]:
        """
        사용자의 선호 카테고리 수에 맞춰 MMR 재정렬
        
        Args:
            scores: [N] 각 아이템의 관련성 점수
            embeddings: [N, D] 각 아이템의 임베딩
            top_k: 최종 선택할 아이템 수
            num_preferred_categories: 사용자의 선호 카테고리 개수
            
        Returns:
            [(item_idx, mmr_score), ...] top_k개
        """
        lambda_param = self.get_lambda_for_category_count(num_preferred_categories)
        
        reranker = MMRReranker(
            lambda_param=lambda_param,
            pool_multiplier=self.pool_multiplier
        )
        
        return reranker.rerank(scores, embeddings, top_k)


def create_reranker_from_config(config: Dict[str, Any]) -> CategoryBasedMMRReranker:
    """
    설정에서 MMR Reranker 생성
    
    Args:
        config: 설정 딕셔너리
        
    Returns:
        CategoryBasedMMRReranker 인스턴스
    """
    rec_config = config.get('recommendation', {})
    mmr_config = rec_config.get('mmr_lambda', {})
    
    return CategoryBasedMMRReranker(
        lambda_few=mmr_config.get('few_categories', 0.8),
        lambda_medium=mmr_config.get('medium_categories', 0.7),
        lambda_many=mmr_config.get('many_categories', 0.6),
        lambda_default=mmr_config.get('default', 0.7),
        pool_multiplier=rec_config.get('mmr_pool_multiplier', 4)
    )
