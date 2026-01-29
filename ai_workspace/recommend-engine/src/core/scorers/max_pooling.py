import torch
from .base import BaseScorer


class MaxPoolingScorer(BaseScorer):
    """Max-Pooling 기반 점수 계산기."""

    def score(self, user_vectors, candidate_vectors, weights=None):
        # 1. 모든 조합의 유사도 계산 (N x M)
        # weights는 사용하지 않음 (가장 강한 신호 하나만 봄).
        scores_matrix = torch.matmul(user_vectors, candidate_vectors.T)

        # 2. Max-Pooling (각 후보 입장에서 가장 높은 유사도 선택)
        # values Shape: (M, )
        final_scores, _ = torch.max(scores_matrix, dim=0)

        return final_scores
