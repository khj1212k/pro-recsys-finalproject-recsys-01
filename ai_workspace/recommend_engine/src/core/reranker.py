# src/core/reranker.py
"""
MMR(Maximal Marginal Relevance) 재정렬 모듈
관련성과 다양성의 균형을 위한 재정렬
"""

import numpy as np
from typing import Dict, List, Tuple, Optional, Any


class MMRReranker:
    """관련성-다양성 균형 조절을 위한 MMR(Maximal Marginal Relevance) reranker
    MMR = λ * Relevance(item) - (1-λ) * max_sim(item, selected_items)
    λ가 높을수록 관련성을, 낮을수록 다양성을 우선시"""
    
    def __init__(
        self,
        lambda_param: float = 0.7,
        pool_multiplier: int = 4
    ):
        """reranking 가중치(lambda)와 후보군 배수(pool_multiplier) 값 설정"""
        self.lambda_param = lambda_param
        self.pool_multiplier = pool_multiplier
    
    def rerank(
        self,
        scores: np.ndarray,
        embeddings: np.ndarray,
        top_k: int
    ) -> List[Tuple[int, float]]:
        """임베딩 유사도 & mmr 점수를 기반으로 MMR 정렬을 수행하여 상위 Top-K 리스트 반환"""
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
        # 후보마다 "선택된 아이템들과의 최대 유사도"를 매번 다시 계산하는 대신, 새로 선택된
        # 아이템과의 유사도로 누적 최대값(max_sim)만 갱신한다. 선택 결과는 이전의 이중
        # 루프 구현과 같다(동점이면 풀 안에서 앞선 인덱스 우선 - np.argmax의 첫 최대값;
        # tests/recommend_engine/test_mmr_vectorized_equivalence.py). 요청마다 도는
        # 실시간 경로(ADR 0015)에서 top_k=20/풀 80/1024차원 기준 p50 약 14ms -> 0.35ms.
        selected = []
        available = np.ones(pool_size, dtype=bool)
        max_sim = None

        for _ in range(min(top_k, pool_size)):
            penalty = 0.0 if max_sim is None else max_sim
            mmr = self.lambda_param * norm_scores - (1 - self.lambda_param) * penalty
            mmr = np.where(available, mmr, -np.inf)
            best_idx = int(np.argmax(mmr))

            selected.append((top_indices[best_idx], pool_scores[best_idx]))
            available[best_idx] = False
            sims = norm_embeddings @ norm_embeddings[best_idx]
            max_sim = sims if max_sim is None else np.maximum(max_sim, sims)

        return selected


class CategoryBasedMMRReranker:
    """사용자의 선호 카테고리 수에 따라 다양성 가중치(lambda)를 동적으로 조절하는 reranker"""
    
    def __init__(
        self,
        lambda_few: float = 0.8,      # 1~2개 카테고리
        lambda_medium: float = 0.7,    # 3~4개 카테고리
        lambda_many: float = 0.6,      # 5개 이상
        lambda_default: float = 0.7,
        pool_multiplier: int = 4
    ):
        """카테고리 개수 구간별 lambda 값과 후보군 배수 설정"""
        self.lambda_few = lambda_few
        self.lambda_medium = lambda_medium
        self.lambda_many = lambda_many
        self.lambda_default = lambda_default
        self.pool_multiplier = pool_multiplier
    
    def get_lambda_for_category_count(self, num_categories: int) -> float:
        """사용자 선호 카테고리 개수에 따른 최적의 lambda 값 반환"""
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
        """사용자 선호 카테고리 개수를 고려해 동적 Lambda를 적용하여 MMR 재정렬 수행"""
        lambda_param = self.get_lambda_for_category_count(num_preferred_categories)
        
        reranker = MMRReranker(
            lambda_param=lambda_param,
            pool_multiplier=self.pool_multiplier
        )
        
        return reranker.rerank(scores, embeddings, top_k)


def create_reranker_from_config(config: Dict[str, Any]) -> CategoryBasedMMRReranker:
    """설정 객체(Config)로부터 파라미터를 불러와 CategoryBasedMMRReranker 인스턴스 생성"""
    rec_config = config.get('recommendation', {})
    mmr_config = rec_config.get('mmr_lambda', {})
    
    return CategoryBasedMMRReranker(
        lambda_few=mmr_config.get('few_categories', 0.8),
        lambda_medium=mmr_config.get('medium_categories', 0.7),
        lambda_many=mmr_config.get('many_categories', 0.6),
        lambda_default=mmr_config.get('default', 0.7),
        pool_multiplier=rec_config.get('mmr_pool_multiplier', 4)
    )
