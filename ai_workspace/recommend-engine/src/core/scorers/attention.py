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
        # user_vectors: (N, Dim) -> Key & Value
        # candidate_vectors: (M, Dim) -> Query
        
        # 1. Attention Score 계산 (Query x Key)
        # Shape: (M, N) - M개 후보 각각에 대해 N개 이력과의 유사도 산출
        attn_scores = torch.matmul(candidate_vectors, user_vectors.T)
        
        # 2. Softmax를 통해 확률값(가중치)으로 변환
        # 각 후보(row)별로 이력들의 합이 1이 되도록 정규화
        attn_weights = F.softmax(attn_scores, dim=1)  # (M, N)

        # 3. User Representation 동적 생성 (Weighted Sum of Values)
        # 각 후보 뉴스에 맞춤형으로 생성된 유저 벡터
        # (M, N) x (N, Dim) -> (M, Dim)
        dynamic_user_reps = torch.matmul(attn_weights, user_vectors)

        # 4. 최종 유사도 계산
        # (M, Dim) * (M, Dim) -> element-wise multiplication & sum
        final_scores = torch.sum(dynamic_user_reps * candidate_vectors, dim=1) # (M, )

        return final_scores
