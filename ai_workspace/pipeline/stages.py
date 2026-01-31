"""
Pipeline Stage Implementations

This module defines the abstract base class for pipeline stages and implementations
for all stages of the news processing pipeline.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List
import logging

from config.settings import Settings

logger = logging.getLogger(__name__)


class PipelineStage(ABC):
    """Abstract base class for all pipeline stages"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.name = self.__class__.__name__
    
    @abstractmethod
    def execute(self, *args, **kwargs) -> Any:
        """Execute the stage logic"""
        pass


class Stage0_UserEmbedding(PipelineStage):
    """Stage 0: User Embedding Generation"""
    
    def execute(self) -> Dict[str, int]:
        logger.info("=" * 60)
        logger.info("👤 Stage 0: User Embedding Generation")
        logger.info("=" * 60)
        
        from core.user_embedder import UserEmbedder
        
        embedder = UserEmbedder()
        result = embedder.batch_update_all_users(only_null=True)
        
        logger.info(f"  ✅ Success: {result.get('success', 0)}")
        logger.info(f"  ❌ Failed: {result.get('failed', 0)}")
        logger.info(f"  ⏭️  Skipped: {result.get('skipped', 0)}")
        
        return result


class Stage1_RSSCollection(PipelineStage):
    """Stage 1: RSS News Collection"""
    
    def execute(self) -> int:
        logger.info("\n" + "=" * 60)
        logger.info("📡 Stage 1: RSS News Collection")
        logger.info("=" * 60)
        
        from crawler.rss_collector import RssCollector
        collector = RssCollector()
        return collector.collect_rss()


class Stage2_ContentExtraction(PipelineStage):
    """Stage 2: Article Content Extraction"""
    
    def execute(self, num_workers: int = 8) -> int:
        logger.info("\n" + "=" * 60)
        logger.info("📰 Stage 2: Content Extraction")
        logger.info("=" * 60)
        
        from crawler.content_extractor import ContentExtractor
        extractor = ContentExtractor()
        return extractor.extract_parallel(num_workers=num_workers)


class Stage3_NewsEmbedding(PipelineStage):
    """Stage 3: Article Embedding Generation"""
    
    def execute(self, force_cpu: bool = False, batch_size: Optional[int] = None) -> int:
        logger.info("\n" + "=" * 60)
        logger.info("🔢 Stage 3: Article Embedding Generation")
        logger.info("=" * 60)
        
        from crawler.embedding_generator import generate_embeddings_for_articles
        return generate_embeddings_for_articles(force_cpu=force_cpu, batch_size=batch_size)


class Stage4_Clustering(PipelineStage):
    """Stage 4: Clustering (Part of the adaptive loop logic)"""
    
    def execute(self, min_cluster_size: int = 3, min_samples: int = 2, min_target: int = 0) -> Any:
        # Note: In the original main.py, clustering and newsletter generation are tightly coupled
        # in run_newsletter_adaptive. We might keep them coupled or separate them here.
        # For now, we will likely call this as part of the combined stage or helper.
        pass


class Stage5_NewsletterGeneration(PipelineStage):
    """Stage 4-5: Clustering + Newsletter Generation (Adaptive Loop)"""
    
    def execute(
        self, 
        limit: Optional[int] = None, 
        min_cluster_size: int = 3, 
        min_samples: int = 2,
        min_target: int = 5,
        force_cpu: bool = False,
        batch_size: Optional[int] = None
    ) -> int:
        logger.info("\n" + "=" * 60)
        logger.info("🚀 Stage 4-5: Clustering + Newsletter Generation")
        logger.info("=" * 60)
        
        # We implementation the logic from run_newsletter_adaptive here
        
        total_created = 0
        current_min_cluster = min_cluster_size
        current_min_samples = min_samples
        
        # Iteration limits
        MAX_ITERATIONS = 3
        iteration = 0
        
        while iteration < MAX_ITERATIONS:
            iteration += 1
            logger.info(f"\n🔄 Iteration {iteration}/{MAX_ITERATIONS} (min_features={current_min_cluster})")
            
            created_count = self._run_single_pass(
                limit=limit,
                min_cluster_size=current_min_cluster,
                min_samples=current_min_samples
            )
            
            total_created += created_count
            logger.info(f"   Created {created_count} newsletters in this pass. Total: {total_created}")
            
            # Check target
            if total_created >= min_target:
                logger.info(f"✨ Target reached ({total_created} >= {min_target}). Stopping.")
                break
                
            if created_count == 0:
                logger.info("⚠️  No newsletters created in this pass. Relaxing constraints...")
                if current_min_cluster > 3:
                    current_min_cluster = max(3, current_min_cluster - 1)
                    current_min_samples = max(2, current_min_samples - 1)
                else:
                    logger.info("   Cannot relax further. Stopping.")
                    break
        
        return total_created

    def _run_single_pass(
        self,
        limit: Optional[int] = None,
        min_cluster_size: int = 3,
        min_samples: int = 2
    ) -> int:
        """Run a single pass of clustering and generation"""
        from core.clusterer import run_clustering, get_cluster_groups
        from workflow.graph import compile_workflow
        from sklearn.metrics import silhouette_score
        import traceback
        
        logger.info("\n[4/5] Performing HDBSCAN clustering...")
        
        # Load embeddings and cluster
        cluster_groups, cluster_ids, data = run_clustering(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            min_target=0, # Not used inside run_clustering logic mostly
        )
        
        # Check if any clusters were found
        if not cluster_ids:
            logger.info("  No clusters found. Skipping newsletter generation.")
            return 0
            
        sorted_cluster_ids = sorted(cluster_ids)
        logger.info(f"  Clusters found: {len(sorted_cluster_ids)}")
        
        # Calculate silhouette score
        try:
            score = silhouette_score(cluster_groups, data)
            logger.info(f"  Silhouette score: {score:.3f}")
        except Exception:
            logger.warning("  Could not calculate silhouette score.")
            
        logger.info(f"  Processing {len(sorted_cluster_ids)} clusters")
        
        # Apply limit if specified
        if limit and len(sorted_cluster_ids) > limit:
            sorted_cluster_ids = sorted_cluster_ids[:limit]
            logger.info(f"  Limiting to {limit} clusters")
            
        # Get batch ID
        from db.batch_manager import ClusterLog, create_new_batch
        cluster_log = ClusterLog.get_latest()
        run_id = create_new_batch(cluster_log)
        logger.info(f"  Batch ID (run_id): {run_id}")
        
        logger.info("\n[3/3] Running LangGraph workflow (Sequential Execution)...")
        
        # Compile workflow once
        app = compile_workflow()
        
        completed_count = 0
        
        for idx, cluster_id in enumerate(sorted_cluster_ids):
            logger.info(f"\n--- Processing cluster {cluster_id} ({idx+1}/{len(sorted_cluster_ids)}) ---")
            
            # Initialize state
            state = {
                "run_id": run_id,
                "all_cluster_groups": cluster_groups,
                "all_cluster_ids": sorted_cluster_ids,
                "current_cluster_index": idx,
                "current_cluster_id": cluster_id,
                "data": data,
                "completed_newsletters": [],
                "failed_clusters": [],
                "skipped_clusters": [],
                "current_article_ids": [],
                "current_articles": [],
                "cluster_eval": None,
                "cluster_retry_count": 0,
                "newsletter_draft": None,
                "newsletter_eval": None,
                "newsletter_retry_count": 0,
                "newsletter_feedback": None,
                "generation_history": None,
                "should_continue": True,
                "error_message": None
            }
            
            try:
                # Use invoke for sync execution
                res = app.invoke(state)
                
                if res.get("completed_newsletters"):
                    draft = res.get("newsletter_draft", {}) or {}
                    title = draft.get("title", "No Title")
                    logger.info(f"✅ [Cluster {cluster_id}] Completed: {title}")
                    completed_count += 1
                elif res.get("skipped_clusters"):
                    reason = "Evaluation Failed"
                    if res.get("cluster_eval") and res["cluster_eval"].get("feedback"):
                        reason = res["cluster_eval"]["feedback"]
                    logger.info(f"⏭️  [Cluster {cluster_id}] Skipped: {str(reason)[:60]}...")
                elif res.get("failed_clusters"):
                    error = res.get("error_message") or "Unknown Error"
                    logger.info(f"❌ [Cluster {cluster_id}] Failed: {str(error)[:80]}...")
                    
            except Exception as e:
                error_detail = ''.join(traceback.format_exception(type(e), e, e.__traceback__))
                logger.error(f"❌ Error processing cluster {cluster_id}:")
                logger.error(error_detail)
        
        return completed_count


class Stage6_NewsletterEmbedding(PipelineStage):
    """Stage 6: Batch Newsletter Embedding Generation"""
    
    def execute(self, force_cpu: bool = False, batch_size: Optional[int] = None) -> int:
        logger.info("\n" + "=" * 60)
        logger.info("🔖 Stage 6: Newsletter Embedding Generation (Batch)")
        logger.info("=" * 60)
        
        from crawler.embedding_generator import generate_embeddings_for_newsletters
        return generate_embeddings_for_newsletters(force_cpu=force_cpu, batch_size=batch_size)
