"""Pipeline Stages: 간소화된 파이프라인 단계 정의"""
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Dict
from tqdm import tqdm

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
        from crawler.content_extractor import ContentExtractor
        return ContentExtractor().extract_parallel(num_workers)

class Stage3_NewsEmbedding(PipelineStage):
    """기사 임베딩 (NewsEmbedder -> news_raw 테이블에 저장)"""
    def execute(self, force_cpu=False, batch_size=None, **kwargs) -> int:
        from core.embedder import NewsEmbedder
        from db.connection import get_connection, release_connection

        batch_size = batch_size or self.settings.EMBEDDING_BATCH_SIZE
        count = 0
        failed_batches = 0

        # 임베딩 모델을 마지막에 VRAM에서 확실히 제거하기 위해 with문 사용
        with NewsEmbedder(force_cpu=force_cpu, verbose=True) as embedder:
            conn = get_connection()
            try:
                with conn.cursor() as cur:
                    # 임베딩이 없고 본문이 실제로 채워진 기사만 조회.
                    # raw_news_content가 비어있는 행은 대상에서 제외하고 embedding_result를
                    # NULL로 남겨둔다 -> 본문이 채워지면 다음 실행에서 자동으로 재검토된다.
                    cur.execute("""
                        SELECT raw_news_id, raw_news_title, raw_news_content
                        FROM news_raw
                        WHERE embedding_result IS NULL
                          AND raw_news_content IS NOT NULL
                          AND raw_news_content != ''
                    """)
                    rows = cur.fetchall() # 임베딩 없는 기사 목록 (본문 있는 것만)

                    cur.execute("""
                        SELECT COUNT(*) FROM news_raw
                        WHERE embedding_result IS NULL
                          AND (raw_news_content IS NULL OR raw_news_content = '')
                    """)
                    pending_no_content = cur.fetchone()[0]
                    if pending_no_content:
                        logger.info(
                            f"⏭️  본문 미수집으로 임베딩 보류: {pending_no_content}건 "
                            f"(본문이 채워지면 다음 실행에서 처리됩니다)"
                        )

                    if not rows:
                        logger.info("건너뜀: 임베딩할 새로운 기사가 없습니다.")
                        return 0

                    logger.info(f"🚀 기사 {len(rows)}건 임베딩 시작 (Batch: {batch_size})...")

                    # 배치 처리: 배치 하나가 실패해도 나머지 배치는 계속 처리한다
                    # (이전에는 예외가 루프 전체를 중단시켜 이후 배치가 전부 스킵됐음).
                    for i in tqdm(range(0, len(rows), batch_size), desc="🚀 Embedding Articles", unit="batch"):
                        batch = rows[i:i+batch_size]
                        try:
                            # 제목 + 본문 결합
                            texts = [f"{r[1]} {r[2]}"[:8000] for r in batch]
                            embeddings, _ = embedder.generate_embeddings_batch(texts, batch_size)

                            # 저장
                            updates = [(emb, r[0]) for emb, r in zip(embeddings, batch)]
                            cur.executemany("UPDATE news_raw \
                                             SET embedding_result=%s \
                                             WHERE raw_news_id=%s", updates) # (emb, raw_news_id)
                            conn.commit()
                            count += len(updates)
                        except Exception as e:
                            conn.rollback()
                            failed_batches += 1
                            logger.error(f"❌ 배치 임베딩 실패 (batch {i // batch_size}), 건너뛰고 계속 진행: {e}")

            except Exception as e:
                # 배치 루프 진입 전(쿼리 준비 단계 등) 실패 - 지금까지 커밋된 count는 보존한다
                conn.rollback()
                logger.error(f"임베딩 준비 단계 실패: {e}")
            finally:
                release_connection(conn)

        if failed_batches:
            logger.warning(f"⚠️  총 {failed_batches}개 배치가 실패하여 건너뛰었습니다.")

        return count

def _compute_clustering_stats(clusterer, clusters, effective_params) -> Dict[str, Any]:
    """클러스터링 실행 통계(전체 기사 수, 클러스터 수, noise 비율)를 계산.

    clusterer.labels_가 없거나(예: 테스트 목) 길이를 알 수 없는 경우에도 파이프라인이
    죽지 않도록 방어적으로 계산한다.
    """
    n_articles = None
    noise_ratio = None
    try:
        labels = getattr(clusterer, "labels_", None)
        if labels is not None:
            labels_list = list(labels)
            n_articles = len(labels_list)
            if n_articles:
                noise_count = sum(1 for label in labels_list if int(label) == -1)
                noise_ratio = noise_count / n_articles
    except Exception as e:
        logger.warning(f"클러스터링 통계 계산 실패 (무시하고 계속 진행): {e}")

    return {
        "n_articles": n_articles,
        "n_clusters": len(clusters),
        "noise_ratio": noise_ratio,
        "effective_params": effective_params,
    }


class Stage5_NewsletterGeneration(PipelineStage):
    """뉴스레터 생성 (Clustering + Workflow)"""
    def execute(self, limit=None, min_cluster_size=None, min_samples=None,
                min_target=None, lookback_hours=None, **kwargs) -> int:
        from core.clusterer import NewsClusterer
        from workflow.graph import compile_workflow
        from core.llm_metrics import get_metrics_collector
        from db.batch_manager import create_new_batch

        # Start LLM metrics collection
        metrics = get_metrics_collector()
        metrics.start_batch()

        # 유효 파라미터 결정: CLI(명시적으로 넘어온 값) > Settings > 하드코딩 기본값
        effective_min_cluster_size = (
            min_cluster_size if min_cluster_size is not None
            else getattr(self.settings, "HDBSCAN_MIN_CLUSTER_SIZE", 3)
        )
        effective_min_samples = (
            min_samples if min_samples is not None
            else getattr(self.settings, "HDBSCAN_MIN_SAMPLES", 2)
        )
        effective_min_target = (
            min_target if min_target is not None
            else getattr(self.settings, "MIN_NEWSLETTER_TARGET", 0)
        )
        effective_lookback_hours = (
            lookback_hours if lookback_hours is not None
            else getattr(self.settings, "CLUSTER_LOOKBACK_HOURS", 24)
        )

        logger.info(
            "🎛️  클러스터링 유효 파라미터: "
            f"min_cluster_size={effective_min_cluster_size}, min_samples={effective_min_samples}, "
            f"min_target={effective_min_target}, lookback_hours={effective_lookback_hours}"
        )

        # 1. 클러스터링
        clusterer = NewsClusterer()
        clusters = clusterer.cluster_news(
            min_cluster_size=effective_min_cluster_size,
            min_samples=effective_min_samples,
            lookback_hours=effective_lookback_hours,
        )

        if not clusters:
            logger.info("생성된 클러스터가 없습니다.")
            return 0

        effective_params = {
            "min_cluster_size": effective_min_cluster_size,
            "min_samples": effective_min_samples,
            "min_target": effective_min_target,
            "lookback_hours": effective_lookback_hours,
        }
        clustering_stats = _compute_clustering_stats(clusterer, clusters, effective_params)
        logger.info(f"📊 클러스터링 통계: {clustering_stats}")

        # 2. 정식 run_id 발급 (cluster_history에 이번 배치 기록 + 클러스터링 통계 첨부)
        # clusters 원본을 그대로 변형하지 않도록 얕은 복사 후 통계 항목을 추가한다
        # (all_cluster_groups로도 그대로 재사용되기 때문).
        cluster_log = dict(clusters)
        cluster_log["clustering_stats"] = clustering_stats
        run_id = create_new_batch(cluster_log)
        logger.info(f"🆔 배치 run_id={run_id} 발급 완료")

        # 3. 워크플로우 실행 (뉴스레터 생성)
        app = compile_workflow()
        count = 0

        # 데이터 준비 (한 번에 로드)
        data = clusterer.get_clustered_articles(list(clusters.keys()))

        all_ids = sorted(list(clusters.keys()), reverse=True) # 최신순? (ID가 크면 최신이라 가정)
        attempt_ids = all_ids[:limit] if limit else all_ids

        logger.info(
            f"🚀 뉴스레터 생성 워크플로우 시작 (대상 클러스터: {len(attempt_ids)}개, "
            f"min_target={effective_min_target})"
        )

        def process_cluster(cid, idx) -> bool:
            state = {
                "current_cluster_id": cid,
                "current_cluster_index": idx,
                "all_cluster_ids": all_ids,
                "all_cluster_groups": clusters,
                "data": data,
                "run_id": run_id,
            }
            try:
                final = app.invoke(state)
                return bool(final.get("completed_newsletters"))
            except Exception as e:
                logger.error(f"Clubster {cid} 처리 중 에러: {e}")
                return False

        for i, cid in enumerate(attempt_ids):
            if process_cluster(cid, i):
                count += 1

        # min_target 미달 시, limit으로 제외됐던 나머지 클러스터를 순서대로 추가 시도
        # (min_target=0이면 기존과 동일하게 limit에서 정확히 끊긴다 - 기본 동작 보존).
        if limit and effective_min_target and count < effective_min_target:
            for i in range(len(attempt_ids), len(all_ids)):
                if count >= effective_min_target:
                    break
                if process_cluster(all_ids[i], i):
                    count += 1

        # End LLM metrics collection, print summary, and persist for later cost/latency 분석
        metrics.end_batch()
        metrics.print_summary()
        metrics.save_summary(os.path.join("logs", f"llm_metrics_run{run_id}.json"))

        logger.info(f"✨ 뉴스레터 생성 완료: {count}건 (run_id={run_id})")
        return count

