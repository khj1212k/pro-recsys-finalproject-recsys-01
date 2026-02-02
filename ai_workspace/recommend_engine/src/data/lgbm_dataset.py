# src/data/lgbm_dataset.py
import pandas as pd
import numpy as np
import random
from typing import List
from ..features.feature_engineer import FeatureEngineer
from .data_loader import DataLoader

class LGBMDataset:
    def __init__(self, data_loader: DataLoader, feature_engineer: FeatureEngineer):
        self.data_loader = data_loader
        self.fe = feature_engineer

    def create_train_dataset(self, neg_ratio: int = 5) -> pd.DataFrame:
        """
        학습용 데이터셋 생성 (Negative Sampling 적용)
        Args:
            neg_ratio: Positive 1개당 생성할 Negative 샘플 개수
        """
        print(f"📊 학습 데이터 생성 시작 (Negative Ratio 1:{neg_ratio})")
        
        # 1. DB에서 Positive Log 로드
        # load_ctr_logs('train')은 timestamp 기준 80% 앞부분 데이터를 가져옵니다.
        pos_df = self.data_loader.load_ctr_logs('train')
        
        # 유저별 클릭한 뉴스 ID 집합핑 (Negative Sampling 시 제외용)
        # DB 컬럼명 주의: news_letter_id
        user_clicked_map = {}
        # is_clicked가 있으면 1인 것만, 없으면 전체를 Positive로 가정
        if 'is_clicked' in pos_df.columns:
            pos_df = pos_df[pos_df['is_clicked'] == 1]
            
        pos_pairs = pos_df[['user_id', 'news_letter_id']].values.tolist()
        
        for uid, nid in pos_pairs:
            if uid not in user_clicked_map:
                user_clicked_map[uid] = set()
            user_clicked_map[uid].add(nid)

        # 전체 뉴스 ID 리스트 (여기서 랜덤 추출)
        all_news_ids = list(self.data_loader.load_embedded_news().keys())
        
        users_list = []
        news_list = []
        labels_list = []

        # 2. 샘플링 진행
        for uid, nid in pos_pairs:
            # (1) Positive Sample
            users_list.append(uid)
            news_list.append(nid)
            labels_list.append(1)
            
            # (2) Negative Sample (Random)
            # 해당 유저가 본 적 없는 뉴스 중에서 k개 뽑기
            count = 0
            # 무한루프 방지용 안전장치
            safety_break = 0 
            
            while count < neg_ratio and safety_break < (neg_ratio * 10):
                rand_nid = random.choice(all_news_ids)
                if rand_nid not in user_clicked_map[uid]:
                    users_list.append(uid)
                    news_list.append(rand_nid)
                    labels_list.append(0)
                    count += 1
                safety_break += 1

        print(f"✅ 샘플링 완료: Positive {len(pos_pairs)}개, Total {len(labels_list)}개")
        
        # 3. 피처 생성 호출
        train_df = self.fe.create_features(users_list, news_list, labels_list)
        return train_df

    def create_inference_dataset(self, target_user_ids: List[int] = None) -> pd.DataFrame:
        """
        추론용 데이터셋 생성 (모든 User x 모든 News 조합)
        """
        if target_user_ids is None:
            # 타겟 유저가 없으면 전체 유저 로드
            users_df = self.data_loader.load_users()
            target_user_ids = users_df['user_id'].tolist()

        all_news_ids = list(self.data_loader.load_embedded_news().keys())
        
        users_list = []
        news_list = []

        # Cartesian Product (User x All News)
        # 100명 * 200뉴스 = 20,000행 (메모리 충분)
        for uid in target_user_ids:
            for nid in all_news_ids:
                users_list.append(uid)
                news_list.append(nid)
                
        print(f"📊 추론 데이터 준비: {len(target_user_ids)}명 대상, 총 {len(users_list)}건")
        
        # 라벨 없이 피처만 생성
        inference_df = self.fe.create_features(users_list, news_list, labels=None)
        return inference_df