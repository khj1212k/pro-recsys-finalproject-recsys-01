# main_lgbm.py
import os
import json
import argparse
import pandas as pd
import numpy as np
import pickle
import lightgbm as lgb
from datetime import datetime
from sqlalchemy import text

from src.data.data_loader import DataLoader
from src.data.lgbm_dataset import LGBMDataset
from src.data.time_split import time_ordered_group_safe_split
from src.features.feature_engineer import FeatureEngineer
from src.models.lgbm_ranker import LGBMRanker
from src.core.reranker import create_reranker_from_config
from src.utils.common import load_config, get_logger

logger = get_logger("MainExecutor")


def save_model_version(model_dir: str, booster, version: str = None, metrics: dict = None) -> str:
    """모델을 LightGBM 네이티브 텍스트 포맷(.txt, booster.save_model())의 버전 파일로
    저장하고 latest_model.json 포인터를 갱신한다.

    CHANGE: 기존에는 pickle로 저장했다. pickle은 LightGBM/Python 버전이 바뀌면
    역직렬화가 깨질 수 있고 바이너리라 사람이 들여다볼 수도 없다. LightGBM 자체
    텍스트 포맷은 버전 간 호환성이 더 안정적이고, 필요하면 diff/grep도 가능하다.
    """
    if version is None:
        version = datetime.now().strftime("%Y%m%d_%H%M%S")

    os.makedirs(model_dir, exist_ok=True)
    model_filename = f"lgbm_model_{version}.txt"
    model_path = os.path.join(model_dir, model_filename)

    booster.save_model(model_path)

    pointer = {
        "version": version,
        "model_file": model_filename,
        "format": "lightgbm_text",
        "saved_at": datetime.now().isoformat(),
    }
    if metrics:
        pointer["metrics"] = metrics

    pointer_path = os.path.join(model_dir, "latest_model.json")
    with open(pointer_path, 'w', encoding='utf-8') as f:
        json.dump(pointer, f, indent=2)

    return model_path


def load_latest_model_path(model_dir: str) -> str:
    """latest_model.json 포인터가 가리키는 '현재 서빙 중인' 모델의 전체 경로를 반환한다."""
    pointer_path = os.path.join(model_dir, "latest_model.json")
    if not os.path.exists(pointer_path):
        raise FileNotFoundError(f"모델 포인터 파일이 없습니다: {pointer_path} (--train 먼저 실행)")
    with open(pointer_path, encoding='utf-8') as f:
        pointer = json.load(f)
    return os.path.join(model_dir, pointer["model_file"])


def load_booster(model_path: str) -> lgb.Booster:
    """모델 파일을 로드한다. .pkl 확장자면 CHANGE #17 이전(pickle) 버전 파일로 간주해
    하위 호환으로 pickle.load 폴백한다(사소한 분기라 굳이 별도 마이그레이션 없이 유지)."""
    if model_path.endswith('.pkl'):
        with open(model_path, 'rb') as f:
            return pickle.load(f)
    return lgb.Booster(model_file=model_path)


def log_mlflow_run(config: dict, ranker, extra_metrics: dict = None, tracking_uri: str = None) -> bool:
    """MLflow에 이번 학습 실행의 하이퍼파라미터/지표를 기록한다.

    mlflow는 선택적 의존성이다 - 설치되어 있지 않으면 경고만 남기고 False를 반환해
    학습 파이프라인 자체가 실패하지 않도록 한다(pip install -e ".[tracking]"으로 활성화).
    """
    try:
        import mlflow
    except ImportError:
        logger.warning("⚠️ mlflow가 설치되어 있지 않아 실험 기록을 건너뜁니다. (pip install -e \".[tracking]\")")
        return False

    tracking_uri = tracking_uri or os.getenv("MLFLOW_TRACKING_URI") or (
        "file:" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "mlruns")
    )
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("newsletter_lgbm_ranker")

    with mlflow.start_run():
        mlflow.log_params(config['lightgbm']['params'])
        mlflow.log_param('negative_sample_ratio', config['lightgbm'].get('negative_sample_ratio', 5))
        mlflow.log_param('validation_ratio', config['data'].get('validation_ratio', 0.2))

        best_score = getattr(ranker.model, 'best_score', None) or {}
        for dataset_name, metrics_dict in best_score.items():
            for metric_name, value in metrics_dict.items():
                mlflow.log_metric(f"{dataset_name}_{metric_name}", float(value))

        if extra_metrics:
            for k, v in extra_metrics.items():
                mlflow.log_metric(k, float(v))

    logger.info(f"📈 MLflow 실험 기록 완료 (tracking_uri={tracking_uri})")
    return True


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
    """학습 파이프라인: 데이터 로드 -> 시간순 정렬 -> 그룹 안전 분할 -> 학습 -> 저장"""
    logger.info("🚀 [Train] 파이프라인 시작")

    group_key = config.get('ranking', {}).get('group_key', 'user_timestamp')

    # 1. 초기화
    fe = FeatureEngineer(loader)
    dataset = LGBMDataset(loader, fe)
    ranker = LGBMRanker(params=config['lightgbm']['params'], group_key=group_key)

    # 2. 데이터셋 생성 (Negative Sampling 포함 전체 데이터)
    # LGBMDataset -> FeatureEngineer를 거쳐 '_timestamp' 컬럼이 포함된 DF가 반환됨
    neg_ratio = config['lightgbm'].get('negative_sample_ratio', 5)
    full_df = dataset.create_train_dataset(neg_ratio=neg_ratio)

    if full_df.empty:
        logger.error("❌ 학습 데이터가 없습니다. (Cold Start or DB Empty)")
        return

    # 3. [완벽주의] 시간순 정렬 + 그룹(CORRECTION #5: 기본은 (user_id, 클릭 시각)) 안전 분할
    if '_timestamp' in full_df.columns:
        val_ratio = config['data'].get('validation_ratio', 0.2)
        train_df, valid_df = time_ordered_group_safe_split(full_df, val_ratio, group_key=group_key)

        logger.info(f"✂️ 시간순 데이터 분할 (Ratio: {val_ratio}, group_key: {group_key}, Split Index: {len(train_df)})")
        logger.info(f"   Train 기간: {train_df['_timestamp'].min()} ~ {train_df['_timestamp'].max()}")
        logger.info(f"   Valid 기간: {valid_df['_timestamp'].min()} ~ {valid_df['_timestamp'].max()}")

        # [New] 평가 스크립트(evaluate_results.py)가 정확한 valid 기간을 알 수 있도록 저장
        valid_period = {
            "valid_start": valid_df['_timestamp'].min().isoformat(),
            "valid_end": valid_df['_timestamp'].max().isoformat(),
        }

        # 주의: _timestamp 컬럼은 여기서 제거하지 않는다. LGBMRanker.train()이 그룹
        # 계산(group_key='user_timestamp')에 이 컬럼을 쓰고, 모델 피처에서는 자체적으로
        # 제외하므로(drop_cols) 모델 오염 없이 안전하게 넘길 수 있다.

    else:
        logger.warning("⚠️ _timestamp 컬럼이 없습니다. 정렬 없이 분할합니다.")
        val_ratio = config['data'].get('validation_ratio', 0.2)
        split_idx = int(len(full_df) * (1 - val_ratio))
        train_df = full_df.iloc[:split_idx]
        valid_df = full_df.iloc[split_idx:]
        valid_period = None

    logger.info(f"   Train: {len(train_df)}건, Valid: {len(valid_df)}건")

    # 4. 학습
    logger.info("🏋️ LightGBM 학습 시작...")
    ranker.train(train_df, valid_df=valid_df)

    # 5. 저장 (LightGBM 네이티브 텍스트 포맷 버전 파일 + latest_model.json 포인터)
    model_dir = config['output']['checkpoint_dir']
    model_path = save_model_version(model_dir, ranker.model)
    logger.info(f"💾 모델 저장 완료: {model_path}")

    # [New] valid 기간 저장 (evaluate_results.py가 재사용)
    if valid_period is not None:
        period_path = os.path.join(model_dir, "valid_period.json")
        with open(period_path, 'w', encoding='utf-8') as f:
            json.dump(valid_period, f, indent=2)
        logger.info(f"💾 Valid 기간 저장 완료: {period_path}")

    # [New] MLflow에 하이퍼파라미터/검증 지표 기록 (mlflow 미설치 시 자동으로 건너뜀)
    log_mlflow_run(config, ranker)

def inference_pipeline(config: dict, loader: DataLoader):
    """추론 파이프라인: 시간 설정 -> 데이터 생성 -> 예측 -> Reranking -> 저장"""
    logger.info("🔮 [Inference] 파이프라인 시작")

    group_key = config.get('ranking', {}).get('group_key', 'user_timestamp')

    # 1. 모델 로드 (latest_model.json 포인터가 가리키는 최신 버전)
    model_path = load_latest_model_path(config['output']['checkpoint_dir'])

    ranker = LGBMRanker(params=config['lightgbm']['params'], group_key=group_key)
    ranker.model = load_booster(model_path)

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

    # 4. 예측 (Scoring) - _timestamp가 있어도 LGBMRanker.predict()가 피처에서 자체 제외함
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
