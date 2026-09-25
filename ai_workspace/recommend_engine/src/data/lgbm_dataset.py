# src/data/lgbm_dataset.py
import bisect
import random
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from tqdm import tqdm

from ..features.feature_engineer import FeatureEngineer
from .data_loader import DataLoader
from ..utils.common import get_logger

logger = get_logger("LGBMDataset")


def _sample_negatives(rng: random.Random, candidate_news_ids: List[int], excluded_ids: set, neg_ratio: int) -> List[int]:
    """candidate_news_ids(이미 point-in-time으로 필터링된 후보 풀)에서 excluded_ids(유저가
    이미 클릭한 뉴스)를 제외하고 neg_ratio개의 negative news_id를 샘플링한다."""
    sampled = []
    count = 0
    safety_break = 0
    while count < neg_ratio and safety_break < (neg_ratio * 10) and candidate_news_ids:
        rand_nid = rng.choice(candidate_news_ids)
        if rand_nid not in excluded_ids:
            sampled.append(rand_nid)
            count += 1
        safety_break += 1
    return sampled


class LGBMDataset:
    def __init__(self, data_loader: DataLoader, feature_engineer: FeatureEngineer, seed: Optional[int] = None):
        self.data_loader = data_loader
        self.fe = feature_engineer
        self._init_rng(seed)
        self._news_ids_by_time: Optional[List[int]] = None
        self._news_timestamps_by_time: Optional[List[datetime]] = None

    def _init_rng(self, seed: Optional[int] = None) -> None:
        # negative sampling 재현성을 위해 전역 random 대신 시드 고정된 로컬 인스턴스 사용.
        # 별도 seed가 없으면 LightGBM 학습에 이미 쓰이는 random_state(config.yaml)를 그대로 재사용.
        if seed is None:
            try:
                seed = self.data_loader.config['lightgbm']['params'].get('random_state', 42)
            except (AttributeError, KeyError, TypeError):
                seed = 42
        self._rng = random.Random(seed)

    def _build_news_time_index(self) -> None:
        """뉴스를 발행 시각(timestamp) 오름차순으로 1회만 정렬해둔다.
        news_dict는 FeatureEngineer가 이미 로드해둔 것을 재사용 - 별도 쿼리 없음."""
        items = sorted(self.fe.news_dict.items(), key=lambda kv: kv[1].timestamp)
        self._news_ids_by_time = [nid for nid, _ in items]
        self._news_timestamps_by_time = [item.timestamp for _, item in items]

    def _eligible_news_ids_as_of(self, cutoff_time: datetime) -> List[int]:
        """cutoff_time 시점에 이미 존재했던(news_letter_created_at <= cutoff_time) 뉴스
        id만 반환한다.

        CORRECTION #16: negative sampling이 시간을 무시하고 전체 뉴스 풀에서 뽑으면,
        '유저가 클릭한 시점에는 아직 존재하지도 않았던 미래 뉴스'가 negative로 섞여
        들어갈 수 있다. 모델이 "이 유저가 이 뉴스를 안 볼 것이다"를 학습하는 근거가
        실제로는 학습 시점 이후에 생긴 데이터라 point-in-time 원칙에 어긋난다.
        """
        if self._news_ids_by_time is None:
            self._build_news_time_index()
        idx = bisect.bisect_right(self._news_timestamps_by_time, cutoff_time)
        return self._news_ids_by_time[:idx]

    def create_train_dataset(self, neg_ratio: int = 5) -> pd.DataFrame:
        """
        학습용 데이터셋 생성 (Negative Sampling + Time Correctness 적용)
        neg_ratio: Positive Log 1개당 생성할 Negative 샘플 개수
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

        users_list = []
        news_list = []
        labels_list = []
        timestamps_list = [] # 시간 정보 리스트

        # 2. 샘플링 진행
        logger.info("⚡ Negative Sampling 및 데이터 구성 중...")
        for uid, nid, ts in tqdm(pos_data, desc="Sampling", ncols=80):
            # (1) Positive Sample
            users_list.append(uid)
            news_list.append(nid)
            labels_list.append(1)
            timestamps_list.append(ts) # 로그 발생 시간 그대로 사용

            # (2) Negative Sample (시드 고정된 로컬 rng로, 클릭 시점에 존재했던 뉴스 중에서만 샘플링)
            eligible_news_ids = self._eligible_news_ids_as_of(ts)
            for rand_nid in _sample_negatives(self._rng, eligible_news_ids, user_clicked_map.get(uid, set()), neg_ratio):
                users_list.append(uid)
                news_list.append(rand_nid)
                labels_list.append(0)
                timestamps_list.append(ts) # Positive와 '동일한 시간' 부여 (Point-in-Time)

        logger.info(f"✅ 샘플링 완료: Positive {len(pos_data)}개, Total {len(labels_list)}개")

        # 3. 피처 생성 호출
        # timestamps: 피처 엔지니어링(Recency) 및 Time-based Split(Train/Valid 분리)에 사용
        train_df = self.fe.create_features(
            user_ids=users_list,
            news_ids=news_list,
            labels=labels_list,
            timestamps=timestamps_list
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
