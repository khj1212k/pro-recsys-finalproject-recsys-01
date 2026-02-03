# src/data/lgbm_dataset.py
import pandas as pd
import numpy as np
import random
from datetime import datetime
from typing import List, Optional
from tqdm import tqdm

from ..features.feature_engineer import FeatureEngineer
from .data_loader import DataLoader
from ..utils.common import get_logger

logger = get_logger("LGBMDataset")

class LGBMDataset:
    def __init__(self, data_loader: DataLoader, feature_engineer: FeatureEngineer):
        self.data_loader = data_loader
        self.fe = feature_engineer

    def create_train_dataset(self, neg_ratio: int = 5) -> pd.DataFrame:
        """
        학습용 데이터셋 생성 (Negative Sampling + Time Correctness 적용)
        Args:
            neg_ratio: Positive 1개당 생성할 Negative 샘플 개수
        """
        logger.info(f"📊 학습 데이터 생성 시작 (Negative Ratio 1:{neg_ratio})")
        
        # 1. DB에서 Positive Log 로드 (이미 28일치 필터링됨)
        # 컬럼: [user_id, news_letter_id, timestamp]
        pos_df = self.data_loader.load_ctr_logs() 
        
        # 유저별 클릭한 뉴스 ID 집합핑 (Negative Sampling 시 제외용)
        user_clicked_map = {}
        
        # DataFrame을 순회하며 Positive Pair 구성
        # values로 변환하여 속도 향상: [uid, nid, timestamp]
        pos_data = pos_df[['user_id', 'news_letter_id', 'timestamp']].values.tolist()
        
        for uid, nid, _ in pos_data:
            if uid not in user_clicked_map:
                user_clicked_map[uid] = set()
            user_clicked_map[uid].add(nid)

        # 전체 뉴스 ID 리스트 (여기서 랜덤 추출)
        all_news_ids = self.data_loader.get_all_news_ids()
        
        users_list = []
        news_list = []
        labels_list = []
        timestamps_list = [] # [New] 시간 정보 리스트

        # 2. 샘플링 진행
        logger.info("⚡ Negative Sampling 및 데이터 구성 중...")
        for uid, nid, ts in tqdm(pos_data, desc="Sampling", ncols=80):
            # (1) Positive Sample
            users_list.append(uid)
            news_list.append(nid)
            labels_list.append(1)
            timestamps_list.append(ts) # [New] 로그 발생 시간 그대로 사용
            
            # (2) Negative Sample (Random)
            # 해당 유저가 본 적 없는 뉴스 중에서 k개 뽑기
            count = 0
            safety_break = 0 
            
            while count < neg_ratio and safety_break < (neg_ratio * 10):
                rand_nid = random.choice(all_news_ids)
                if rand_nid not in user_clicked_map.get(uid, set()):
                    users_list.append(uid)
                    news_list.append(rand_nid)
                    labels_list.append(0)
                    timestamps_list.append(ts) # [New] Positive와 '동일한 시간' 부여 (Point-in-Time)
                    count += 1
                safety_break += 1

        logger.info(f"✅ 샘플링 완료: Positive {len(pos_data)}개, Total {len(labels_list)}개")
        
        # 3. 피처 생성 호출 (timestamps 전달)
        train_df = self.fe.create_features(
            user_ids=users_list, 
            news_ids=news_list, 
            labels=labels_list, 
            timestamps=timestamps_list # [New] 핵심 수정 사항
        )
        return train_df

    def create_inference_dataset(self, target_user_ids: List[int] = None, eval_timestamp: datetime = None) -> pd.DataFrame:
        """
        추론용 데이터셋 생성 (모든 User x News 조합)
        
        Args:
            target_user_ids: 추론 대상 유저 ID 리스트
            eval_timestamp: [New] 추론 기준 시간 (Virtual Time 또는 Real Time)
        """
        if eval_timestamp is None:
            eval_timestamp = datetime.now()
            
        if target_user_ids is None:
            target_user_ids = self.data_loader.get_all_user_ids()

        all_news_ids = self.data_loader.get_all_news_ids()
        
        # [Optimazation Note]
        # 실제 운영시에는 모든 뉴스가 아니라 '최근 7일치 뉴스' 등으로 후보를 좁혀야 함.
        # 현재는 디버깅 및 소규모 데이터셋을 가정하여 전체 조합 생성.
        
        users_list = []
        news_list = []
        
        # Cartesian Product (User x All News)
        for uid in target_user_ids:
            for nid in all_news_ids:
                users_list.append(uid)
                news_list.append(nid)
                
        logger.info(f"📊 추론 데이터 준비: {len(target_user_ids)}명 x {len(all_news_ids)}뉴스 = {len(users_list)}건 (기준 시간: {eval_timestamp})")
        
        # [New] 모든 행에 대해 동일한 '기준 시간' 적용
        timestamps_list = [eval_timestamp] * len(users_list)
        
        # 라벨 없이 피처만 생성
        inference_df = self.fe.create_features(
            user_ids=users_list, 
            news_ids=news_list, 
            labels=None,
            timestamps=timestamps_list # [New] 핵심 수정 사항
        )
        return inference_df