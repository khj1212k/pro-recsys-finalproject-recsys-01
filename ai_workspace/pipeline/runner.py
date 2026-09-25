"""
Pipeline Runner (파이프라인 실행기)
"""
from typing import Dict, Any, Optional
import logging

from config.settings import Settings
from pipeline.stages import (
    Stage0_UserEmbedding,
    Stage1_RSSCollection,
    Stage2_ContentExtraction,
    Stage3_NewsEmbedding,
    Stage5_NewsletterGeneration,
)

logger = logging.getLogger(__name__)


class PipelineRunner:
    """전체 파이프라인 실행을 조율하는 클래스"""
    
    def __init__(self, Settings):
        self.settings = Settings
    
    def run_full_pipeline(
        self,
        reset_db: bool = True,
        num_workers: int = 8,
        force_cpu: bool = False,
        limit: Optional[int] = None,
        min_cluster_size: Optional[int] = None,
        min_samples: Optional[int] = None,
        min_target: Optional[int] = None,
        lookback_hours: Optional[int] = None,
        batch_size: Optional[int] = None,
        start_stage: int = 1,
        end_stage: int = 5
    ) -> Dict[str, Any]:
        """
        전체 파이프라인을 처음부터 끝까지 실행

        Args:
            reset_db: 데이터베이스 초기화 여부 (test_db 전용)
            num_workers: 병렬 처리를 위한 작업자(Process) 수
            force_cpu: 강제로 CPU를 사용할지 여부 (GPU 미사용 시)
            limit: 처리할 클러스터 최대 개수 제한 (디버깅용)
            min_cluster_size: HDBSCAN 군집화 최소 크기 (None이면 Settings 값 사용)
            min_samples: HDBSCAN 군집화 최소 샘플 수 (None이면 Settings 값 사용)
            min_target: 생성할 뉴스레터 최소 목표 수량 (None이면 Settings 값 사용)
            lookback_hours: 클러스터링 대상 기사의 crawled_at lookback 시간 (None이면 Settings 값 사용)
            batch_size: 임베딩 생성 시 배치 크기

        Returns:
            Dict: 각 단계별 실행 결과 요약 정보
        """
        results = {}
        
        logger.info("🚀 AI 작업공간 파이프라인 시작")
        import os
        logger.info(f"환경(Environment): {os.getenv('ENV', 'dev')}")
        
        # Stage 0: User Embedding
        if start_stage <= 0 <= end_stage:
            stage0 = Stage0_UserEmbedding(self.settings)
            results['user_embedding'] = stage0.execute()
        
        # Stage 1: RSS Collection
        # rss_collector.py에서 수집된 기사들의 메타데이터 DB에 저장(뉴스원문 제외)
        if start_stage <= 1 <= end_stage:
            stage1 = Stage1_RSSCollection(self.settings)
            results['rss_collection'] = stage1.execute()
        
        # Stage 2: Content Extraction
        # 위에서 저장된 메타데이터로 뉴스원문 추출 및 DB에 저장
        if start_stage <= 2 <= end_stage:
            stage2 = Stage2_ContentExtraction(self.settings)
            results['content_extraction'] = stage2.execute(num_workers=num_workers)
        
        # Stage 3: Article Embedding
        # 뉴스원문을 임베딩 벡터로 변환
        if start_stage <= 3 <= end_stage:
            stage3 = Stage3_NewsEmbedding(self.settings)
            results['article_embedding'] = stage3.execute(
                force_cpu=force_cpu, 
                batch_size=batch_size or self.settings.EMBEDDING_BATCH_SIZE
            )
        
        # Stage 4-5: Clustering & Newsletter Generation
        # 임베딩 벡터를 기반으로 HDBSCAN 클러스터링 수행 및 뉴스레터 생성
        if start_stage <= 5 and end_stage >= 4:
            stage5 = Stage5_NewsletterGeneration(self.settings)
            results['newsletters_created'] = stage5.execute(
                limit=limit,
                min_cluster_size=min_cluster_size,
                min_samples=min_samples,
                min_target=min_target,
                lookback_hours=lookback_hours
            )
        
        # Stage 6: Newsletter Embedding
        # stage6 = Stage6_NewsletterEmbedding(self.settings)
        # 뉴스레터 임베딩 벡터로 변환
        # results['newsletter_embedding'] = stage6.execute(
        #     force_cpu=force_cpu,
        #     batch_size=batch_size or self.settings.EMBEDDING_BATCH_SIZE
        # )
        
        
        self._print_summary(results)
        
        return results

    def _print_summary(self, results: Dict[str, Any]) -> None:
        logger.info("\n" + "=" * 60)
        logger.info("📊 Pipeline Execution Summary")
        logger.info("=" * 60)
        
        rss_result = results.get('rss_collection') or {}
        logger.info(f"User Embeddings Updated: {results.get('user_embedding', {}).get('success', 0)}")
        logger.info(
            f"RSS Articles Collected: 신규 {rss_result.get('inserted', 0)}건 / "
            f"스킵(중복) {rss_result.get('skipped', 0)}건"
        )
        logger.info(f"Content Extracted: {results.get('content_extraction', 0)}")
        logger.info(f"Article Embeddings: {results.get('article_embedding', 0)}")
        logger.info(f"Newsletters Created: {results.get('newsletters_created', 0)}")
        logger.info(f"Newsletter Embeddings: {results.get('newsletter_embedding', 0)}")
        logger.info("=" * 60)
