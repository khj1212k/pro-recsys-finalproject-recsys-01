import torch
from collections import Counter
from .base import BaseScorer


class WeightedAverageScorer(BaseScorer):
    """Weighted Average 기반 점수 계산"""

    def calculate_weights(self, history_items, user_prefs):
        """
        Multi-Tag Frequency Sum 방식으로 가중치 계산.
        1. 전체 이력의 모든 태그 빈도 집계
        2. 개별 뉴스: (가진 태그들의 빈도 합) * (선호 카테고리 부스팅)
        
        반환: 각 이력 아이템별 가중치 리스트
        """
        if not history_items:
            return []

        # 1. 전체 태그 수집 및 빈도 계산
        all_tags = [] # 모든 태그를 저장할 리스트
        for item in history_items:
            cat_str = str(item.get('category', ''))
            tags = [t.strip() for t in cat_str.split(',')]
            all_tags.extend(tags)
        
        tag_counts = Counter(all_tags) # 태그별 빈도수 딕셔너리

        # 2. 개별 아이템 가중치 산출
        weights = [] # 각 아이템의 가중치 리스트
        for item in history_items:
            cat_str = str(item.get('category', ''))
            current_tags = [t.strip() for t in cat_str.split(',')] # 현재 아이템의 태그 리스트
            
            # A. 빈도 점수 (태그 빈도 합산)
            w = sum(tag_counts[t] for t in current_tags) # 기본 가중치
            
            # B. 선호 카테고리 부스팅
            is_preferred = False # 선호 카테고리 포함 여부
            if isinstance(user_prefs, list):
                is_preferred = any(p in cat_str for p in user_prefs)
            elif isinstance(user_prefs, str):
                is_preferred = user_prefs in cat_str
            
            if is_preferred:
                w *= 1.5 # 선호 카테고리 포함 시 1.5배 부스팅
            
            weights.append(float(w))
            
        return weights

    def score(self, user_vectors, candidate_vectors, weights=None):
        """
        Weighted Average 방식으로 추천 점수 계산.
        1. User Representation 생성 (가중 평균 또는 단순 평균)
        2. 후보 벡터와의 유사도 계산 (내적)
        
        반환: 각 후보 뉴스에 대한 점수 Tensor (shape: (M,))
        """
        # 1. 대상 데이터 타입 확인 (FP16/FP32 등)
        target_dtype = candidate_vectors.dtype

        # 2. User Representation 생성
        if weights is None:
            # 가중치 없으면 단순 평균
            user_rep = torch.mean(user_vectors, dim=0) # 유저 대표 벡터 (평균)
        else:
            # 가중 평균 연산
            weights = weights.view(-1, 1).to(user_vectors.device) # 가중치를 열 벡터로 변환
            
            weighted_sum = torch.sum(user_vectors * weights, dim=0) # 가중합
            total_weight = torch.sum(weights) + 1e-9 # 가중치 합 (0 나누기 방지)
            user_rep = weighted_sum / total_weight # 유저 대표 벡터 (가중 평균)

        # 데이터 타입 맞춤 (FP32 -> FP16 등)
        if user_rep.dtype != target_dtype:
            user_rep = user_rep.to(dtype=target_dtype)

        # 3. 유사도 계산 (내적)
        final_scores = torch.matmul(candidate_vectors, user_rep) # 최종 유사도 점수 (shape: (M,))

        return final_scores
