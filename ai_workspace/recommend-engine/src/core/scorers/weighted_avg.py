import torch
from collections import Counter
from .base import BaseScorer


class WeightedAverageScorer(BaseScorer):
    """Weighted Average 기반 점수 계산기."""

    def calculate_weights(self, history_items, user_prefs):
        """
        [로직] Multi-Tag Frequency Sum 방식
        1. 전체 이력의 모든 태그 빈도 집계
        2. 개별 뉴스: (가진 태그들의 빈도 합) * (선호 카테고리 부스팅)
        """
        if not history_items:
            return []

        # 1. 전체 태그 수집 및 빈도 계산
        all_tags = []
        for item in history_items:
            cat_str = str(item.get('category', ''))
            tags = [t.strip() for t in cat_str.split(',')]
            all_tags.extend(tags)
        
        tag_counts = Counter(all_tags)

        # 2. 개별 아이템 가중치 산출
        weights = []
        for item in history_items:
            cat_str = str(item.get('category', ''))
            current_tags = [t.strip() for t in cat_str.split(',')]
            
            # A. 빈도 점수 (태그 빈도 합산)
            w = sum(tag_counts[t] for t in current_tags)
            
            # B. 선호 카테고리 부스팅
            is_preferred = False
            if isinstance(user_prefs, list):
                is_preferred = any(p in cat_str for p in user_prefs)
            elif isinstance(user_prefs, str):
                is_preferred = user_prefs in cat_str
            
            if is_preferred:
                w *= 1.5
            
            weights.append(float(w))
            
        return weights

    def score(self, user_vectors, candidate_vectors, weights=None):
        # 1. 대상 데이터 타입 확인
        target_dtype = candidate_vectors.dtype

        # 2. User Representation 생성
        if weights is None:
            user_rep = torch.mean(user_vectors, dim=0)
        else:
            # 가중 평균 연산
            weights = weights.view(-1, 1).to(user_vectors.device)
            
            weighted_sum = torch.sum(user_vectors * weights, dim=0)
            total_weight = torch.sum(weights) + 1e-9
            user_rep = weighted_sum / total_weight

        # 데이터 타입 맞춤 (float32 -> float16 등)
        if user_rep.dtype != target_dtype:
            user_rep = user_rep.to(dtype=target_dtype)

        # 3. 유사도 계산
        final_scores = torch.matmul(candidate_vectors, user_rep)

        return final_scores
