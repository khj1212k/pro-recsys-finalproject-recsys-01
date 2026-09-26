"""cluster: LLM 호출 없이 HDBSCAN(+split_v2)만 돌려 클러스터 통계를 job_runs에 남긴다(읽기 전용).

generate를 켜기 전에도 매일 클러스터 수/노이즈 비율 추이를 쌓아 두기 위한 잡이다.
cluster_history(run_id)는 생성 배치와 1:1이라 여기서는 쓰지 않는다.
"""
from typing import Any, Dict


def add_arguments(parser) -> None:
    parser.add_argument("--lookback-hours", type=int, default=None, help="기본: Settings.CLUSTER_LOOKBACK_HOURS")


def run(ctx) -> Dict[str, Any]:
    from config.settings import Settings
    from core.clusterer import NewsClusterer
    from pipeline.stages import _compute_clustering_stats

    params = {
        "min_cluster_size": Settings.HDBSCAN_MIN_CLUSTER_SIZE,
        "min_samples": Settings.HDBSCAN_MIN_SAMPLES,
        "lookback_hours": ctx.args.lookback_hours or Settings.CLUSTER_LOOKBACK_HOURS,
    }
    clusterer = NewsClusterer()
    clusters = clusterer.cluster_news(**params)
    stats = _compute_clustering_stats(clusterer, clusters, params)
    sizes = sorted((len(ids) for ids in clusters.values()), reverse=True)
    stats["clustered_articles"] = sum(sizes)
    stats["cluster_sizes_top10"] = sizes[:10]
    stats["cluster_size_median"] = sizes[len(sizes) // 2] if sizes else None
    return {"clustering": stats}
