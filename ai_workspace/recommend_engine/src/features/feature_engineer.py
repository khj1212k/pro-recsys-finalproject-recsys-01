# src/features/feature_engineer.py
import pandas as pd
import numpy as np
from datetime import datetime
from typing import List, Dict, Any, Optional
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm

from ..data.data_loader import DataLoader
from ..utils.common import get_logger

logger = get_logger("FeatureEngineer")

class FeatureEngineer:
    def __init__(self, data_loader: DataLoader):
        self.data_loader = data_loader
        self.config = data_loader.config
        
        logger.info("🛠️ Feature Engineer 초기화 중...")
        
        # 데이터 미리 로드 (Memory Caching)
        # NewsItem 객체 내에 category_ids와 embedding이 이미 포함되어 있음
        self.news_dict = self.data_loader.load_embedded_news()
        
        # 유저 프로필 로드 (카테고리 정보 포함)
        self.user_profiles = self.data_loader.build_user_profiles()
        
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

    def create_features(self, 
                        user_ids: List[int], 
                        news_ids: List[int], 
                        labels: List[int] = None, 
                        timestamps: List[datetime] = None) -> pd.DataFrame:
        """
        (User, News) 쌍 리스트를 받아 Feature DataFrame 반환
        
        Args:
            user_ids: 유저 ID 리스트
            news_ids: 뉴스 ID 리스트
            labels: 정답 레이블 (Optional)
            timestamps: [New] 피처 계산의 기준이 되는 시간 리스트 (로그 발생 시점 또는 가상 시점)
        """
        if len(user_ids) != len(news_ids):
            raise ValueError("User IDs and News IDs must have the same length")

        features = []
        # timestamps가 없을 경우를 대비한 기본값 (현재 시간)
        default_now = datetime.now()

        # tqdm 설정
        iterator = zip(user_ids, news_ids)
        if len(user_ids) > 1000:
            iterator = tqdm(iterator, total=len(user_ids), desc="Generating Features", ncols=80)

        for i, (uid, nid) in enumerate(iterator):
            user_profile = self.user_profiles.get(uid)
            news_item = self.news_dict.get(nid)
            
            # Cold Start / 데이터 누락 방어 로직
            if not user_profile or not news_item:
                continue

            row = {
                'user_id': uid,
                'news_id': nid
            }
            if labels is not None:
                row['label'] = labels[i]

            # ---------------------------------------------------------
            # 1. Recency Features (Point-in-Time Correctness 적용)
            # ---------------------------------------------------------
            # 시스템 시간이 아니라, 전달받은 '기준 시간(timestamps[i])' 사용
            ref_time = timestamps[i] if timestamps is not None else default_now
            
            # [New] 정렬을 위해 원본 시간도 잠시 저장 (모델 입력 전 반드시 삭제해야 함)
            row['_timestamp'] = ref_time

            # 뉴스 발행 시간
            news_time = self._parse_datetime(news_item.timestamp)
            
            # 시간 차이 계산 (기준 시간 - 발행 시간)
            # 미래 데이터(음수)가 나올 경우 0으로 처리 (로그 시간 오차 등 방어)
            diff_hours = (ref_time - news_time).total_seconds() / 3600.0
            diff_hours = max(0.0, diff_hours) 
            
            row['hours_since_published'] = diff_hours
            row['is_fresh_24h'] = 1 if diff_hours <= 24 else 0
            row['is_fresh_7d'] = 1 if diff_hours <= 168 else 0

            # ---------------------------------------------------------
            # 2. Semantic Features (Embedding Similarity)
            # ---------------------------------------------------------
            sim_score = self._calculate_cosine_sim(user_profile.history_embedding, news_item.embedding)
            row['history_cosine_similarity'] = sim_score

            # ---------------------------------------------------------
            # 3. Explicit Match Features (Category)
            # ---------------------------------------------------------
            # 시간 감쇠 없이 단순 집합(Set) 연산 수행
            user_cats = set(user_profile.onboarding_categories)
            news_cats = set(news_item.category_ids) if news_item.category_ids else set()
            
            # 교집합 개수
            match_cnt = len(user_cats.intersection(news_cats))
            
            row['category_match_count'] = match_cnt
            row['is_category_match'] = 1 if match_cnt > 0 else 0
            
            # 뉴스 카테고리 (대표 카테고리 하나만 Feature로 사용)
            row['news_category_repr'] = list(news_cats)[0] if news_cats else 1

            # ---------------------------------------------------------
            # 4. User Meta Features
            # ---------------------------------------------------------
            row['user_age_band'] = user_profile.age_band_idx
            row['user_gender'] = user_profile.gender_idx
            row['user_onboarding_cnt'] = len(user_profile.onboarding_categories)

            features.append(row)

        return pd.DataFrame(features)