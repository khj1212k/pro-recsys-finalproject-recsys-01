# scripts/evaluate_results.py
import os
import sys
import pandas as pd
import numpy as np
import json
import glob
from sqlalchemy import text
from datetime import datetime

# 1. 프로젝트 루트 경로 설정
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.data.data_loader import DataLoader
from src.core.evaluator import Evaluator
from src.utils.common import load_config, get_logger

logger = get_logger("ResultEvaluator")

def load_latest_result_csv(results_dir: str) -> str:
    """results 폴더에서 가장 최근 생성된 csv 파일 경로 반환"""
    list_of_files = glob.glob(os.path.join(results_dir, '*.csv')) 
    if not list_of_files:
        raise FileNotFoundError(f"'{results_dir}' 폴더에 CSV 파일이 없습니다.")
    latest_file = max(list_of_files, key=os.path.getctime)
    return latest_file

def evaluate():
    config = load_config()
    loader = DataLoader(config)
    
    # 1. 추천 결과 로드 (CSV)
    results_dir = config['output']['results_dir']
    try:
        csv_path = load_latest_result_csv(results_dir)
        logger.info(f"📂 추천 결과 파일 로드: {csv_path}")
        rec_df = pd.read_csv(csv_path)
    except Exception as e:
        logger.error(f"결과 파일을 찾을 수 없습니다: {e}")
        return

    # 유저별 추천 리스트 변환 {user_id: [item_id1, item_id2, ...]}
    # (이미 순서대로 정렬되어 있다고 가정)
    recommendations = rec_df.groupby('user_id')['news_letter_id'].apply(list).to_dict()
    
    # 2. 정답 데이터(Ground Truth) 로드 (Validation Set 재활용)
    # 전체 28일 중 20%는 대략 6일 정도임.
    days_ago = 6 
    logger.info(f"🔍 정답 데이터 로드 중 (최근 {days_ago}일 - Validation Set 근사)")
    
    # 최근 "days_ago"일치 유저-뉴스레터 클릭 로그 로드
    gt_query = text(f"""
        SELECT user_id, news_letter_id
        FROM user_newsletter_ctr_log
        WHERE created_at >= NOW() - INTERVAL '{days_ago} DAYS'
    """)
    
    gt_df = pd.read_sql(gt_query, loader.engine)
    
    if gt_df.empty:
        logger.warning("⚠️ 정답으로 사용할 로그 데이터가 없습니다.")
        return

    # 유저별 정답 셋 변환 {user_id: {item_id1, item_id2, ...}}
    ground_truth = gt_df.groupby('user_id')['news_letter_id'].apply(set).to_dict()
    logger.info(f"✅ 정답 데이터 로드 완료: {len(gt_df)}건 (유저 {len(ground_truth)}명)")

    # 3. 메타데이터 로드 (추천 결과 Coverage 계산용)
    logger.info("📚 카테고리 정보 로드 중...")
    news_items = loader.load_embedded_news()
    item_categories = {nid: item.category_ids for nid, item in news_items.items()}

    # 4. 평가 수행
    logger.info("🚀 평가 지표 계산 시작...")
    evaluator = Evaluator(k_values=[5, 10, 20])
    metrics = evaluator.evaluate_all_users(recommendations, ground_truth, item_categories)
    
    # 5. 결과 출력 및 저장
    print("\n" + "="*50)
    print(" [ Evaluation Results ]")
    print("="*50)
    print(evaluator.format_metrics(metrics))
    print("="*50 + "\n")
    
    # JSON 저장
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(results_dir, f"metrics_{timestamp}.json")
    
    # numpy float 등 직렬화 문제 해결을 위해 float() 변환
    serializable_metrics = {k: float(v) for k, v in metrics.items()}
    
    with open(save_path, 'w', encoding='utf-8') as f:
        json.dump(serializable_metrics, f, indent=2)
        
    logger.info(f"💾 평가 결과 저장 완료: {save_path}")

if __name__ == "__main__":
    evaluate()