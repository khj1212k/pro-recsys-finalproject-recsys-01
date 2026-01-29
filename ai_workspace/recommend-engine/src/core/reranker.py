import torch


class MMRReranker:
    """MMR(Maximal Marginal Relevance) 기반 재정렬기."""

    def __init__(self, lambda_param=0.7):
        self.lambda_param = lambda_param

    def rerank(self, scores, candidate_vectors, candidate_ids, top_k):
        """
        MMR 알고리즘을 적용하여 최종 추천 리스트를 반환함.
        
        Args:
            scores (Tensor): Scorer가 계산한 1차 점수 (M, )
            candidate_vectors (Tensor): 후보 벡터들 (M, Dim)
            candidate_ids (List): 후보 ID 리스트
            top_k (int): 최종 추천 개수
        """
        # 1. Pre-Filtering (상위 4배수만 대상으로 수행)
        pool_size = min(len(scores), top_k * 4)
        pool_scores, pool_indices = torch.topk(scores, k=pool_size)
        pool_vectors = candidate_vectors[pool_indices]

        selected_indices = []
        final_results = []

        # 2. Greedy Selection loop
        for _ in range(min(top_k, pool_size)):
            best_mmr = -float("inf")
            best_idx = -1

            for i in range(pool_size):
                if i in selected_indices:
                    continue

                # 점수 계산
                rel = pool_scores[i].item()  # 관련성 (Relevance)
                redundancy = 0.0             # 중복도 (Redundancy)

                if selected_indices:
                    current_vec = pool_vectors[i]
                    selected_vecs = pool_vectors[selected_indices]
                    sims = torch.matmul(selected_vecs, current_vec)
                    redundancy = torch.max(sims).item()

                # MMR 공식
                mmr_score = (self.lambda_param * rel) - (
                    (1.0 - self.lambda_param) * redundancy
                )

                if mmr_score > best_mmr:
                    best_mmr = mmr_score
                    best_idx = i

            # 베스트 선정 및 저장
            if best_idx != -1:
                selected_indices.append(best_idx)
                news_id = candidate_ids[pool_indices[best_idx].item()]
                original_score = pool_scores[best_idx].item()
                
                final_results.append({
                    "news_id": news_id,
                    "score": original_score
                })

        return final_results
