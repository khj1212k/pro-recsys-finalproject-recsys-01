"""
AI Workspace - LangGraph News Pipeline
뉴스 전처리, 임베딩, 클러스터링, 뉴스레터 생성을 위한 파이프라인
"""
import argparse
import sys
import os
import logging
from dotenv import load_dotenv

# 다른 python 파일을 import 할 때 현재 파일의 디렉토리가 sys.path에 포함되어야 함
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import Settings
from pipeline.runner import PipelineRunner
from db.schema import full_reset
from utils.logger import setup_logger

load_dotenv(override=False) # .env 파일 로드 (시스템 환경변수 우선)

# 루트 로거 설정 (전역 로깅 설정 초기화)
# - 이름("")을 비워두면 모든 로거의 부모인 Root Logger를 설정
# - level=logging.INFO: INFO 레벨 이상의 로그(INFO, WARNING, ERROR, CRITICAL)만 출력
setup_logger("", level=logging.INFO) # (DEBUG,) INFO, WARNING, ERROR, CRITICAL 전부 출력
logger = logging.getLogger(__name__)


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="AI Workspace News Pipeline")
    
    # Execution options
    parser.add_argument("--reset",      action="store_true",       help="Reset test_db (schema kept, data cleared)")
    parser.add_argument("--workers",    type=int, default=8,       help="Number of workers for extraction")
    parser.add_argument("--limit",      type=int, default=None,    help="Limit number of clusters to process")
    parser.add_argument("--min-target", type=int, default=None,    help="Minimum target number of newsletters (default: Settings.MIN_NEWSLETTER_TARGET)")
    parser.add_argument("--from-stage", type=int, default=1,       help="Start stage number (0-6)")
    parser.add_argument("--to-stage",   type=int, default=5,       help="End stage number (0-6)")

    # Model/Resource options
    parser.add_argument("--force-cpu",  action="store_true",       help="Force CPU for embeddings")
    parser.add_argument("--batch-size", type=int, default=None,    help="Batch size for embedding")

    # Clustering options
    # 기본값을 None으로 둬서 명시적으로 넘기지 않으면 Settings 값(HDBSCAN_MIN_CLUSTER_SIZE 등)이
    # 적용되도록 한다 (CLI > Settings > 기본값 우선순위, pipeline/stages.py에서 최종 해석).
    parser.add_argument("--min-cluster-size", type=int, default=None, help="HDBSCAN min_cluster_size (default: Settings.HDBSCAN_MIN_CLUSTER_SIZE)")
    parser.add_argument("--min-samples",      type=int, default=None, help="HDBSCAN min_samples (default: Settings.HDBSCAN_MIN_SAMPLES)")
    parser.add_argument("--cluster-lookback-hours", type=int, default=None,
                         help="클러스터링 대상 기사의 crawled_at lookback 시간 (default: Settings.CLUSTER_LOOKBACK_HOURS)")

    return parser.parse_args()


def main():
    """
    AI 작업공간(Workspace) 파이프라인의 메인 진입점(Entry Point)
    
    실행 흐름 (Flow):
    1. CLI 인자(Arguments) 파싱: 사용자가 입력한 옵션 확인
    2. 설정(Settings) 로드: 환경 변수 및 설정 파일 로드
    3. DB 초기화 확인: test_db인 경우 요청 시 DB 리셋 수행 (안전장치 포함)
    4. 파이프라인 러너(Runner) 초기화: 전체 실행 흐름 관리자 생성
    5. 전체 파이프라인 실행: 설정된 옵션에 따라 각 단계(Stage)를 순차적으로 실행
    """
    args = parse_args()
    
    logger.info(f"파이프라인 시작 (대상 DB: {os.getenv('DB_NAME')})")
    
    # DB 리셋 로직: 운영 DB 실수 방지를 위한 안전 확인
    if args.reset:
        cur_env = os.getenv("ENV", "dev")
        if cur_env == "prod":
            logger.error(f"🚫 [CRITICAL] 운영(prod) 환경에서는 DB 리셋이 절대 불가능.")
            sys.exit(1)

        db_name = os.getenv("DB_NAME", "")
        if db_name == 'test_db':
            logger.info(f"🗑️  데이터베이스 초기화 진행: {db_name} (ENV={cur_env})")
            full_reset()
        else:
            logger.warning(f"⚠️  전체 리셋 건너뜀 (DB_NAME={db_name} != test_db)")
    
    # 파이프라인 러너 초기화 및 실행
    # Runner는 모든 단계(유저 임베딩 -> 수집 -> 추출 -> 기사 임베딩 -> 뉴스레터 생성)를 조율
    runner = PipelineRunner(Settings)
    
    try:
        runner.run_full_pipeline(
            reset_db=args.reset,
            num_workers=args.workers,
            force_cpu=args.force_cpu,
            limit=args.limit,
            min_cluster_size=args.min_cluster_size,
            min_samples=args.min_samples,
            min_target=args.min_target,
            lookback_hours=args.cluster_lookback_hours,
            batch_size=args.batch_size,
            start_stage=args.from_stage,
            end_stage=args.to_stage
        )
        logger.info("✨ Pipeline completed successfully!")
        
    except KeyboardInterrupt:
        logger.warning("\n⚠️ Pipeline interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"❌ Pipeline failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
