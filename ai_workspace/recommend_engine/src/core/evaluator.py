# src/core/evaluator.py
"""
추천 시스템 평가 모듈
Precision, Recall, MRR, nDCG, Coverage 등 계산
"""

import numpy as np
from typing import Dict, List, Set, Tuple, Optional, Any
from collections import defaultdict


class Evaluator:
    """
    추천 결과 평가 지표
    
    Metrics:
        - Precision@K: 상위 K개 추천 중 정답 비율
        - Recall@K: 전체 정답 중 상위 K개에 포함된 비율
        - MRR: Mean Reciprocal Rank (첫 번째 정답의 역순위 평균)
        - nDCG@K: Normalized Discounted Cumulative Gain
        - Category Coverage: 추천 아이템의 카테고리 다양성
    """
    
    def __init__(self, k_values: List[int] = [5, 10, 20]):
        """
        Args:
            k_values: 평가할 K 값들
        """
        self.k_values = k_values
    
    @staticmethod
    def precision_at_k(recommended: List[int], relevant: Set[int], k: int) -> float:
        """Precision@K"""
        if k <= 0 or not recommended:
            return 0.0
        
        top_k = recommended[:k]
        hits = sum(1 for item in top_k if item in relevant)
        return hits / k
    
    @staticmethod
    def recall_at_k(recommended: List[int], relevant: Set[int], k: int) -> float:
        """Recall@K"""
        if not relevant or k <= 0:
            return 0.0
        
        top_k = recommended[:k]
        hits = sum(1 for item in top_k if item in relevant)
        return hits / len(relevant)
    
    @staticmethod
    def mrr(recommended: List[int], relevant: Set[int]) -> float:
        """Mean Reciprocal Rank"""
        for i, item in enumerate(recommended):
            if item in relevant:
                return 1.0 / (i + 1)
        return 0.0
    
    @staticmethod
    def dcg_at_k(recommended: List[int], relevant: Set[int], k: int) -> float:
        """DCG@K"""
        if k <= 0:
            return 0.0
        
        dcg = 0.0
        for i, item in enumerate(recommended[:k]):
            if item in relevant:
                dcg += 1.0 / np.log2(i + 2)  # i+2 because log2(1) = 0
        return dcg
    
    @staticmethod
    def ndcg_at_k(recommended: List[int], relevant: Set[int], k: int) -> float:
        """Normalized DCG@K"""
        if not relevant or k <= 0:
            return 0.0
        
        dcg = Evaluator.dcg_at_k(recommended, relevant, k)
        
        # Ideal DCG: 모든 관련 아이템이 상위에 있을 때
        ideal_relevant = list(relevant)[:k]
        idcg = sum(1.0 / np.log2(i + 2) for i in range(min(len(ideal_relevant), k)))
        
        if idcg == 0:
            return 0.0
        
        return dcg / idcg
    
    @staticmethod
    def category_coverage(
        recommended: List[int],
        item_categories: Dict[int, List[int]],
        total_categories: int = 7
    ) -> float:
        """
        Category Coverage: 추천된 아이템들이 커버하는 카테고리 비율
        
        Args:
            recommended: 추천 아이템 ID 리스트
            item_categories: {item_id: [category_ids]} 매핑
            total_categories: 전체 카테고리 수
        """
        covered = set()
        for item_id in recommended:
            if item_id in item_categories:
                covered.update(item_categories[item_id])
        
        return len(covered) / total_categories
    
    def evaluate_user(
        self,
        recommended: List[int],
        relevant: Set[int],
        item_categories: Dict[int, List[int]] = None
    ) -> Dict[str, float]:
        """
        단일 사용자 평가
        
        Args:
            recommended: 추천 아이템 리스트 (순위순)
            relevant: 정답 아이템 집합
            item_categories: 아이템별 카테고리 매핑
            
        Returns:
            메트릭 딕셔너리
        """
        metrics = {}
        
        # MRR
        metrics['mrr'] = self.mrr(recommended, relevant)
        
        # K별 메트릭
        for k in self.k_values:
            metrics[f'precision@{k}'] = self.precision_at_k(recommended, relevant, k)
            metrics[f'recall@{k}'] = self.recall_at_k(recommended, relevant, k)
            metrics[f'ndcg@{k}'] = self.ndcg_at_k(recommended, relevant, k)
            
            if item_categories:
                metrics[f'coverage@{k}'] = self.category_coverage(
                    recommended[:k], item_categories
                )
        
        return metrics
    
    def evaluate_all_users(
        self,
        recommendations: Dict[int, List[int]],
        ground_truth: Dict[int, Set[int]],
        item_categories: Dict[int, List[int]] = None
    ) -> Dict[str, float]:
        """
        전체 사용자 평균 평가
        
        Args:
            recommendations: {user_id: [recommended_items]}
            ground_truth: {user_id: {relevant_items}}
            item_categories: 아이템별 카테고리 매핑
            
        Returns:
            평균 메트릭 딕셔너리
        """
        all_metrics = defaultdict(list)
        
        for user_id, recommended in recommendations.items():
            if user_id not in ground_truth:
                continue
            
            relevant = ground_truth[user_id]
            if not relevant:
                continue
            
            user_metrics = self.evaluate_user(recommended, relevant, item_categories)
            
            for key, value in user_metrics.items():
                all_metrics[key].append(value)
        
        # 평균 계산
        avg_metrics = {}
        for key, values in all_metrics.items():
            avg_metrics[key] = np.mean(values) if values else 0.0
        
        avg_metrics['num_users'] = len(all_metrics.get('mrr', []))
        
        return avg_metrics
    
    def format_metrics(self, metrics: Dict[str, float], decimal: int = 4) -> str:
        """메트릭 포맷팅"""
        lines = []
        
        # MRR
        if 'mrr' in metrics:
            lines.append(f"MRR: {metrics['mrr']:.{decimal}f}")
        
        # K별 메트릭
        for k in self.k_values:
            parts = []
            if f'precision@{k}' in metrics:
                parts.append(f"P@{k}: {metrics[f'precision@{k}']:.{decimal}f}")
            if f'recall@{k}' in metrics:
                parts.append(f"R@{k}: {metrics[f'recall@{k}']:.{decimal}f}")
            if f'ndcg@{k}' in metrics:
                parts.append(f"nDCG@{k}: {metrics[f'ndcg@{k}']:.{decimal}f}")
            if f'coverage@{k}' in metrics:
                parts.append(f"Cov@{k}: {metrics[f'coverage@{k}']:.{decimal}f}")
            
            if parts:
                lines.append(" | ".join(parts))
        
        return "\n".join(lines)


class RankingEvaluator:
    """
    CTR 로그 기반 랭킹 평가
    """
    
    def __init__(self, data_loader, k_values: List[int] = [5, 10, 20]):
        """
        Args:
            data_loader: NewsDataLoader 인스턴스
            k_values: 평가할 K 값들
        """
        self.data_loader = data_loader
        self.evaluator = Evaluator(k_values)
        self.k_values = k_values
    
    def evaluate_from_ctr_logs(
        self,
        recommendations: Dict[int, List[int]],
        split: str = 'valid'
    ) -> Dict[str, float]:
        """
        CTR 로그에서 ground truth 추출하여 평가
        
        Args:
            recommendations: {user_id: [recommended_items]}
            split: 'train' or 'valid'
            
        Returns:
            평가 메트릭
        """
        ctr_df = self.data_loader.load_ctr_logs(split)
        
        # Ground truth 구축 (is_clicked=1인 아이템)
        ground_truth = defaultdict(set)
        for _, row in ctr_df.iterrows():
            if row['is_clicked'] == 1:
                ground_truth[int(row['user_id'])].add(int(row['news_letter_id']))
        
        # 아이템 카테고리 매핑
        news_dict = self.data_loader.load_embedded_news()
        item_categories = {
            nid: item.category_ids 
            for nid, item in news_dict.items()
        }
        
        # 평가
        metrics = self.evaluator.evaluate_all_users(
            recommendations,
            dict(ground_truth),
            item_categories
        )
        
        return metrics
