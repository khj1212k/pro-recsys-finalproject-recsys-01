# main_lgbm.py
import argparse
import os
import sys
import pandas as pd
import numpy as np
import json
from datetime import datetime
from tqdm import tqdm
from collections import defaultdict

# 프로젝트 루트 경로 설정
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from src.data.data_loader import DataLoader
from src.features.feature_engineer import FeatureEngineer
from src.data.lgbm_dataset import LGBMDataset
from src.models.lgbm_ranker import LGBMRanker
from src.core.reranker import create_reranker_from_config
from src.core.evaluator import Evaluator
from src.utils.common import load_config
from src.utils.logger import setup_logger

logger = setup_logger("MainExecutor")

def run_experiment(mode='train'):
    # 1. 설정 로드
    config = load_config()
    
    # 추천 설정
    rec_config = config.get('recommendation', {})
    top_k = rec_config.get('top_k', 20)
    use_mmr = rec_config.get('use_mmr', False)
    
    # 학습 설정
    lgbm_config = config.get('lightgbm', {})
    neg_ratio = lgbm_config.get('negative_sample_ratio', 5)
    
    logger.info(f"🚀 실험 시작 (Mode: {mode})")
    
    # 데이터 로드
    logger.info("🛠️ 데이터 및 피처 엔지니어링 초기화...")
    loader = DataLoader(config=config) 
    fe = FeatureEngineer(loader)
    dataset_maker = LGBMDataset(loader, fe)
    ranker = LGBMRanker(model_path="checkpoints/lgbm_model.pkl", config=config)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if mode == 'train':
        logger.info(f"📊 학습 데이터 생성 (Negative Ratio: 1:{neg_ratio})...")
        full_df = dataset_maker.create_train_dataset(neg_ratio=neg_ratio)
        
        # Shuffle & Split
        full_df = full_df.sample(frac=1, random_state=42).reset_index(drop=True)
        split_idx = int(len(full_df) * 0.8)
        train_df = full_df.iloc[:split_idx]
        valid_df = full_df.iloc[split_idx:]
        
        logger.info(f"🔪 데이터 분할 완료: Train {len(train_df)}건, Valid {len(valid_df)}건")
        ranker.train(train_df, valid_df)
        logger.info("✅ 학습 완료.")

    elif mode == 'inference':
        # ---------------------------------------------------------
        # 1. Inference & Reranking
        # ---------------------------------------------------------
        logger.info("🧠 추론 데이터셋 생성 중...")
        inference_df = dataset_maker.create_inference_dataset()
        
        logger.info("🔮 LightGBM 스코어링 수행 중...")
        scored_df = ranker.predict(inference_df)
        
        final_recs_list = [] # DataFrame 변환용 리스트 (모든 피처 포함)
        recommendations_dict = {} # 평가용 딕셔너리 {uid: [nid, nid...]}
        
        if use_mmr:
            logger.info("⚖️ MMR Reranking 적용 중 (다양성 확보)...")
            reranker = create_reranker_from_config(config)
            news_dict = loader.load_embedded_news()
            
            # 유저별 선호 카테고리 수 로드
            user_pref_cats = loader.load_user_preferred_categories()
            user_cat_counts = user_pref_cats.groupby('user_id')['category_id'].count().to_dict()
            
            unique_users = scored_df['user_id'].unique()
            
            for uid in tqdm(unique_users, desc="Reranking"):
                # 해당 유저 데이터 (점수 높은 순 정렬)
                user_df = scored_df[scored_df['user_id'] == uid].sort_values('score', ascending=False)
                
                scores = user_df['score'].values
                news_ids = user_df['news_id'].values
                embeddings = np.array([news_dict[nid].embedding for nid in news_ids])
                
                num_cats = user_cat_counts.get(uid, 0)
                
                # MMR 수행 (return: [(index_in_list, mmr_score), ...])
                selected_items = reranker.rerank_for_user(
                    scores=scores,
                    embeddings=embeddings,
                    top_k=top_k,
                    num_preferred_categories=num_cats
                )
                
                # 결과 정리
                user_rec_nids = []
                for list_idx, _ in selected_items:
                    # 원본 Row의 모든 Feature를 가져옴 (디버깅용)
                    row_data = user_df.iloc[list_idx].to_dict()
                    final_recs_list.append(row_data)
                    user_rec_nids.append(int(row_data['news_id']))
                
                recommendations_dict[uid] = user_rec_nids
                
            top_k_df = pd.DataFrame(final_recs_list)
            
        else:
            logger.info("⚡ MMR 미사용 (단순 스코어링)...")
            # 기존 로직: 점수순 정렬 후 상위 k개
            top_k_df = scored_df.sort_values(
                ['user_id', 'score'], ascending=[True, False]
            ).groupby('user_id').head(top_k)
            
            # 평가용 딕셔너리 변환
            for uid, group in top_k_df.groupby('user_id'):
                recommendations_dict[uid] = group['news_id'].astype(int).tolist()

        # ---------------------------------------------------------
        # 컬럼 이름 변경 및 순서 재정렬
        # news_id -> news_letter_id
        # ---------------------------------------------------------
        # 1. 컬럼명 변경
        if 'news_id' in top_k_df.columns:
            top_k_df.rename(columns={'news_id': 'news_letter_id'}, inplace=True)

        all_cols = top_k_df.columns.tolist()
        priority_cols = ['user_id', 'news_letter_id', 'score']
        
        # 나머지 피처 컬럼들 (순서 유지)
        feature_cols = [c for c in all_cols if c not in priority_cols]
        
        # 최종 순서 적용
        top_k_df = top_k_df[priority_cols + feature_cols]

        # ---------------------------------------------------------
        # 2. 결과 저장 (CSV & JSON)
        # ---------------------------------------------------------
        os.makedirs("results", exist_ok=True)
        
        # CSV 저장
        csv_filename = f"results/recs_{timestamp}.csv"
        top_k_df.to_csv(csv_filename, index=False)
        logger.info(f"💾 CSV 저장 완료 (Feature 포함): {csv_filename}")
        
        # JSON 저장 (created_at 제거, news_letter_ids로 변경)
        json_results = []
        for uid, nids in recommendations_dict.items():
            json_results.append({
                "user_id": int(uid),
                "news_letter_ids": nids
            })
            
        json_filename = f"results/recs_{timestamp}.json"
        with open(json_filename, 'w', encoding='utf-8') as f:
            json.dump(json_results, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 JSON 저장 완료: {json_filename}")

        # ---------------------------------------------------------
        # 3. 성능 평가 (Ground Truth가 있을 경우에만)
        # ---------------------------------------------------------
        logger.info("📊 성능 평가 수행 여부 확인 중...")
        
        try:
            # Valid Logs 로드 시도
            valid_logs = loader.load_ctr_logs('valid')
            
            if not valid_logs.empty:
                logger.info("🎯 검증 데이터(Validation Log) 발견! 평가를 시작합니다.")
                
                # Ground Truth 생성
                ground_truth = defaultdict(set)
                for _, row in valid_logs.iterrows():
                    ground_truth[int(row['user_id'])].add(int(row['news_letter_id']))
                
                # 카테고리 정보 로드 (Coverage용)
                news_dict = loader.load_embedded_news()
                item_categories = {nid: item.category_ids for nid, item in news_dict.items()}
                
                # 평가 실행
                evaluator = Evaluator(k_values=[5, 10, 20])
                metrics = evaluator.evaluate_all_users(
                    recommendations_dict, # MMR 적용된 최종 결과
                    ground_truth,
                    item_categories
                )
                
                # 결과 출력 및 저장
                result_str = evaluator.format_metrics(metrics)
                print("\n" + "="*40)
                print(f"   최종 모델 평가 결과 (MMR: {use_mmr})   ")
                print("="*40)
                print(result_str)
                print("="*40)
                
                metrics_filename = f"results/metrics_{timestamp}.txt"
                with open(metrics_filename, "w") as f:
                    f.write(f"Timestamp: {timestamp}\n")
                    f.write(f"Method: LightGBM + MMR({use_mmr})\n")
                    f.write("="*40 + "\n")
                    f.write(result_str)
                    
                logger.info(f"📈 평가 지표 저장 완료: {metrics_filename}")
                
            else:
                logger.warning("⚠️ 검증 데이터가 비어있어 평가를 생략합니다.")
                
        except Exception as e:
            logger.warning(f"⚠️ 평가 과정 중 예외 발생 (결과는 저장됨): {e}")

        logger.info("✅ 모든 프로세스 완료.")
        logger.info(f"   --> DB 업로드 실행: python scripts/upload_to_db.py --file {json_filename}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--train', action='store_true', help='모델 학습 수행')
    parser.add_argument('--inference', action='store_true', help='추천 생성 수행')
    
    args = parser.parse_args()
    
    try:
        if args.train:
            run_experiment('train')
        elif args.inference:
            run_experiment('inference')
        else:
            print("사용법: python main_lgbm.py [--train | --inference]")
            sys.exit(1)
        sys.exit(0)
    except Exception as e:
        logger.exception("❌ 실행 중 치명적인 오류 발생")
        sys.exit(1)