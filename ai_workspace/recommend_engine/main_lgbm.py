# main_lgbm.py
import os
import argparse
import pandas as pd
import numpy as np
import pickle
import lightgbm as lgb
from datetime import datetime
from sqlalchemy import text

from src.data.data_loader import DataLoader
from src.data.lgbm_dataset import LGBMDataset
from src.features.feature_engineer import FeatureEngineer
from src.models.lgbm_ranker import LGBMRanker
from src.core.reranker import create_reranker_from_config
from src.utils.common import load_config, get_logger

logger = get_logger("MainExecutor")

def get_db_max_timestamp(loader: DataLoader) -> datetime:
    """
    [Debug Mode] DB에서 가장 최근 로그의 시간을 조회하여 '가상 현재 시간'으로 사용
    """
    query = "SELECT MAX(created_at) as max_ts FROM user_newsletter_ctr_log"
    try:
        with loader.engine.connect() as conn:
            result = conn.execute(text(query)).fetchone()
            if result and result[0]:
                return pd.to_datetime(result[0])
    except Exception as e:
        logger.warning(f"⚠️ DB 시간 조회 실패: {e}")
    
    # 실패하거나 로그가 없으면 현재 시간 반환
    return datetime.now()

def train_pipeline(config: dict, loader: DataLoader):
    """학습 파이프라인: 데이터 로드 -> 시간순 정렬 -> 분할 -> 컬럼 삭제 -> 학습 -> 저장"""
    logger.info("🚀 [Train] 파이프라인 시작")
    
    # 1. 초기화
    fe = FeatureEngineer(loader)
    dataset = LGBMDataset(loader, fe)
    ranker = LGBMRanker(params=config['lightgbm']['params'])
    
    # 2. 데이터셋 생성 (Negative Sampling 포함 전체 데이터)
    # LGBMDataset -> FeatureEngineer를 거쳐 '_timestamp' 컬럼이 포함된 DF가 반환됨
    neg_ratio = config['lightgbm'].get('negative_sample_ratio', 5)
    full_df = dataset.create_train_dataset(neg_ratio=neg_ratio)
    
    if full_df.empty:
        logger.error("❌ 학습 데이터가 없습니다. (Cold Start or DB Empty)")
        return

    # 3. [완벽주의] Dynamic Time-Sorted Split
    if '_timestamp' in full_df.columns:
        # (1) 시간순 정렬
        full_df = full_df.sort_values(by='_timestamp', ascending=True)
        
        # (2) 분할 (Validation Ratio 기반)
        val_ratio = config['data'].get('validation_ratio', 0.2)
        split_idx = int(len(full_df) * (1 - val_ratio))
        
        logger.info(f"✂️ 시간순 데이터 분할 (Ratio: {val_ratio}, Split Index: {split_idx})")
        
        # copy()를 써서 SettingWithCopyWarning 방지
        train_df = full_df.iloc[:split_idx].copy()
        valid_df = full_df.iloc[split_idx:].copy()
        
        logger.info(f"   Train 기간: {train_df['_timestamp'].min()} ~ {train_df['_timestamp'].max()}")
        logger.info(f"   Valid 기간: {valid_df['_timestamp'].min()} ~ {valid_df['_timestamp'].max()}")

        # (3) [핵심] Timestamp 컬럼 제거 (모델 노이즈 방지)
        logger.info("🗑️ 학습 직전 _timestamp 컬럼 제거 (모델 오염 방지)")
        train_df.drop(columns=['_timestamp'], inplace=True)
        valid_df.drop(columns=['_timestamp'], inplace=True)
        
    else:
        logger.warning("⚠️ _timestamp 컬럼이 없습니다. 정렬 없이 분할합니다.")
        val_ratio = config['data'].get('validation_ratio', 0.2)
        split_idx = int(len(full_df) * (1 - val_ratio))
        train_df = full_df.iloc[:split_idx]
        valid_df = full_df.iloc[split_idx:]

    logger.info(f"   Train: {len(train_df)}건, Valid: {len(valid_df)}건")

    # 4. 학습
    logger.info("🏋️ LightGBM 학습 시작...")
    ranker.train(train_df, valid_df=valid_df)
    
    # 5. 저장
    model_dir = config['output']['checkpoint_dir']
    os.makedirs(model_dir, exist_ok=True)
    model_path = os.path.join(model_dir, "lgbm_model.pkl")
    
    with open(model_path, 'wb') as f:
        pickle.dump(ranker.model, f)
    logger.info(f"💾 모델 저장 완료: {model_path}")

def inference_pipeline(config: dict, loader: DataLoader):
    """추론 파이프라인: 시간 설정 -> 데이터 생성 -> 컬럼 삭제 -> 예측 -> Reranking -> 저장"""
    logger.info("🔮 [Inference] 파이프라인 시작")
    
    # 1. 모델 로드
    model_path = os.path.join(config['output']['checkpoint_dir'], "lgbm_model.pkl")
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"모델 파일이 없습니다: {model_path} (--train 먼저 실행)")
    
    ranker = LGBMRanker(params=config['lightgbm']['params'])
    with open(model_path, 'rb') as f:
        ranker.model = pickle.load(f)

    # 2. 기준 시간(Virtual Now) 설정
    env = config.get('execution_env', 'debug')
    if env == 'production':
        eval_time = datetime.now()
        logger.info(f"⏱️ [Production] 기준 시간: {eval_time} (현재 시간)")
    else:
        # Debug 모드: DB 마지막 로그 시간 자동 감지
        eval_time = get_db_max_timestamp(loader)
        logger.info(f"⏱️ [Debug] 기준 시간: {eval_time} (DB 마지막 로그 기준)")

    # 3. 데이터셋 생성 (User x News Cartesian Product)
    fe = FeatureEngineer(loader)
    dataset = LGBMDataset(loader, fe)
    
    # 타겟 유저: 전체 유저 (None)
    inference_df = dataset.create_inference_dataset(
        target_user_ids=None, 
        eval_timestamp=eval_time
    )
    
    if inference_df.empty:
        logger.warning("⚠️ 추론 대상 데이터가 생성되지 않았습니다.")
        return

    # 4. 예측 (Scoring)
    
    # 추론 데이터에서도 _timestamp 컬럼 제거
    if '_timestamp' in inference_df.columns:
        inference_df.drop(columns=['_timestamp'], inplace=True)

    logger.info("🧠 스코어링 수행 중...")
    scored_df = ranker.predict(inference_df)
    
    # 5. Reranking (MMR)
    use_mmr = config['recommendation']['use_mmr']
    top_k = config['recommendation']['top_k']
    
    final_recs_list = []
    
    if use_mmr:
        logger.info("⚖️ MMR Reranking 적용 중...")
        reranker = create_reranker_from_config(config)
        
        # 필요한 데이터 로드 (메모리 효율을 위해 필요한 것만)
        news_dict = loader.load_embedded_news()
        pref_cats = loader.load_user_preferred_categories()
        user_cat_counts = pref_cats.groupby('user_id')['category_id'].count().to_dict()
        
        # 유저별 처리
        for uid, group in scored_df.groupby('user_id'):
            # 점수순 정렬
            group = group.sort_values('score', ascending=False)
            
            # 임베딩 준비
            valid_nids = [nid for nid in group['news_id'] if nid in news_dict]
            if not valid_nids:
                continue
                
            filtered_group = group[group['news_id'].isin(valid_nids)]
            scores = filtered_group['score'].values
            embeddings = np.array([news_dict[nid].embedding for nid in valid_nids])
            
            # MMR 수행
            num_cats = user_cat_counts.get(uid, 0)
            selected = reranker.rerank_for_user(
                scores=scores,
                embeddings=embeddings,
                top_k=top_k,
                num_preferred_categories=num_cats
            )
            
            # 결과 저장
            for idx, _ in selected:
                rec_nid = valid_nids[idx]
                final_recs_list.append({'user_id': uid, 'news_letter_id': rec_nid})
                
    else:
        # Top-K 단순 정렬
        sorted_df = scored_df.sort_values(['user_id', 'score'], ascending=[True, False])
        top_k_df = sorted_df.groupby('user_id').head(top_k)
        top_k_df = top_k_df.rename(columns={'news_id': 'news_letter_id'})
        final_recs_list = top_k_df[['user_id', 'news_letter_id']].to_dict('records')

    # 6. 결과 저장
    result_df = pd.DataFrame(final_recs_list)
    logger.info(f"📝 최종 추천 결과: {len(result_df)}건 (유저 {result_df['user_id'].nunique()}명)")
    
    loader.save_inference_results(result_df)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--train', action='store_true', help='학습 모드 실행')
    parser.add_argument('--inference', action='store_true', help='추론 모드 실행')
    args = parser.parse_args()

    # 설정 로드
    config = load_config()
    loader = DataLoader(config)

    # 모드별 실행
    if args.train:
        train_pipeline(config, loader)
    
    if args.inference:
        inference_pipeline(config, loader)
        
    if not args.train and not args.inference:
        logger.warning("실행 모드를 선택해주세요: --train 또는 --inference")

if __name__ == "__main__":
    main()