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


def _latest_batch_only(batch_df: pd.DataFrame) -> pd.DataFrame:
    """created_at 기준 가장 최신 배치(들)만 필터링 (여러 user가 같은 배치 시각을 가짐)"""
    if batch_df.empty:
        return batch_df
    latest_ts = batch_df['created_at'].max()
    return batch_df[batch_df['created_at'] == latest_ts]


def _expand_news_letter_ids(batch_df: pd.DataFrame) -> pd.DataFrame:
    """news_letter_ids(JSON 문자열 또는 이미 파싱된 리스트) 컬럼을 (user_id, news_letter_id) row로 펼침"""
    rows = []
    for _, row in batch_df.iterrows():
        nids = row['news_letter_ids']
        if isinstance(nids, str):
            nids = json.loads(nids)
        for nid in nids:
            rows.append({'user_id': row['user_id'], 'news_letter_id': int(nid)})
    return pd.DataFrame(rows, columns=['user_id', 'news_letter_id'])


def load_valid_period(checkpoint_dir: str):
    """main_lgbm.py의 train_pipeline이 저장한 정확한 valid 기간(JSON)을 읽는다.
    파일이 없으면 None을 반환 (호출부에서 근사치로 폴백)."""
    period_path = os.path.join(checkpoint_dir, "valid_period.json")
    if not os.path.exists(period_path):
        return None
    with open(period_path, 'r', encoding='utf-8') as f:
        raw = json.load(f)
    return {
        "valid_start": datetime.fromisoformat(raw["valid_start"]),
        "valid_end": datetime.fromisoformat(raw["valid_end"]),
    }


def load_latest_recommendations(loader: DataLoader, config: dict) -> pd.DataFrame:
    """실행 모드(debug=CSV / production=DB)에 맞춰 최신 추천 결과를 (user_id, news_letter_id) 형태로 로드"""
    mode = config.get('execution_env', 'debug')

    if mode == 'production':
        logger.info("📂 [Production] news_letter_today_batch 테이블에서 최신 추천 결과 로드")
        query = text("""
            SELECT user_id, news_letter_ids, created_at
            FROM news_letter_today_batch
            ORDER BY created_at DESC
        """)
        batch_df = pd.read_sql(query, loader.engine)
        if batch_df.empty:
            raise FileNotFoundError("news_letter_today_batch 테이블에 추론 결과가 없습니다. --inference를 먼저 실행하세요.")
        latest_df = _latest_batch_only(batch_df)
        return _expand_news_letter_ids(latest_df)

    results_dir = config['output']['results_dir']
    csv_path = load_latest_result_csv(results_dir)
    logger.info(f"📂 [Debug] 추천 결과 파일 로드: {csv_path}")
    return pd.read_csv(csv_path)


def evaluate():
    config = load_config()
    loader = DataLoader(config)

    # 1. 추천 결과 로드 (CSV 또는 DB, execution_env에 따라 분기)
    try:
        rec_df = load_latest_recommendations(loader, config)
    except Exception as e:
        logger.error(f"결과를 불러올 수 없습니다: {e}")
        return

    recommendations = rec_df.groupby('user_id')['news_letter_id'].apply(list).to_dict()

    # 2. 정답 데이터(Ground Truth) 로드 (실제 Validation Set 기간을 우선 사용, 없으면 근사치로 폴백)
    checkpoint_dir = config['output']['checkpoint_dir']
    valid_period = load_valid_period(checkpoint_dir)

    if valid_period is not None:
        valid_start = valid_period["valid_start"]
        logger.info(f"🔍 정답 데이터 로드 중 (실제 Validation 기간: {valid_start} ~ {valid_period['valid_end']})")
        gt_query = text("""
            SELECT user_id, news_letter_id
            FROM user_newsletter_ctr_log
            WHERE created_at >= :valid_start
        """)
        gt_df = pd.read_sql(gt_query, loader.engine, params={"valid_start": valid_start})
    else:
        # 폴백: valid_period.json이 없으면(구버전 모델) 기존 근사치 방식 사용
        days_ago = 6
        logger.warning(f"⚠️ valid_period.json을 찾을 수 없어 근사치(최근 {days_ago}일)로 폴백합니다. "
                        f"--train을 다시 실행하면 정확한 기간이 저장됩니다.")
        gt_query = text(f"""
            SELECT user_id, news_letter_id
            FROM user_newsletter_ctr_log
            WHERE created_at >= NOW() - INTERVAL '{days_ago} DAYS'
        """)
        gt_df = pd.read_sql(gt_query, loader.engine)

    if gt_df.empty:
        logger.warning("⚠️ 정답으로 사용할 로그 데이터가 없습니다.")
        return

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

    results_dir = config['output']['results_dir']
    os.makedirs(results_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = os.path.join(results_dir, f"metrics_{timestamp}.json")

    # numpy float 등 직렬화 문제 해결을 위해 float() 변환
    serializable_metrics = {k: float(v) for k, v in metrics.items()}

    with open(save_path, 'w', encoding='utf-8') as f:
        json.dump(serializable_metrics, f, indent=2)

    logger.info(f"💾 평가 결과 저장 완료: {save_path}")

if __name__ == "__main__":
    evaluate()
