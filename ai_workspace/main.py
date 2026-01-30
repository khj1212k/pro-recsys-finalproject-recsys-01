"""
AI Workspace - LangGraph News Pipeline
Main entry point for the complete news processing workflow

Full Pipeline:
1. Collect (RSS) -> 2. Extract (Content) -> 3. Embed -> 4. Cluster -> 5. Newsletter (LangGraph)

테스트 환경에서는 test_db를 사용하며, 기본 실행 시 DB를 초기화합니다.
"""
import argparse
import sys
import os
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)

from db.connection import get_connection
from config.settings import Settings


def reset_test_db() -> None:
    """Test DB 초기화 (스키마 재생성 포함)"""
    from db.schema import full_reset

    db_name = os.getenv("DB_NAME", "test_db")
    print(f"\n🗑️  Resetting database: {db_name}")

    # DB 이름이 test_db일 때만 전체 리셋 허용 (안전장치)
    if db_name == 'test_db':
        full_reset()
    else:
        print(f"⚠️  Skipping full reset for safety (DB_NAME={db_name})")


def run_collect() -> int:
    """Stage 1: RSS 수집"""
    print("\n" + "=" * 60)
    print("📡 Stage 1: RSS 뉴스 수집")
    print("=" * 60)
    
    from crawler.rss_collector import RssCollector
    collector = RssCollector()
    return collector.collect_rss()


def run_extract(num_workers: int = 8) -> int:
    """Stage 2: 본문 추출"""
    print("\n" + "=" * 60)
    print("📰 Stage 2: 본문 추출")
    print("=" * 60)
    
    from crawler.content_extractor import ContentExtractor
    extractor = ContentExtractor()
    return extractor.extract_parallel(num_workers=num_workers)


def run_embed(force_cpu: bool = False, batch_size: Optional[int] = None) -> int:
    """Stage 3: 임베딩 생성"""
    print("\n" + "=" * 60)
    print("🔢 Stage 3: 임베딩 생성")
    print("=" * 60)
    
    from crawler.embedding_generator import generate_embeddings_for_articles
    return generate_embeddings_for_articles(force_cpu=force_cpu, batch_size=batch_size)


def run_newsletter(limit: Optional[int] = None, min_cluster_size: int = 3, min_samples: int = 2) -> int:
    """Stage 4-5: 클러스터링 + LangGraph 뉴스레터 생성"""
    print("\n" + "=" * 60)
    print("🚀 Stage 4-5: 클러스터링 + LangGraph 뉴스레터 생성")
    print("=" * 60)
    
    from core.clusterer import NewsClusterer, load_embeddings_from_db, get_cluster_groups
    from workflow.graph import compile_workflow
    from workflow.state import AgentState
    
    # Step 1: Load embeddings from DB
    print("\n[1/3] Loading embeddings from database...")
    try:
        conn = get_connection()
        data = load_embeddings_from_db(conn, exclude_clustered=True)
        conn.close()
    except Exception as e:
        print(f"❌ Database connection failed: {e}")
        return 0
    
    if len(data['ids']) == 0:
        print("⚠️ No unclustered articles found. Nothing to process.")
        return 0
    
    print(f"  Loaded {len(data['ids'])} articles with embeddings")
    
    # Step 2: Perform HDBSCAN clustering
    print("\n[2/3] Performing HDBSCAN clustering...")
    clusterer = NewsClusterer(
        min_cluster_size=min_cluster_size,
        min_samples=min_samples
    )
    labels = clusterer.fit_predict(data['embeddings'])
    metrics = clusterer.evaluate(data['embeddings'])
    
    print(f"  Clusters found: {metrics.get('n_clusters', 0)}")
    print(f"  Noise articles: {metrics.get('n_noise', 0)} ({metrics.get('noise_ratio', 0)*100:.1f}%)")
    if metrics.get('silhouette_score', -1) > 0:
        print(f"  Silhouette score: {metrics['silhouette_score']:.3f}")
    
    # Get cluster groups (excluding noise)
    cluster_groups = get_cluster_groups(data['ids'], labels)
    
    if not cluster_groups:
        print("⚠️ No valid clusters formed. Nothing to process.")
        return 0
    
    # Sort by size and apply limit
    sorted_cluster_ids = sorted(cluster_groups.keys(), key=lambda k: len(cluster_groups[k]), reverse=True)
    if limit:
        sorted_cluster_ids = sorted_cluster_ids[:limit]
    
    print(f"  Processing {len(sorted_cluster_ids)} clusters" + (f" (limited to top {limit})" if limit else ""))
    
    # Step 3: Run LangGraph workflow (Parallel)
    import asyncio
    
    print("\n[3/3] Running LangGraph workflow (Parallel Execution)...")
    
    # Compile workflow once
    app = compile_workflow()
    
    # Semaphore to limit concurrency (avoid Rate Limit)
    semaphore = asyncio.Semaphore(5)

    async def process_single_cluster(cluster_id):
        async with semaphore:
            # Initialize state for this cluster
            state = {
                "all_cluster_groups": cluster_groups,
                "all_cluster_ids": sorted_cluster_ids,
                "current_cluster_index": 0, # Unused in parallel mode
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
            "should_continue": True,
            "error_message": None
        }
        
        try:
            # Use ainvoke for async execution
            return await app.ainvoke(state)
        except Exception as e:
            print(f"Error processing cluster {cluster_id}: {e}")
            return {"failed_clusters": [cluster_id], "error_message": str(e)}

    async def run_parallel():
        tasks = [process_single_cluster(cid) for cid in sorted_cluster_ids]
        results = []
        
        for future in asyncio.as_completed(tasks):
            res = await future
            results.append(res)
            
            cid = res.get("current_cluster_id")
            
            if res.get("completed_newsletters"):
                draft = res.get("newsletter_draft", {})
                title = draft.get("title", "No Title")
                print(f"✅ [Cluster {cid}] Completed: {title}")
                
            elif res.get("skipped_clusters"):
                # Reason extraction
                reason = "Evaluation Failed"
                if res.get("cluster_eval") and res["cluster_eval"].get("feedback"):
                    reason = res["cluster_eval"]["feedback"]
                print(f"⏭️  [Cluster {cid}] Skipped: {reason.replace(chr(10), ' ')[:60]}...")
                
            elif res.get("failed_clusters"):
                error = res.get("error_message") or "Unknown Error"
                print(f"❌ [Cluster {cid}] Failed: {error.replace(chr(10), ' ')[:80]}...")
                
        return results

    # Execute parallel processing
    results = asyncio.run(run_parallel())
    
    # Aggregate results for summary
    total_completed = 0
    total_failed = 0
    total_skipped = 0
    
    for res in results:
        total_completed += len(res.get('completed_newsletters', []))
        total_failed += len(res.get('failed_clusters', []))
        total_skipped += len(res.get('skipped_clusters', []))
        
    print("\n" + "=" * 60)
    print("📊 Pipeline Summary")
    print("=" * 60)
    print(f"  Newsletters created: {total_completed}")
    print(f"  Clusters failed: {total_failed}")
    print(f"  Clusters skipped: {total_skipped}")

    return total_completed


def run_newsletter_adaptive(limit: Optional[int] = None, min_cluster_size: int = 5, min_samples: int = 2, min_target: int = 0) -> int:
    """Run newsletter generation with adaptive cluster sizing to meet minimum target"""
    total_created = 0
    current_min_cluster = min_cluster_size
    
    # First pass
    created = run_newsletter(limit=limit, min_cluster_size=current_min_cluster, min_samples=min_samples)
    total_created += created
    
    # Adaptive loop: if target not met and can reduce cluster size
    while min_target > 0 and total_created < min_target and current_min_cluster > 2:
        print(f"\n⚠️ Target not met ({total_created}/{min_target}). Reducing min_cluster_size from {current_min_cluster} to {current_min_cluster - 1}...")
        current_min_cluster -= 1
        
        # Retry with smaller cluster size (incremental)
        created = run_newsletter(limit=limit, min_cluster_size=current_min_cluster, min_samples=min_samples)
        total_created += created
        
        if created == 0 and current_min_cluster <= 2:
             # Stop if hitting bottom and no results
             pass

    print(f"\n🏁 Final Result: {total_created} newsletters created (Target: {min_target})")
    return total_created


def run_full_pipeline(
    num_workers: int = 8,
    force_cpu: bool = False,
    batch_size: Optional[int] = None,
    limit: Optional[int] = None,
    min_cluster_size: int = 5,
    min_samples: int = 2,
    min_target: int = 0,
    reset_db: bool = True
) -> None:
    """전체 파이프라인 실행"""
    print("\n" + "=" * 60)
    print("🚀 AI Workspace - Full Pipeline")
    print("=" * 60)
    
    # DB 초기화 (test 모드)
    if reset_db:
        reset_test_db()
    
    # 1. RSS 수집
    run_collect()
    
    # 2. 본문 추출
    run_extract(num_workers=num_workers)
    
    # 3. 임베딩 생성
    run_embed(force_cpu=force_cpu, batch_size=batch_size)
    
    # 4-5. 클러스터링 + 뉴스레터 (Adaptive Loop)
    run_newsletter_adaptive(
        limit=limit, 
        min_cluster_size=min_cluster_size, 
        min_samples=min_samples, 
        min_target=min_target
    )


def print_status() -> None:
    """Print current database status"""
    conn = get_connection()
    cur = conn.cursor()
    
    db_name = os.getenv("DB_NAME", "unknown")
    
    print("\n" + "=" * 60)
    print(f"📊 Database Status: {db_name}")
    print("=" * 60)
    
    # Article stats
    cur.execute("""
        SELECT
            COUNT(*) as total,
            COUNT(CASE WHEN raw_news_content IS NOT NULL AND raw_news_content != '' THEN 1 END) as with_content,
            COUNT(CASE WHEN embedding_result IS NOT NULL THEN 1 END) as with_embedding,
            COUNT(CASE WHEN news_letter_id IS NULL AND embedding_result IS NOT NULL THEN 1 END) as pending,
            COUNT(CASE WHEN news_letter_id > 0 THEN 1 END) as in_newsletter,
            COUNT(CASE WHEN news_letter_id = -1 THEN 1 END) as dropped
        FROM news_raw
    """)
    row = cur.fetchone()
    
    print(f"\n[News_Raw Articles]")
    print(f"  Total: {row[0]}")
    print(f"  With content: {row[1]}")
    print(f"  With embedding: {row[2]}")
    print(f"  Pending (ready for clustering): {row[3]}")
    print(f"  In newsletters: {row[4]}")
    print(f"  Dropped: {row[5]}")
    
    # Newsletter stats
    cur.execute("SELECT COUNT(*) FROM news_letter")
    count = cur.fetchone()[0]
    print(f"\n[News_Letter]")
    print(f"  Total newsletters: {count}")
    
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Workspace - LangGraph News Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                       # Run full pipeline (default)
  python main.py --no-reset            # Run without DB reset
  python main.py --collect             # Only collect RSS
  python main.py --extract             # Only extract content
  python main.py --embed               # Only generate embeddings
  python main.py --newsletter --limit 5  # Only run newsletter generation (top 5 clusters)
  python main.py --status              # Show database status
        """
    )
    
    # Stage selection (default: run full pipeline)
    parser.add_argument('--collect', action='store_true', help='Only collect RSS feeds')
    parser.add_argument('--extract', action='store_true', help='Only extract article content')
    parser.add_argument('--embed', action='store_true', help='Only generate embeddings')
    parser.add_argument('--newsletter', action='store_true', help='Only run newsletter generation (cluster + LangGraph)')
    parser.add_argument('--status', action='store_true', help='Show database status')
    
    # Options
    parser.add_argument('--no-reset', action='store_true', help='Skip DB reset (for incremental runs)')
    parser.add_argument('--limit', type=int, default=None, help='Max clusters to process (default: all)')
    parser.add_argument('--min-cluster', type=int, default=5, help='HDBSCAN min_cluster_size')
    parser.add_argument('--min-target', type=int, default=0, help='Minimum newsletters to generate (adaptive reduction)')
    parser.add_argument('--min-samples', type=int, default=2, help='HDBSCAN min_samples')
    parser.add_argument('--workers', type=int, default=8, help='Number of parallel workers for extraction')
    parser.add_argument('--force-cpu', action='store_true', help='Force CPU for embedding (no GPU)')
    parser.add_argument('--batch-size', type=int, default=20, help='Batch size for embedding generation (default: 10)')
    
    args = parser.parse_args()
    
    if args.status:
        print_status()
        return
    
    # Individual stages
    if args.collect:
        run_collect()
    elif args.extract:
        run_extract(num_workers=args.workers)
    elif args.embed:
        run_embed(force_cpu=args.force_cpu, batch_size=args.batch_size)
    elif args.newsletter:
        run_newsletter_adaptive(
            limit=args.limit,
            min_cluster_size=args.min_cluster,
            min_samples=args.min_samples,
            min_target=args.min_target
        )
    else:
        # Default: run full pipeline
        run_full_pipeline(
            num_workers=args.workers,
            force_cpu=args.force_cpu,
            batch_size=args.batch_size,
            limit=args.limit,
            min_cluster_size=args.min_cluster,
            min_samples=args.min_samples,
            min_target=args.min_target,
            reset_db=not args.no_reset
        )


if __name__ == "__main__":
    main()
