import torch
import torch.nn.functional as F
from .base import BaseScorer


class AttentionScorer(BaseScorer):
    """
    [Future Work] Query-Based Attention 점수 계산기.
    
    주의사항:
    - 현재 단계(Cold Start)보다는 데이터가 충분히 쌓인 후 고도화 시 사용 권장함.
    - Weighted Avg와 달리, 모든 후보 뉴스마다 가중치를 다시 계산하므로 연산 비용이 높음.
    - 작동 원리: "지금 추천하려는 후보 뉴스(Query)와 유사한 과거 이력(Key)에 더 높은 가중치를 부여함."
    """

    def score(self, user_vectors, candidate_vectors, weights=None):
        """
        Query-Based Attention 방식으로 추천 점수 계산.
        1. Attention Score 계산 (Query x Key^T)
        2. Softmax로 가중치 정규화
        3. User Representation 동적 생성 (Weighted Sum)
        4. 최종 유사도 계산
        
        반환: 각 후보 뉴스에 대한 점수 Tensor (shape: (M,))
        """
        # user_vectors: (N, Dim) -> Key & Value
        # candidate_vectors: (M, Dim) -> Query
        
        # 1. Attention Score 계산 (Query x Key^T)
        attn_scores = torch.matmul(candidate_vectors, user_vectors.T) # Attention Score 행렬 (shape: (M, N))
        
        # 2. Softmax를 통해 확률값(가중치)으로 변환
        attn_weights = F.softmax(attn_scores, dim=1) # Attention 가중치 (shape: (M, N))

        # 3. User Representation 동적 생성 (Weighted Sum of Values)
        dynamic_user_reps = torch.matmul(attn_weights, user_vectors) # 동적 유저 표현 벡터 (shape: (M, Dim))

        # 4. 최종 유사도 계산 (Element-wise multiplication 후 합산)
        final_scores = torch.sum(dynamic_user_reps * candidate_vectors, dim=1) # 최종 점수 (shape: (M,))

        return final_scores
