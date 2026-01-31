"""
AI Workspace - LangGraph News Pipeline
Main entry point for the complete news processing workflow
"""
import argparse
import sys
import os
import logging
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import get_settings
from pipeline.runner import PipelineRunner
from db.schema import full_reset
from utils.logger import setup_logger

load_dotenv(override=True)

# Setup root logger
setup_logger("ai_workspace", level=logging.INFO)
logger = logging.getLogger(__name__)


def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="AI Workspace News Pipeline")
    
    # Execution options
    parser.add_argument("--no-reset", action="store_true", help="Skip DB reset")
    parser.add_argument("--workers", type=int, default=8, help="Number of workers for extraction")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of clusters to process")
    parser.add_argument("--min-target", type=int, default=0, help="Minimum target number of newsletters")
    
    # Model/Resource options
    parser.add_argument("--force-cpu", action="store_true", help="Force CPU for embeddings")
    parser.add_argument("--batch-size", type=int, default=None, help="Batch size for embedding")
    
    # Clustering options
    parser.add_argument("--min-cluster-size", type=int, default=3, help="HDBSCAN min_cluster_size")
    parser.add_argument("--min-samples", type=int, default=2, help="HDBSCAN min_samples")
    
    return parser.parse_args()


def main():
    """Main entry point"""
    args = parse_args()
    settings = get_settings()
    
    logger.info(f"Starting pipeline with DB: {os.getenv('DB_NAME')}")
    
    # DB Reset Logic
    if not args.no_reset:
        db_name = os.getenv("DB_NAME", "")
        if db_name == 'test_db':
            logger.info(f"🗑️  Resetting database: {db_name}")
            full_reset()
        else:
            logger.warning(f"⚠️  Skipping full reset (DB_NAME={db_name} != test_db)")
    
    # Initialize and run pipeline
    runner = PipelineRunner(settings)
    
    try:
        runner.run_full_pipeline(
            reset_db=not args.no_reset,
            num_workers=args.workers,
            force_cpu=args.force_cpu,
            limit=args.limit,
            min_cluster_size=args.min_cluster_size,
            min_samples=args.min_samples,
            min_target=args.min_target,
            batch_size=args.batch_size
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
