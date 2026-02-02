"""
Update User Embeddings Script
사용자 임베딩 업데이트 독립 실행 스크립트

사용법:
    # 전체 사용자 업데이트
    python update_user_embeddings.py --all
    
    # 특정 사용자만 업데이트
    python update_user_embeddings.py --user-id 123
    
    # 여러 사용자 업데이트
    python update_user_embeddings.py --user-ids 1,2,3,4,5
    
    # 최소 상호작용 수 설정
    python update_user_embeddings.py --all --min-interactions 3
"""

import argparse
import logging
import sys
from typing import List

from core.user_embedder import UserEmbedder

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def update_single_user(
    user_id: int,
    preference_ratio: float = 0.4,
    decay_rate: float = 0.05,
    lookback_days: int = 90
) -> bool:
    """
    단일 사용자 임베딩 업데이트
    
    Args:
        user_id: 사용자 ID
        preference_ratio: 선호도 가중치 비율
        decay_rate: 시간 감쇠율
        lookback_days: 읽기 이력 조회 기간
        
    Returns:
        성공 여부
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"사용자 임베딩 업데이트: User ID {user_id}")
    logger.info(f"{'='*80}\n")
    
    embedder = UserEmbedder(
        preference_weight_ratio=preference_ratio,
        decay_rate=decay_rate,
        lookback_days=lookback_days
    )
    
    # 임베딩 생성
    embedding = embedder.generate_user_embedding(user_id)
    
    if embedding is None:
        logger.error(f"❌ User {user_id} 임베딩 생성 실패")
        return False
    
    # DB 저장
    success = embedder.update_user_embedding_db(user_id, embedding)
    
    if success:
        logger.info(f"✅ User {user_id} 임베딩 업데이트 완료")
    else:
        logger.error(f"❌ User {user_id} 임베딩 저장 실패")
    
    return success


def update_multiple_users(
    user_ids: List[int],
    preference_ratio: float = 0.4,
    decay_rate: float = 0.05,
    lookback_days: int = 90
) -> dict:
    """
    여러 사용자 임베딩 업데이트
    
    Args:
        user_ids: 사용자 ID 리스트
        preference_ratio: 선호도 가중치 비율
        decay_rate: 시간 감쇠율
        lookback_days: 읽기 이력 조회 기간
        
    Returns:
        업데이트 결과 통계
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"다중 사용자 임베딩 업데이트: {len(user_ids)}명")
    logger.info(f"{'='*80}\n")
    
    embedder = UserEmbedder(
        preference_weight_ratio=preference_ratio,
        decay_rate=decay_rate,
        lookback_days=lookback_days
    )
    
    results = embedder.batch_update_all_users(user_ids=user_ids)
    
    return results


def update_all_users(
    min_interactions: int = 1,
    preference_ratio: float = 0.4,
    decay_rate: float = 0.05,
    lookback_days: int = 90,
    only_null: bool = True
) -> dict:
    """
    전체 사용자 임베딩 업데이트
    
    Args:
        min_interactions: 최소 상호작용 수
        preference_ratio: 선호도 가중치 비율
        decay_rate: 시간 감쇠율
        lookback_days: 읽기 이력 조회 기간
        only_null: 임베딩이 NULL인 사용자만 처리
        
    Returns:
        업데이트 결과 통계
    """
    logger.info(f"\n{'='*80}")
    logger.info(f"전체 사용자 임베딩 업데이트")
    logger.info(f"  임베딩 NULL만: {'예' if only_null else '아니오 (전체)'}")
    logger.info(f"  최소 상호작용: {min_interactions}")
    logger.info(f"  선호도 비율: {preference_ratio}")
    logger.info(f"  감쇠율: {decay_rate}")
    logger.info(f"  조회 기간: {lookback_days}일")
    logger.info(f"{'='*80}\n")
    
    embedder = UserEmbedder(
        preference_weight_ratio=preference_ratio,
        decay_rate=decay_rate,
        lookback_days=lookback_days,
        min_interactions=min_interactions
    )
    
    results = embedder.batch_update_all_users(user_ids=None, only_null=only_null)
    
    return results


def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(
        description='사용자 임베딩 업데이트 스크립트',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
사용 예시:
  # 전체 사용자 업데이트
  python update_user_embeddings.py --all
  
  # 특정 사용자 업데이트
  python update_user_embeddings.py --user-id 123
  
  # 여러 사용자 업데이트
  python update_user_embeddings.py --user-ids 1,2,3,4,5
  
  # 최소 상호작용 수 설정
  python update_user_embeddings.py --all --min-interactions 3
  
  # 파라미터 조정
  python update_user_embeddings.py --all --preference-ratio 0.5 --decay-rate 0.1
        """
    )
    
    # 업데이트 대상
    parser.add_argument(
        '--all',
        action='store_true',
        help='전체 사용자 업데이트'
    )
    
    parser.add_argument(
        '--user-id',
        type=int,
        help='단일 사용자 ID'
    )
    
    parser.add_argument(
        '--user-ids',
        type=str,
        help='쉼표로 구분된 사용자 ID 리스트 (예: 1,2,3,4,5)'
    )
    
    # 파라미터
    parser.add_argument(
        '--min-interactions',
        type=int,
        default=1,
        help='최소 상호작용 수 (기본값: 1)'
    )
    
    parser.add_argument(
        '--preference-ratio',
        type=float,
        default=0.4,
        help='선호도 가중치 비율 0-1 (기본값: 0.4)'
    )
    
    parser.add_argument(
        '--decay-rate',
        type=float,
        default=0.05,
        help='시간 감쇠율 (기본값: 0.05)'
    )
    
    parser.add_argument(
        '--lookback-days',
        type=int,
        default=90,
        help='읽기 이력 조회 기간 (일, 기본값: 90)'
    )
    
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='상세 로그 출력'
    )
    
    # 필터링 옵션
    parser.add_argument(
        '--only-null',
        action='store_true',
        default=True,
        help='임베딩이 NULL인 사용자만 처리 (기본값: True)'
    )
    
    parser.add_argument(
        '--force-all',
        action='store_true',
        help='임베딩 존재 여부와 관계없이 모든 사용자 업데이트'
    )
    
    args = parser.parse_args()
    
    # 로깅 레벨 설정
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # 업데이트 대상 검증
    if not (args.all or args.user_id or args.user_ids):
        parser.print_help()
        print("\n❌ 오류: --all, --user-id, 또는 --user-ids 중 하나를 지정해야 합니다")
        sys.exit(1)
    
    # 실행
    try:
        if args.all:
            # only_null 설정: --force-all이면 False, 아니면 True
            only_null = not args.force_all
            
            # 전체 사용자 업데이트
            results = update_all_users(
                min_interactions=args.min_interactions,
                preference_ratio=args.preference_ratio,
                decay_rate=args.decay_rate,
                lookback_days=args.lookback_days,
                only_null=only_null
            )
            
            print(f"\n{'='*80}")
            print(f"✅ 전체 사용자 업데이트 완료")
            print(f"  성공: {results['success']}명")
            print(f"  실패: {results['failed']}명")
            print(f"  건너뜀: {results['skipped']}명")
            print(f"{'='*80}\n")
            
        elif args.user_id:
            # 단일 사용자 업데이트
            success = update_single_user(
                user_id=args.user_id,
                preference_ratio=args.preference_ratio,
                decay_rate=args.decay_rate,
                lookback_days=args.lookback_days
            )
            
            if success:
                print(f"\n✅ User {args.user_id} 업데이트 완료\n")
                sys.exit(0)
            else:
                print(f"\n❌ User {args.user_id} 업데이트 실패\n")
                sys.exit(1)
            
        elif args.user_ids:
            # 여러 사용자 업데이트
            user_ids = [int(uid.strip()) for uid in args.user_ids.split(',')]
            
            results = update_multiple_users(
                user_ids=user_ids,
                preference_ratio=args.preference_ratio,
                decay_rate=args.decay_rate,
                lookback_days=args.lookback_days
            )
            
            print(f"\n{'='*80}")
            print(f"✅ {len(user_ids)}명 사용자 업데이트 완료")
            print(f"  성공: {results['success']}명")
            print(f"  실패: {results['failed']}명")
            print(f"  건너뜀: {results['skipped']}명")
            print(f"{'='*80}\n")
        
    except KeyboardInterrupt:
        print("\n\n⚠️ 사용자에 의해 중단됨")
        sys.exit(130)
    
    except Exception as e:
        logger.error(f"\n❌ 오류 발생: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
