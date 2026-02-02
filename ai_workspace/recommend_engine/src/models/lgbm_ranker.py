# src/models/lgbm_ranker.py
import os
import lightgbm as lgb
import pandas as pd
import joblib
from typing import Dict, Any

class LGBMRanker:
    def __init__(self, model_path: str = "checkpoints/lgbm_model.pkl", config: Dict[str, Any] = None):
        self.model_path = model_path
        self.model = None
        
        # 기본 파라미터 (Config가 없을 경우 대비)
        default_params = {
            'objective': 'binary',
            'metric': 'auc',
            'boosting_type': 'gbdt',
            'num_leaves': 31,
            'learning_rate': 0.05,
            'feature_fraction': 0.9,
            'random_state': 42,
            'verbose': -1
        }
        self.num_boost_round = 1000
        self.early_stopping_rounds = 50
        
        # Config 적용
        if config and 'lightgbm' in config:
            lgbm_conf = config['lightgbm']
            # 파라미터 덮어쓰기
            if 'params' in lgbm_conf:
                default_params.update(lgbm_conf['params'])
            
            self.num_boost_round = lgbm_conf.get('num_boost_round', 1000)
            self.early_stopping_rounds = lgbm_conf.get('early_stopping_rounds', 50)
            print(f"⚙️ LightGBM 설정 로드 완료 (LR: {default_params['learning_rate']}, Leaves: {default_params['num_leaves']})")
            
        self.params = default_params

    def train(self, train_df: pd.DataFrame, valid_df: pd.DataFrame = None):
        """모델 학습"""
        # Feature와 Label 분리
        drop_cols = ['user_id', 'news_id', 'label']
        features = [c for c in train_df.columns if c not in drop_cols]
        
        X_train = train_df[features]
        y_train = train_df['label']
        
        print(f"🏋️ LightGBM 학습 시작 (Features: {len(features)}개)")
        print(f"   목록: {features}")

        lgb_train = lgb.Dataset(X_train, y_train)
        
        callbacks = [lgb.log_evaluation(period=100)]
        eval_set = []

        if valid_df is not None:
            X_valid = valid_df[features]
            y_valid = valid_df['label']
            lgb_valid = lgb.Dataset(X_valid, y_valid, reference=lgb_train)
            eval_set = [lgb_valid]
            
            # Early Stopping 적용
            callbacks.append(lgb.early_stopping(stopping_rounds=self.early_stopping_rounds))
            print(f"   ✅ 검증 데이터 감지: Early Stopping 활성화 (Patience: {self.early_stopping_rounds})")
        else:
            print("   ⚠️ 검증 데이터 없음: Early Stopping 비활성화")

        self.model = lgb.train(
            self.params,
            lgb_train,
            num_boost_round=self.num_boost_round,
            valid_sets=eval_set,
            callbacks=callbacks
        )
        
        os.makedirs(os.path.dirname(self.model_path), exist_ok=True)
        joblib.dump(self.model, self.model_path)
        print(f"💾 모델 저장 완료: {self.model_path}")

    def predict(self, inference_df: pd.DataFrame) -> pd.DataFrame:
        if self.model is None:
            self.load()

        drop_cols = ['user_id', 'news_id', 'label']
        features = [c for c in inference_df.columns if c not in drop_cols]
        
        X_test = inference_df[features]
        scores = self.model.predict(X_test)
        
        result_df = inference_df.copy()
        result_df['score'] = scores
        result_df = result_df.sort_values(['user_id', 'score'], ascending=[True, False])
        return result_df

    def load(self):
        if os.path.exists(self.model_path):
            self.model = joblib.load(self.model_path)
            print(f"📂 모델 로드 완료: {self.model_path}")
        else:
            raise FileNotFoundError(f"모델 파일이 없습니다: {self.model_path}")