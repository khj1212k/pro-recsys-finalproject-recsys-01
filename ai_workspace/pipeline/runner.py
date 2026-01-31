"""
Pipeline Runner

Orchestrates the execution of pipeline stages.
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
    Stage6_NewsletterEmbedding,
)

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Orchestrates the complete pipeline execution"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
    
    def run_full_pipeline(
        self,
        reset_db: bool = True,
        num_workers: int = 8,
        force_cpu: bool = False,
        limit: Optional[int] = None,
        min_cluster_size: int = 3,
        min_samples: int = 2,
        min_target: int = 0,
        batch_size: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Run the complete end-to-end pipeline.
        
        Args:
            reset_db: Whether to reset the database (if test_db)
            num_workers: Number of parallel workers for extraction
            force_cpu: Force CPU for embeddings
            limit: Limit number of clusters to process
            min_cluster_size: HDBSCAN min_cluster_size
            min_samples: HDBSCAN min_samples
            min_target: Minimum target number of newsletters to create
            batch_size: Batch size for embeddings embeddings
            
        Returns:
            Dict containing results from each stage
        """
        results = {}
        
        logger.info("🚀 Starting AI Workspace Pipeline")
        logger.info(f"Environment: {self.settings.get_environment()}")
        
        # Stage 0: User Embedding
        stage0 = Stage0_UserEmbedding(self.settings)
        results['user_embedding'] = stage0.execute()
        
        # Stage 1: RSS Collection
        stage1 = Stage1_RSSCollection(self.settings)
        results['rss_collection'] = stage1.execute()
        
        # Stage 2: Content Extraction
        stage2 = Stage2_ContentExtraction(self.settings)
        results['content_extraction'] = stage2.execute(num_workers=num_workers)
        
        # Stage 3: Article Embedding
        stage3 = Stage3_NewsEmbedding(self.settings)
        results['article_embedding'] = stage3.execute(
            force_cpu=force_cpu, 
            batch_size=batch_size or self.settings.EMBEDDING_BATCH_SIZE
        )
        
        # Stage 4-5: Clustering & Newsletter Generation
        stage5 = Stage5_NewsletterGeneration(self.settings)
        results['newsletters_created'] = stage5.execute(
            limit=limit,
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            min_target=min_target
        )
        
        # Stage 6: Newsletter Embedding
        stage6 = Stage6_NewsletterEmbedding(self.settings)
        results['newsletter_embedding'] = stage6.execute(
            force_cpu=force_cpu,
            batch_size=batch_size or self.settings.EMBEDDING_BATCH_SIZE
        )
        
        self._print_summary(results)
        
        return results

    def _print_summary(self, results: Dict[str, Any]) -> None:
        """Print execution summary"""
        logger.info("\n" + "=" * 60)
        logger.info("📊 Pipeline Execution Summary")
        logger.info("=" * 60)
        
        logger.info(f"User Embeddings Updated: {results.get('user_embedding', {}).get('success', 0)}")
        logger.info(f"RSS Articles Collected: {results.get('rss_collection', 0)}")
        logger.info(f"Content Extracted: {results.get('content_extraction', 0)}")
        logger.info(f"Article Embeddings: {results.get('article_embedding', 0)}")
        logger.info(f"Newsletters Created: {results.get('newsletters_created', 0)}")
        logger.info(f"Newsletter Embeddings: {results.get('newsletter_embedding', 0)}")
        logger.info("=" * 60)
