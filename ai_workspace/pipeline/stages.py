"""Pipeline Stages: 간소화된 파이프라인 단계 정의"""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict

logger = logging.getLogger(__name__)

class PipelineStage(ABC):
    """파이프라인 단계 기본 클래스"""
    def __init__(self, settings):
        self.settings = settings

    # 추상 메소드: 하위 클래스에서 구현, 실행함수이름 execute로 통일
    @abstractmethod
    def execute(self, **kwargs) -> Any:
        pass

class Stage0_UserEmbedding(PipelineStage):
    """UserEmbedding 생성"""
    def execute(self, **kwargs) -> Dict[str, int]:
        from core.user_embedder import UserEmbedder
        return UserEmbedder().batch_update_all_users()

class Stage1_RSSCollection(PipelineStage):
    """RSS 수집"""
    def execute(self, **kwargs) -> int:
        from crawler.rss_collector import collect_rss
        return collect_rss()

class Stage2_ContentExtraction(PipelineStage):
    """본문 추출"""
    def execute(self, num_workers=None, **kwargs) -> int:
        from crawler.content_extractor.extractor import ContentExtractor
        return ContentExtractor().extract_parallel(num_workers)

class Stage3_NewsEmbedding(PipelineStage):
    """기사 임베딩 (NewsEmbedder -> news_raw 테이블에 저장)"""
    def execute(self, force_cpu=False, batch_size=None, **kwargs) -> int:
        from core.embedder import NewsEmbedder
        from db.connection import get_connection
        from tqdm import tqdm
        
        batch_size = batch_size or self.settings.EMBEDDING_BATCH_SIZE
        count = 0
        
        # 임베딩 모델을 마지막에 VRAM에서 확실히 제거하기 위해 with문 사용
        with NewsEmbedder(force_cpu=force_cpu, verbose=True) as embedder:
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    # 임베딩 없는 기사 조회
                    cur.execute("SELECT raw_news_id, raw_news_title, raw_news_content \
                                 FROM news_raw \
                                 WHERE embedding_result IS NULL")
                    rows = cur.fetchall() # 임베딩 없는 기사 목록

                    if not rows:
                        logger.info("건너뜀: 임베딩할 새로운 기사가 없습니다.")
                        return 0

                    logger.info(f"🚀 기사 {len(rows)}건 임베딩 시작 (Batch: {batch_size})...")

                    # 배치 처리
                    for i in tqdm(range(0, len(rows), batch_size), desc="embedding"):
                        batch = rows[i:i+batch_size]
                        texts = [f"{r[1]} {r[2]}"[:8000] for r in batch]
                        try:
                            embeddings, _ = embedder.generate_embeddings_batch(texts, batch_size)
                        except Exception as e:
                            logger.info(f"ℹ️ 임베딩 배치 실패 (batch {i//batch_size + 1}): {e}")
                            conn.rollback()
                            continue

                        if not embeddings:
                            continue

                        updates = [(emb, r[0]) for emb, r in zip(embeddings, batch)]
                        if updates:
                            cur.executemany("UPDATE news_raw \
                                             SET embedding_result=%s \
                                             WHERE raw_news_id=%s", updates)
                            count += len(updates)
                            conn.commit()

            except Exception as e:
                conn.rollback()
                logger.info(f"ℹ️ 임베딩 실패: {e}")
            finally:
                conn.close()
                
        return count

class Stage5_NewsletterGeneration(PipelineStage):
    """뉴스레터 생성 (Clustering + Workflow)"""
    def execute(self, limit=None, min_cluster_size=3, min_samples=2, min_target=0, **kwargs) -> int:
        from core.clusterer import NewsClusterer
        from workflow.graph import compile_workflow
        from tqdm import tqdm
        
        # 1. 클러스터링
        logger.info("🧩 뉴스 클러스터링 수행 중...")
        clusterer = NewsClusterer()
        clusters = clusterer.cluster_news(min_cluster_size=min_cluster_size, min_samples=min_samples)
        
        if not clusters:
            logger.info("생성된 클러스터가 없습니다.")
            return 0
            
        # 2. 워크플로우 실행 (뉴스레터 생성)
        app = compile_workflow()
        count = 0
        total = len(clusters) if not limit else min(len(clusters), limit)
        
        logger.info(f"🚀 뉴스레터 생성 워크플로우 시작 (대상 클러스터: {total}개)")

        # 데이터 준비 (한 번에 로드)
        data = clusterer.get_clustered_articles(list(clusters.keys()))
        
        all_ids = sorted(list(clusters.keys()), reverse=True) # 최신순? (ID가 크면 최신이라 가정)
        if limit: all_ids = all_ids[:limit]

        for i, cid in enumerate(tqdm(all_ids, desc="generating newsletter")):
            state = {
                "current_cluster_id": cid,
                "current_cluster_index": i,
                "all_cluster_ids": all_ids,
                "all_cluster_groups": clusters,
                "data": data,
                "run_id": int(logging.getLogger().name) if logging.getLogger().name.isdigit() else 0 # 임시 run_id
            }
            
            try:
                final = app.invoke(state)
                if final.get("completed_newsletters"):
                    count += 1
            except Exception as e:
                logger.info(f"ℹ️ Cluster {cid} 처리 중 문제: {e}")

        logger.info(f"✨ 뉴스레터 생성 완료: {count}건")
        return count
