# src/features/feature_engineer.py
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List, Dict, Any
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm

from ..data.data_loader import DataLoader

class FeatureEngineer:
    def __init__(self, data_loader: DataLoader):
        self.data_loader = data_loader
        self.config = data_loader.config
        
        print("🛠️ Feature Engineer 초기화 중...")
        # 속도 향상을 위해 데이터 미리 로드 (Memory Caching)
        self.news_dict = self.data_loader.load_embedded_news()
        self.user_profiles = self.data_loader.build_user_profiles()
        
        # 카테고리 매핑 로드
        self.newsletter_categories = self.data_loader.load_newsletter_categories_map()
        # 뉴스 ID별 카테고리 리스트 딕셔너리 변환
        self.news_cat_map = self.newsletter_categories.groupby('news_letter_id')['category_id'].apply(list).to_dict()

    def _calculate_cosine_sim(self, vec1: np.ndarray, vec2: np.ndarray) -> float:
        """코사인 유사도 안전하게 계산"""
        if vec1 is None or vec2 is None:
            return 0.0
        # 0 벡터 처리
        if np.all(vec1 == 0) or np.all(vec2 == 0):
            return 0.0
            
        # 1D -> 2D reshape
        if vec1.ndim == 1: vec1 = vec1.reshape(1, -1)
        if vec2.ndim == 1: vec2 = vec2.reshape(1, -1)
            
        return float(cosine_similarity(vec1, vec2)[0][0])

    def _parse_datetime(self, dt_val) -> datetime:
        """DB(datetime객체)와 CSV(str) 모두 대응하는 날짜 파싱"""
        if isinstance(dt_val, str):
            try:
                return datetime.fromisoformat(dt_val)
            except ValueError:
                return datetime.strptime(dt_val, "%Y-%m-%d %H:%M:%S")
        return dt_val

    def create_features(self, user_ids: List[int], news_ids: List[int], labels: List[int] = None) -> pd.DataFrame:
        """
        (User, News) 쌍 리스트를 받아 Feature DataFrame 반환
        """
        if len(user_ids) != len(news_ids):
            raise ValueError("User IDs and News IDs must have the same length")

        features = []
        now = datetime.now()

        # 진행률 표시와 함께 루프
        iterator = zip(user_ids, news_ids)
        if len(user_ids) > 1000:
            iterator = tqdm(iterator, total=len(user_ids), desc="Generating Features", ncols=80)

        for i, (uid, nid) in enumerate(iterator):
            user_profile = self.user_profiles.get(uid)
            news_item = self.news_dict.get(nid)
            
            # Cold Start 방어 로직 (데이터가 없으면 스킵)
            if not user_profile or not news_item:
                continue

            row = {
                'user_id': uid,
                'news_id': nid
            }
            if labels is not None:
                row['label'] = labels[i]

            # ---------------------------------------------------------
            # 1. Recency Features (Time Decay 핵심)
            # ---------------------------------------------------------
            news_time = self._parse_datetime(news_item.timestamp)
            # 시간 차이 (시간 단위)
            diff_hours = (now - news_time).total_seconds() / 3600.0
            diff_hours = max(0, diff_hours)
            
            row['hours_since_published'] = diff_hours
            row['is_fresh_24h'] = 1 if diff_hours <= 24 else 0
            row['is_fresh_7d'] = 1 if diff_hours <= 168 else 0

            # ---------------------------------------------------------
            # 2. Semantic Features (Embedding Similarity)
            # ---------------------------------------------------------
            # user_profile.history_embedding은 이미 Time Decay가 적용된 Weighted Average 벡터임
            sim_score = self._calculate_cosine_sim(user_profile.history_embedding, news_item.embedding)
            row['history_cosine_similarity'] = sim_score

            # ---------------------------------------------------------
            # 3. Explicit Match Features (Category)
            # ---------------------------------------------------------
            user_cats = set(user_profile.onboarding_categories)
            news_cats = set(self.news_cat_map.get(nid, []))
            
            # 교집합 개수
            match_cnt = len(user_cats.intersection(news_cats))
            row['category_match_count'] = match_cnt
            row['is_category_match'] = 1 if match_cnt > 0 else 0
            
            # 뉴스 카테고리 (대표 카테고리 하나만 Feature로 사용 - 모델이 카테고리 편향 학습)
            row['news_category_repr'] = list(news_cats)[0] if news_cats else 0

            # ---------------------------------------------------------
            # 4. User Meta Features
            # ---------------------------------------------------------
            row['user_age_band'] = user_profile.age_band_idx
            row['user_gender'] = user_profile.gender_idx
            row['user_onboarding_cnt'] = len(user_profile.onboarding_categories)

            features.append(row)

        return pd.DataFrame(features)