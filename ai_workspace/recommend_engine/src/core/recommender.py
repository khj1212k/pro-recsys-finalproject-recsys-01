# src/core/recommender.py
"""
추천 생성 모듈
Two-Tower 모델 기반 추천 및 배치 출력
"""

import os
import json
import torch
import numpy as np
from datetime import datetime
from typing import Dict, List, Tuple, Optional, Any
from tqdm import tqdm

from ..data.data_loader import DataLoader, NUM_CATEGORIES
from ..models.two_tower import TwoTowerModel
from .reranker import CategoryBasedMMRReranker, create_reranker_from_config
from ..utils.common import get_project_root, ensure_dir, save_json


class Recommender:
    """
    Two-Tower 기반 추천 생성기
    
    Features:
        - 사용자별 개인화 추천
        - MMR 재정렬 (카테고리 개수 기반 λ)
        - News_Letter_Today_Batch 형식 출력
    """
    
    def __init__(
        self,
        model: TwoTowerModel,
        data_loader: DataLoader,
        config: Dict[str, Any],
        device: str = 'cuda'
    ):
        """
        Args:
            model: 학습된 TwoTowerModel
            data_loader: 데이터 로더
            config: 설정 딕셔너리
            device: 'cuda' or 'cpu'
        """
        self.model = model.to(device)
        self.model.eval()
        self.data_loader = data_loader
        self.config = config
        self.device = device
        
        # 추천 설정
        rec_config = config.get('recommendation', {})
        self.top_k = rec_config.get('top_k', 10)
        self.use_mmr = rec_config.get('use_mmr', True)
        
        # 출력 설정
        output_config = config.get('output', {})
        self.score_decimal = output_config.get('score_decimal_places', 4)
        
        # MMR Reranker
        if self.use_mmr:
            self.reranker = create_reranker_from_config(config)
        else:
            self.reranker = None
        
        # 아이템 임베딩 미리 계산
        self._item_embeddings: Optional[torch.Tensor] = None
        self._item_ids: Optional[List[int]] = None
    
    def _precompute_item_embeddings(self, candidate_news_ids: List[int]):
        """
        후보 아이템 임베딩 미리 계산
        
        Args:
            candidate_news_ids: 후보 뉴스 ID 리스트
        """
        self._item_ids = candidate_news_ids
        news_dict = self.data_loader.load_embedded_news()
        
        embeddings = []
        category_multihots = []
        
        for news_id in candidate_news_ids:
            news_item = news_dict.get(news_id)
            
            if news_item is None:
                emb = np.zeros(1024, dtype=np.float32)
                cat_mh = np.zeros(NUM_CATEGORIES, dtype=np.float32)
            else:
                emb = news_item.embedding
                if emb is None or len(emb) != 1024:
                    emb = np.zeros(1024, dtype=np.float32)
                cat_mh = self.data_loader.get_category_multihot(news_item.category_ids)
            
            embeddings.append(emb)
            category_multihots.append(cat_mh)
        
        # 텐서로 변환
        embeddings_tensor = torch.tensor(np.stack(embeddings), dtype=torch.float32).to(self.device)
        categories_tensor = torch.tensor(np.stack(category_multihots), dtype=torch.float32).to(self.device)
        
        # Item Tower로 임베딩 계산
        with torch.no_grad():
            batch = {
                'news_embedding': embeddings_tensor,
                'news_category_multihot': categories_tensor
            }
            self._item_embeddings = self.model.get_item_embedding(batch)
        
        print(f"✅ 아이템 임베딩 계산 완료: {len(candidate_news_ids)}개")
    
    def _get_user_features(self, user_id: int, reference_date: datetime = None) -> Dict[str, torch.Tensor]:
        """사용자 피처를 텐서로 변환"""
        profile = self.data_loader.get_user_profile(user_id)
        
        if profile is None:
            age_band_idx = 1
            gender_idx = 0
        else:
            age_band_idx = profile.age_band_idx
            gender_idx = profile.gender_idx
        
        category_prefs = self.data_loader.get_user_category_preferences(user_id, reference_date)
        history_emb = self.data_loader.get_user_history_embedding(user_id, reference_date)
        
        return {
            'age_band_idx': torch.tensor([age_band_idx], dtype=torch.long).to(self.device),
            'gender_idx': torch.tensor([gender_idx], dtype=torch.long).to(self.device),
            'category_preferences': torch.tensor([category_prefs], dtype=torch.float32).to(self.device),
            'history_embedding': torch.tensor([history_emb], dtype=torch.float32).to(self.device),
        }
    
    @torch.no_grad()
    def recommend_for_user(
        self,
        user_id: int,
        candidate_news_ids: List[int] = None,
        top_k: int = None,
        reference_date: datetime = None,
        use_mmr: bool = None
    ) -> List[Tuple[int, float]]:
        """
        단일 사용자 추천 생성
        
        Args:
            user_id: 사용자 ID
            candidate_news_ids: 후보 뉴스 ID (None이면 전체)
            top_k: 추천 개수 (None이면 config 값)
            reference_date: Time Decay 기준 날짜
            use_mmr: MMR 사용 여부 (None이면 config 값)
            
        Returns:
            [(news_id, score), ...] top_k개
        """
        if top_k is None:
            top_k = self.top_k
        if use_mmr is None:
            use_mmr = self.use_mmr
        
        # 후보 아이템
        if candidate_news_ids is None:
            candidate_news_ids = self.data_loader.get_all_news_ids()
        
        # 아이템 임베딩 계산 (필요시)
        if self._item_embeddings is None or self._item_ids != candidate_news_ids:
            self._precompute_item_embeddings(candidate_news_ids)
        
        # 사용자 피처
        user_features = self._get_user_features(user_id, reference_date)
        
        # User embedding
        user_emb = self.model.get_user_embedding(user_features)  # [1, 128]
        
        # 모든 아이템과의 점수 계산
        scores = self.model.compute_scores(user_emb, self._item_embeddings)  # [N]
        scores_np = scores.cpu().numpy()
        
        # MMR 재정렬
        if use_mmr and self.reranker is not None:
            num_cats = self.data_loader.get_user_preferred_category_count(user_id)
            item_embs_np = self._item_embeddings.cpu().numpy()
            
            reranked = self.reranker.rerank_for_user(
                scores_np, item_embs_np, top_k, num_cats
            )
            
            results = [(self._item_ids[idx], float(score)) for idx, score in reranked]
        else:
            # 단순 점수순 정렬
            top_indices = np.argsort(scores_np)[::-1][:top_k]
            results = [(self._item_ids[idx], float(scores_np[idx])) for idx in top_indices]
        
        return results
    
    def recommend_for_all_users(
        self,
        user_ids: List[int] = None,
        candidate_news_ids: List[int] = None,
        top_k: int = None,
        reference_date: datetime = None,
        use_mmr: bool = None,
        show_progress: bool = True
    ) -> Dict[int, List[Tuple[int, float]]]:
        """
        전체 사용자 배치 추천
        
        Args:
            user_ids: 사용자 ID 리스트 (None이면 전체)
            candidate_news_ids: 후보 뉴스 ID (None이면 전체)
            top_k: 추천 개수
            reference_date: Time Decay 기준 날짜
            use_mmr: MMR 사용 여부
            show_progress: 진행률 표시 여부
            
        Returns:
            {user_id: [(news_id, score), ...]}
        """
        if user_ids is None:
            user_ids = self.data_loader.get_all_user_ids()
        
        if candidate_news_ids is None:
            candidate_news_ids = self.data_loader.get_all_news_ids()
        
        # 아이템 임베딩 미리 계산
        self._precompute_item_embeddings(candidate_news_ids)
        
        recommendations = {}
        
        iterator = tqdm(user_ids, desc="Generating recommendations") if show_progress else user_ids
        
        for user_id in iterator:
            results = self.recommend_for_user(
                user_id=user_id,
                candidate_news_ids=candidate_news_ids,
                top_k=top_k,
                reference_date=reference_date,
                use_mmr=use_mmr
            )
            recommendations[user_id] = results
        
        return recommendations
    
    def generate_batch_output(
        self,
        recommendations: Dict[int, List[Tuple[int, float]]]
    ) -> List[Dict[str, Any]]:
        """
        News_Letter_Today_Batch 형식으로 변환
        
        Args:
            recommendations: {user_id: [(news_id, score), ...]}
            
        Returns:
            [
                {
                    "user_id": 1,
                    "news_letter_ids": [
                        {"news_letter_id": 103, "score": 0.9523},
                        ...
                    ]
                },
                ...
            ]
        """
        output = []
        
        for user_id, recs in recommendations.items():
            news_letter_ids = [
                {
                    "news_letter_id": news_id,
                    "score": round(score, self.score_decimal)
                }
                for news_id, score in recs
            ]
            
            output.append({
                "user_id": user_id,
                "news_letter_ids": news_letter_ids
            })
        
        return output
    
    def run_daily_batch(
        self,
        candidate_news_ids: List[int],
        output_dir: str = None,
        reference_date: datetime = None
    ) -> str:
        """
        일일 배치 추천 실행
        
        Args:
            candidate_news_ids: 당일 생성된 뉴스 ID 리스트
            output_dir: 결과 저장 디렉토리
            reference_date: 기준 날짜 (None이면 현재)
            
        Returns:
            저장된 파일 경로
        """
        if output_dir is None:
            output_dir = self.config.get('output', {}).get('results_dir', 'results')
        
        output_dir = ensure_dir(output_dir)
        
        if reference_date is None:
            reference_date = datetime.now()
        
        print(f"\n{'='*60}")
        print(f"Daily Batch Recommendation")
        print(f"Date: {reference_date.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Candidate news: {len(candidate_news_ids)}개")
        print(f"{'='*60}\n")
        
        # 전체 사용자 추천
        recommendations = self.recommend_for_all_users(
            candidate_news_ids=candidate_news_ids,
            reference_date=reference_date
        )
        
        # 출력 형식 변환
        batch_output = self.generate_batch_output(recommendations)
        
        # 저장
        timestamp = reference_date.strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(output_dir, f'batch_recommendations_{timestamp}.json')
        
        save_json(batch_output, output_path)
        
        print(f"\n✅ 배치 추천 완료!")
        print(f"   Users: {len(batch_output)}명")
        print(f"   Output: {output_path}")
        
        return output_path


def load_recommender(
    checkpoint_path: str,
    config: Dict[str, Any],
    device: str = 'cuda'
) -> Recommender:
    """
    체크포인트에서 Recommender 로딩
    
    Args:
        checkpoint_path: 모델 체크포인트 경로
        config: 설정 딕셔너리
        device: 디바이스
        
    Returns:
        Recommender 인스턴스
    """
    from ..models.two_tower import create_model_from_config
    from ..core.trainer import load_trained_model
    
    # 모델 생성 및 가중치 로딩
    model = create_model_from_config(config)
    model = load_trained_model(model, checkpoint_path, device)
    
    # 데이터 로더
    data_loader = DataLoader(config=config)
    
    # Recommender 생성
    recommender = Recommender(
        model=model,
        data_loader=data_loader,
        config=config,
        device=device
    )
    
    return recommender
