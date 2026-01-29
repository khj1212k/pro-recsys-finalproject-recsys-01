import torch
from .base import BaseScorer


class MaxPoolingScorer(BaseScorer):
    """Max-Pooling 기반 점수 계산"""

    def score(self, user_vectors, candidate_vectors, weights=None):
        """
        Max-Pooling 방식으로 추천 점수 계산.
        1. 모든 (이력, 후보) 조합의 유사도 계산
        2. 각 후보별로 가장 높은 유사도를 최종 점수로 선택
        
        반환: 각 후보 뉴스에 대한 점수 Tensor (shape: (M,))
        """
        # user_vectors: (N, Dim), candidate_vectors: (M, Dim)
        # weights는 사용하지 않음 (가장 강한 신호 하나만 사용)
        
        # 1. 모든 조합의 유사도 계산
        scores_matrix = torch.matmul(user_vectors, candidate_vectors.T) # 유사도 행렬 (shape: (N, M))

        # 2. Max-Pooling (각 후보별 최대값 추출)
        final_scores, _ = torch.max(scores_matrix, dim=0) # 최종 점수 (shape: (M,))

        return final_scores
