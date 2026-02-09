# src/models/lgbm_ranker.py
import lightgbm as lgb
import pandas as pd
from typing import Dict, Any
from ..utils.common import get_logger

# 로거 설정
logger = get_logger("LGBMRanker")

class LGBMRanker:
    def __init__(self, params: Dict[str, Any] = None):
        """LightGBM 랭커 초기화 및 하이퍼파라미터 설정"""
        self.model = None
        
        # 기본 파라미터
        self.params = {
            'objective': 'binary',
            'metric': 'auc',
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
            'feature_fraction': 0.9,
            'random_state': 42,
            'verbose': -1
        }
        
        # 전달받은 파라미터로 덮어쓰기
        if params:
            self.params.update(params)
            
        # Early Stopping 등 학습 제어 파라미터는 params에서 분리하거나 별도 관리 가능하지만,
        # 여기서는 params 내부에 섞여있다고 가정하거나 기본값 사용
        self.num_boost_round = 1000
        self.early_stopping_rounds = 50
        
        logger.info(f"⚙️ LightGBM 설정: LR={self.params.get('learning_rate')}, Leaves={self.params.get('num_leaves')}")

    def train(self, train_df: pd.DataFrame, valid_df: pd.DataFrame = None):
        """LightGBM 모델 학습 (Train/Valid 및 Early Stopping 지원)"""
        # Feature와 Label 분리
        # (주의: _timestamp 컬럼은 main_lgbm.py에서 이미 drop 되었으므로 걱정 X)
        drop_cols = ['user_id', 'news_id', 'label']
        features = [c for c in train_df.columns if c not in drop_cols]
        
        X_train = train_df[features]
        y_train = train_df['label']
        
        logger.info(f"🏋️ 학습 시작 (Features: {len(features)}개)")
        # logger.info(f"   Feature List: {features}") # 너무 길면 주석 처리

        lgb_train = lgb.Dataset(X_train, y_train)
        
        callbacks = [lgb.log_evaluation(period=100)]
        eval_set = []

        if valid_df is not None:
            X_valid = valid_df[features]
            y_valid = valid_df['label']
            lgb_valid = lgb.Dataset(X_valid, y_valid, reference=lgb_train)
            eval_set = [lgb_valid]
            
            # Early Stopping
            callbacks.append(lgb.early_stopping(stopping_rounds=self.early_stopping_rounds))
            logger.info(f"   ✅ 검증 데이터 감지: Early Stopping 활성화 (Patience: {self.early_stopping_rounds})")
        else:
            logger.info("   ⚠️ 검증 데이터 없음: Early Stopping 비활성화")

        # 학습 수행
        self.model = lgb.train(
            self.params,
            lgb_train,
            num_boost_round=self.num_boost_round,
            valid_sets=eval_set,
            callbacks=callbacks
        )
        logger.info("✅ 모델 학습 완료")

    def predict(self, inference_df: pd.DataFrame) -> pd.DataFrame:
        """추론 데이터에 대한 클릭 확률(Score) 예측 수행"""
        if self.model is None:
            raise ValueError("모델이 로드되지 않았습니다.")

        drop_cols = ['user_id', 'news_id', 'label', 'score'] # score가 혹시 있으면 제외
        features = [c for c in inference_df.columns if c not in drop_cols]
        
        # 예측
        X_test = inference_df[features]
        scores = self.model.predict(X_test)
        
        # 결과 정리
        result_df = inference_df.copy()
        result_df['score'] = scores
        
        # User별, 점수별 정렬
        result_df = result_df.sort_values(['user_id', 'score'], ascending=[True, False])
        
        return result_df