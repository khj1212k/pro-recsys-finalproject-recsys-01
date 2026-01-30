import torch


class MMRReranker:
    """MMR(Maximal Marginal Relevance) 기반 재정렬"""

    def __init__(self, lambda_param=0.7):
        self.lambda_param = lambda_param # Relevance와 Diversity 간의 균형 파라미터 (0~1)

    def rerank(self, scores, candidate_vectors, candidate_ids, top_k):
        """
        MMR 알고리즘을 적용하여 다양성을 고려한 추천 리스트 생성.
        1. Pre-Filtering: 상위 4배수만 MMR 대상으로 선정
        2. Greedy Selection: 관련성과 중복도를 고려해 순차 선택
        
        반환: 최종 추천 결과 리스트 (각 항목: {"news_id": ..., "score": ...})
        """
        # scores: (M,), candidate_vectors: (M, Dim), candidate_ids: List
        
        # 1. Pre-Filtering (상위 4배수만 대상으로 MMR 수행)
        pool_size = min(len(scores), top_k * 4) # MMR 풀 크기
        pool_scores, pool_indices = torch.topk(scores, k=pool_size) # 상위 점수와 인덱스
        pool_vectors = candidate_vectors[pool_indices] # 풀에 포함된 후보 벡터들

        selected_indices = [] # 선택된 아이템의 인덱스 리스트
        final_results = [] # 최종 추천 결과 리스트

        # 2. Greedy Selection 반복
        for _ in range(min(top_k, pool_size)):
            best_mmr = -float("inf") # 현재까지의 최고 MMR 점수
            best_idx = -1 # 최고 MMR 점수를 가진 인덱스

            for i in range(pool_size):
                if i in selected_indices:
                    continue

                # 점수 계산
                rel = pool_scores[i].item()  # Relevance (관련성 점수)
                redundancy = 0.0             # Redundancy (중복도 점수)

                if selected_indices:
                    current_vec = pool_vectors[i] # 현재 후보 벡터
                    selected_vecs = pool_vectors[selected_indices] # 이미 선택된 벡터들
                    sims = torch.matmul(selected_vecs, current_vec) # 코사인 유사도 계산
                    redundancy = torch.max(sims).item() # 최대 유사도를 중복도로 사용

                # MMR 공식: λ * Relevance - (1-λ) * Redundancy
                mmr_score = (self.lambda_param * rel) - (
                    (1.0 - self.lambda_param) * redundancy
                )

                if mmr_score > best_mmr:
                    best_mmr = mmr_score
                    best_idx = i

            # 베스트 선정 및 저장
            if best_idx != -1:
                selected_indices.append(best_idx)
                news_id = candidate_ids[pool_indices[best_idx].item()] # 선택된 뉴스 ID
                original_score = pool_scores[best_idx].item() # 원래 Relevance 점수
                
                final_results.append({
                    "news_id": news_id,
                    "score": original_score
                })

        return final_results
