# src/models/lgbm_ranker.py
import lightgbm as lgb
import pandas as pd
from typing import Any, Dict, List, Tuple
from ..data.time_split import compute_group_ids
from ..utils.common import get_logger

# 로거 설정
logger = get_logger("LGBMRanker")

class LGBMRanker:
    def __init__(self, params: Dict[str, Any] = None, group_key: str = "user_timestamp"):
        """LightGBM 랭커 초기화 및 하이퍼파라미터 설정.

        objective를 'lambdarank'로 설정해 유저 내 아이템 간 상대적 순서를 직접
        최적화한다(point-wise binary 분류가 아닌 실제 랭킹 손실 사용).

        group_key(CORRECTION): LambdaRank의 '쿼리 그룹'을 어떻게 묶을지 결정한다
        (config.yaml의 ranking.group_key, 기본값 'user_timestamp'). 유저 전체 이력을
        하나의 그룹으로 묶으면(레거시 'user_id') 학습 기간 전체가 한 쿼리가 되어버려
        "한 번의 노출에서 어떤 아이템이 더 관련있었는가"라는 LambdaRank의 전제와
        맞지 않고, main_lgbm.py의 시간순 Train/Valid 분할과도 충돌한다(그룹이 분할
        경계에 걸쳐 잘림). 기본값은 (user_id, 클릭 시각)로 묶어 lgbm_dataset.py가
        만드는 '한 번의 노출(positive + sampled negatives)' 단위와 일치시킨다.
        """
        self.model = None
        self.group_key = group_key

        # 기본 파라미터
        self.params = {
            'objective': 'lambdarank',
            'metric': 'ndcg',
            'ndcg_eval_at': [5, 10],
            'label_gain': [0, 1],  # label(0/1)에 대한 relevance gain
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

        self.num_boost_round = 1000
        self.early_stopping_rounds = 50

        logger.info(
            f"⚙️ LightGBM 설정: objective={self.params.get('objective')}, "
            f"group_key={self.group_key}, LR={self.params.get('learning_rate')}, "
            f"Leaves={self.params.get('num_leaves')}"
        )

    def _build_groups(self, df: pd.DataFrame, group_key: str = None) -> Tuple[pd.DataFrame, List[int]]:
        """LightGBM lambdarank가 요구하는 '그룹(쿼리) 단위로 연속된 행 + group 크기 배열'을 만든다.

        LightGBM은 데이터가 그룹(쿼리) 단위로 연속되어 있어야 하므로, group_key
        기준으로 정렬한 뒤 각 그룹이 몇 개 행을 갖는지 순서대로 센다.
        """
        group_key = group_key or self.group_key
        group_ids = compute_group_ids(df, group_key)
        sorted_df = df.assign(_group_id=group_ids.values).sort_values(
            '_group_id', kind='mergesort'
        ).reset_index(drop=True)
        groups = sorted_df.groupby('_group_id', sort=False).size().tolist()
        sorted_df = sorted_df.drop(columns=['_group_id'])
        return sorted_df, groups

    def train(self, train_df: pd.DataFrame, valid_df: pd.DataFrame = None):
        """LightGBM 모델 학습 (Train/Valid 및 Early Stopping, LambdaRank group 지원)"""
        # _timestamp는 그룹 계산에만 쓰이고 피처로는 절대 쓰이지 않는다(모델 오염 방지).
        drop_cols = ['user_id', 'news_id', 'label', '_timestamp']

        train_df, train_groups = self._build_groups(train_df)
        features = [c for c in train_df.columns if c not in drop_cols]

        X_train = train_df[features]
        y_train = train_df['label']

        logger.info(f"🏋️ 학습 시작 (Features: {len(features)}개, Groups: {len(train_groups)}개)")

        lgb_train = lgb.Dataset(X_train, y_train, group=train_groups)

        callbacks = [lgb.log_evaluation(period=100)]
        eval_set = []

        if valid_df is not None:
            valid_df, valid_groups = self._build_groups(valid_df)
            X_valid = valid_df[features]
            y_valid = valid_df['label']
            lgb_valid = lgb.Dataset(X_valid, y_valid, reference=lgb_train, group=valid_groups)
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
        """추론 데이터에 대한 랭킹 스코어(relevance score) 예측 수행.

        주의: lambdarank의 score는 [0,1] 확률이 아니라 상대적 순위를 나타내는
        unbounded 스코어다. reranker.py의 MMR 로직은 이미 후보군 내 min-max
        정규화를 수행하므로 하위 호환된다.
        """
        if self.model is None:
            raise ValueError("모델이 로드되지 않았습니다.")

        drop_cols = ['user_id', 'news_id', 'label', 'score', '_timestamp']  # score가 혹시 있으면 제외
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
