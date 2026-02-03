# src/core/evaluator.py
"""
추천 시스템 평가 모듈 (Clean Version)
Precision, Recall, MRR, nDCG, Coverage 등 계산
"""

import numpy as np
from typing import Dict, List, Set, Tuple, Any
from collections import defaultdict


class Evaluator:
    """
    추천 결과 평가 지표 계산기
    """
    
    def __init__(self, k_values: List[int] = [5, 10, 20]):
        self.k_values = k_values
    
    @staticmethod
    def precision_at_k(recommended: List[int], relevant: Set[int], k: int) -> float:
        if k <= 0 or not recommended: return 0.0
        top_k = recommended[:k]
        hits = sum(1 for item in top_k if item in relevant)
        return hits / k
    
    @staticmethod
    def recall_at_k(recommended: List[int], relevant: Set[int], k: int) -> float:
        if not relevant or k <= 0: return 0.0
        top_k = recommended[:k]
        hits = sum(1 for item in top_k if item in relevant)
        return hits / len(relevant)
    
    @staticmethod
    def mrr(recommended: List[int], relevant: Set[int]) -> float:
        for i, item in enumerate(recommended):
            if item in relevant:
                return 1.0 / (i + 1)
        return 0.0
    
    @staticmethod
    def dcg_at_k(recommended: List[int], relevant: Set[int], k: int) -> float:
        if k <= 0: return 0.0
        dcg = 0.0
        for i, item in enumerate(recommended[:k]):
            if item in relevant:
                dcg += 1.0 / np.log2(i + 2)
        return dcg
    
    @staticmethod
    def ndcg_at_k(recommended: List[int], relevant: Set[int], k: int) -> float:
        if not relevant or k <= 0: return 0.0
        dcg = Evaluator.dcg_at_k(recommended, relevant, k)
        ideal_relevant = list(relevant)[:k]
        idcg = sum(1.0 / np.log2(i + 2) for i in range(min(len(ideal_relevant), k)))
        return 0.0 if idcg == 0 else dcg / idcg
    
    @staticmethod
    def category_coverage(
        recommended: List[int],
        item_categories: Dict[int, List[int]],
        total_categories: int = 7
    ) -> float:
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
        metrics = {}
        metrics['mrr'] = self.mrr(recommended, relevant)
        
        for k in self.k_values:
            metrics[f'precision@{k}'] = self.precision_at_k(recommended, relevant, k)
            metrics[f'recall@{k}'] = self.recall_at_k(recommended, relevant, k)
            metrics[f'ndcg@{k}'] = self.ndcg_at_k(recommended, relevant, k)
            if item_categories:
                metrics[f'coverage@{k}'] = self.category_coverage(recommended[:k], item_categories)
        return metrics
    
    def evaluate_all_users(
        self,
        recommendations: Dict[int, List[int]],
        ground_truth: Dict[int, Set[int]],
        item_categories: Dict[int, List[int]] = None
    ) -> Dict[str, float]:
        all_metrics = defaultdict(list)
        
        for user_id, recommended in recommendations.items():
            if user_id not in ground_truth: continue
            relevant = ground_truth[user_id]
            if not relevant: continue
            
            user_metrics = self.evaluate_user(recommended, relevant, item_categories)
            for k, v in user_metrics.items():
                all_metrics[k].append(v)
        
        avg_metrics = {k: np.mean(v) if v else 0.0 for k, v in all_metrics.items()}
        avg_metrics['num_users'] = len(all_metrics.get('mrr', []))
        return avg_metrics
    
    def format_metrics(self, metrics: Dict[str, float], decimal: int = 4) -> str:
        lines = []
        if 'mrr' in metrics:
            lines.append(f"MRR: {metrics['mrr']:.{decimal}f}")
        for k in self.k_values:
            parts = []
            keys = [f'precision@{k}', f'recall@{k}', f'ndcg@{k}', f'coverage@{k}']
            labels = [f'P@{k}', f'R@{k}', f'nDCG@{k}', f'Cov@{k}']
            for key, label in zip(keys, labels):
                if key in metrics:
                    parts.append(f"{label}: {metrics[key]:.{decimal}f}")
            if parts:
                lines.append(" | ".join(parts))
        return "\n".join(lines)